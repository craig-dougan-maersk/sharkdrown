"""OpenAI provider — full Anthropic↔OpenAI protocol conversion.

The proxy's job on the OpenAI side is to translate the Anthropic
``/v1/messages`` request shape into an OpenAI ``/v1/chat/completions``
request, send it to OpenAI, then translate the response (or streaming SSE)
back into Anthropic-shaped bytes so the Claude Code client sees no
difference.

The translation logic lives in three module-level functions so it's easy
to unit-test without instantiating a ``Provider``:

  - ``convert_anthropic_to_openai`` — request body
  - ``transform_openai_response_to_anthropic`` — non-streaming response
  - ``transform_openai_stream_to_anthropic`` — streaming SSE frames

The ``OpenAIProvider`` class wires those into a single ``forward`` method
that builds the right ``httpx.Request`` (URL, headers, body) and dispatches.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin

import httpx

from ccproxy.app.models.anthropic import (
    AnthropicContentBlockText,
    AnthropicContentBlockToolResult,
    AnthropicContentBlockToolUse,
    AnthropicMessage,
    AnthropicRequest,
    AnthropicSystemMessage,
    AnthropicTool,
)
from ccproxy.app.providers.base import Provider

logger = logging.getLogger("claude_code_proxy.providers.openai")

# ---- Constants from the spec ------------------------------------------------

_MAX_TOKENS_O_SERIES_CAP = 16384

# OpenAI rejects these name patterns; we coerce them to the openai form.
_O_SERIES_PREFIXES = ("o1", "o3")


# ---- Request: Anthropic → OpenAI ------------------------------------------


def _flatten_system(req: AnthropicRequest) -> str | None:
    """Anthropic has an array of system blocks; OpenAI has one system message.

    Concatenate the texts with double-newline separators so paragraph
    boundaries are preserved.
    """
    if not req.system:
        return None
    if isinstance(req.system, str):
        return req.system
    parts = [s.text for s in req.system if isinstance(s, AnthropicSystemMessage)]
    if not parts:
        return None
    return "\n\n".join(parts)


def _convert_tool(t: AnthropicTool) -> dict[str, Any]:
    """Anthropic ``Tool`` → OpenAI ``{type:"function", function:{...}}``."""
    schema = dict(t.input_schema or {})
    # OpenAI requires ``type`` on the schema object.
    if not schema.get("type"):
        schema["type"] = "object"
    # Array-typed properties missing ``items`` get a default; OpenAI rejects
    # otherwise. This mirrors the original Go proxy behaviour.
    for prop in (schema.get("properties") or {}).values():
        if prop.get("type") == "array" and "items" not in prop:
            prop["items"] = {"type": "string"}
    fn: dict[str, Any] = {"name": t.name, "parameters": schema}
    if t.description is not None:
        fn["description"] = t.description
    return {"type": "function", "function": fn}


def _convert_tool_choice(
    raw: str | dict[str, Any] | None,
) -> str | dict[str, Any]:
    """Anthropic tool_choice → OpenAI tool_choice."""
    if raw is None:
        return "auto"
    if isinstance(raw, str):
        if raw == "any":
            return "required"
        if raw in {"auto", "none", "required"}:
            return raw
        return "auto"
    # Object form: {"type": "tool", "name": "X"} → {"type":"function","function":{"name":"X"}}
    if isinstance(raw, dict):
        if raw.get("type") == "tool" and "name" in raw:
            return {"type": "function", "function": {"name": raw["name"]}}
        # Already in OpenAI form; pass through.
        return raw
    return "auto"


def _convert_messages(
    anthropic_messages: list[AnthropicMessage],
) -> list[dict[str, Any]]:
    """Translate Anthropic message list → OpenAI chat-completion messages.

    Each Anthropic message becomes *one* OpenAI message. ``tool_result``
    blocks are concatenated into the message's text content prefixed with
    ``"Tool result for <id>:\n<content>\n"``.
    """
    out: list[dict[str, Any]] = []
    for msg in anthropic_messages:
        role = msg.role
        content = msg.content

        if isinstance(content, str):
            text = content
        else:
            # Content is a single block or list of blocks. Normalise to a list.
            blocks = content if isinstance(content, list) else [content]
            text_parts: list[str] = []
            for block in blocks:
                if isinstance(block, AnthropicContentBlockText):
                    text_parts.append(block.text)
                elif isinstance(block, AnthropicContentBlockToolResult):
                    inner = block.content
                    if isinstance(inner, str):
                        rendered = inner
                    elif isinstance(inner, list):
                        rendered = "\n".join(json.dumps(item) for item in inner)
                    elif isinstance(inner, dict):
                        rendered = json.dumps(inner)
                    else:
                        rendered = str(inner)
                    text_parts.append(f"Tool result for {block.tool_use_id}:\n{rendered}\n")
                elif isinstance(block, AnthropicContentBlockToolUse):
                    # Tool use becomes a JSON-encoded text payload — OpenAI is
                    # strict about message boundaries (tool_calls only on
                    # assistant messages), and we keep this implementation simple.
                    text_parts.append(
                        json.dumps(
                            {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }
                        )
                    )
                else:
                    # Image / unknown block types: stringify as JSON.
                    text_parts.append(json.dumps(block.model_dump(mode="json")))
            text = "\n".join(p for p in text_parts if p)

        if not text and role == "user":
            # OpenAI rejects empty user content; substitute a placeholder.
            text = "..."

        out.append({"role": role, "content": text})
    return out


def convert_anthropic_to_openai(req: AnthropicRequest) -> dict[str, Any]:
    """Convert an Anthropic request into a JSON dict ready to send to OpenAI.

    Output is a plain dict (not a Pydantic model) because we serialise via
    ``json.dumps`` and OpenAI rejects unknown keys — so we want to fail
    fast if we ever pass one through.
    """
    is_o_series = req.model.startswith(_O_SERIES_PREFIXES)

    out: dict[str, Any] = {
        "model": req.model,
        "messages": [],
        "stream": req.stream,
    }

    # System message.
    system_text = _flatten_system(req)
    if system_text:
        out["messages"].append({"role": "system", "content": system_text})

    # Conversation messages.
    out["messages"].extend(_convert_messages(req.messages))

    # Tools.
    if req.tools:
        out["tools"] = [_convert_tool(t) for t in req.tools]
        out["tool_choice"] = _convert_tool_choice(req.tool_choice)

    # Tokens: cap at 16384 for o-series (OpenAI rejects higher); rename.
    max_tokens = req.max_tokens
    if max_tokens is None:
        pass  # omit; OpenAI will use its default
    elif max_tokens > _MAX_TOKENS_O_SERIES_CAP and is_o_series:
        max_tokens = _MAX_TOKENS_O_SERIES_CAP

    if max_tokens is not None:
        if is_o_series:
            out["max_completion_tokens"] = max_tokens
        else:
            out["max_tokens"] = max_tokens

    # Temperature: o-series rejects it.
    if not is_o_series and req.temperature is not None:
        out["temperature"] = req.temperature
    if req.top_p is not None and not is_o_series:
        out["top_p"] = req.top_p

    # Streaming options.
    if req.stream:
        out["stream_options"] = {"include_usage": True}

    # Stop sequences.
    if req.stop_sequences:
        out["stop"] = list(req.stop_sequences)

    # User identifier.
    if isinstance(req.metadata, dict) and isinstance(req.metadata.get("user_id"), str):
        out["user"] = req.metadata["user_id"]

    return out


# ---- Response: OpenAI → Anthropic ------------------------------------------


def _build_content_blocks(choice: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert OpenAI ``choices[0].message`` content + tool_calls → Anthropic blocks."""
    msg = choice.get("message") or {}
    blocks: list[dict[str, Any]] = []

    # Text content.
    text = msg.get("content")
    if text:
        blocks.append({"type": "text", "text": text})

    # Tool calls.
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments", "")
        try:
            parsed_args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            parsed_args = {"raw": raw_args}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{id(tc)}"),
                "name": fn.get("name", ""),
                "input": parsed_args,
            }
        )

    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def transform_openai_response_to_anthropic(resp_body: bytes) -> bytes:
    """Translate a non-streaming OpenAI response into an Anthropic-shaped one.

    Returns the JSON-encoded Anthropic response body. The caller wraps it
    in an ``httpx.Response`` and forwards.
    """
    openai = json.loads(resp_body)
    choice = (openai.get("choices") or [{}])[0]
    content_blocks = _build_content_blocks(choice)

    usage_in = 0
    usage_out = 0
    if isinstance(openai.get("usage"), dict):
        usage_in = int(openai["usage"].get("prompt_tokens") or 0)
        usage_out = int(openai["usage"].get("completion_tokens") or 0)

    anthropic = {
        "id": openai.get("id", "msg_unknown"),
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": openai.get("model", "unknown"),
        "stop_reason": _map_finish_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage_in,
            "output_tokens": usage_out,
        },
    }
    return json.dumps(anthropic, ensure_ascii=False).encode()


def _map_finish_reason(reason: str | None) -> str:
    """Map an OpenAI ``finish_reason`` to an Anthropic ``stop_reason``."""
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "refusal",
    }.get(reason or "", "end_turn")


# ---- Streaming: OpenAI SSE → Anthropic SSE --------------------------------


class _AnthropicStreamState:
    """Tracks per-stream state for SSE transformation.

    Each ``OpenAIProvider.transform_openai_stream_to_anthropic`` invocation
    constructs one of these and threads it through the helper functions.
    """

    def __init__(self, message_id: str = "msg_unknown", model: str = "unknown") -> None:
        self.message_id = message_id
        self.model = model
        self.message_started = False
        # For text content we maintain a single content-block index; tool
        # calls each get their own index.
        self.text_block_index: int | None = None
        self.tool_block_indices: dict[int, int] = {}  # openai index → anthropic index
        self.next_index: int = 0
        self.input_json_buffers: dict[int, str] = {}  # openai index → accumulated args
        self.input_json_names: dict[int, str] = {}  # openai index → function name
        self.input_json_ids: dict[int, str] = {}  # openai index → tool call id
        self.usage_received = False
        self.final_usage: dict[str, int] | None = None


def _sse(payload: dict[str, Any] | str) -> bytes:
    """Format one Anthropic SSE record."""
    if isinstance(payload, str):
        payload = {"type": "ping"}  # never used; placeholder
    return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _build_message_start(state: _AnthropicStreamState) -> bytes:
    state.message_started = True
    return _sse(
        {
            "type": "message_start",
            "message": {
                "id": state.message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": state.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
    )


def _build_message_delta(state: _AnthropicStreamState) -> bytes:
    payload: dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
    }
    if state.final_usage:
        payload["usage"] = state.final_usage
    return _sse(payload)


def _build_message_stop() -> bytes:
    return _sse({"type": "message_stop"})


def _build_content_block_stop(index: int) -> bytes:
    return _sse({"type": "content_block_stop", "index": index})


def _build_text_delta(index: int, text: str) -> bytes:
    return _sse(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def _build_input_json_delta(index: int, partial: str) -> bytes:
    return _sse(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial},
        }
    )


def _alloc_block_index(state: _AnthropicStreamState) -> int:
    idx = state.next_index
    state.next_index += 1
    return idx


async def transform_openai_stream_to_anthropic(
    upstream: AsyncIterator[bytes],
    *,
    message_id: str = "msg_stream",
    model: str = "unknown",
) -> AsyncIterator[bytes]:
    """Translate OpenAI SSE chunks into Anthropic SSE chunks.

    ``upstream`` is the raw byte iterator from ``httpx.Response.aiter_bytes``.
    Yields complete SSE frames (events terminated by ``\\n\\n``) so the
    consumer can flush them directly to the client.

    Handles text deltas and streaming tool calls; ignores other deltas
    (role markers, finish_reason-only chunks).
    """
    state = _AnthropicStreamState(message_id=message_id, model=model)
    buffer = ""

    async for chunk in upstream:
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, _, buffer = buffer.partition("\n")
            line = line.rstrip("\r")
            if not line or not line.startswith("data:"):
                continue
            payload_str = line[len("data:") :].strip()
            if payload_str == "[DONE]":
                # Close any open blocks.
                if state.text_block_index is not None:
                    yield _build_content_block_stop(state.text_block_index)
                for idx in state.tool_block_indices.values():
                    yield _build_content_block_stop(idx)
                yield _build_message_delta(state)
                yield _build_message_stop()
                return

            try:
                event = json.loads(payload_str)
            except json.JSONDecodeError:
                logger.debug("ignoring unparseable SSE chunk: %r", payload_str)
                continue

            # Emit message_start on first meaningful event.
            if not state.message_started:
                yield _build_message_start(state)

            # Handle usage (final chunk carries usage when include_usage=True).
            if isinstance(event.get("usage"), dict) and event["usage"]:
                state.final_usage = {
                    "input_tokens": int(event["usage"].get("prompt_tokens") or 0),
                    "output_tokens": int(event["usage"].get("completion_tokens") or 0),
                }
                state.usage_received = True

            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}

                # Text deltas.
                if "content" in delta and delta["content"] is not None:
                    if state.text_block_index is None:
                        idx = _alloc_block_index(state)
                        state.text_block_index = idx
                        yield _sse(
                            {
                                "type": "content_block_start",
                                "index": idx,
                                "content_block": {"type": "text", "text": ""},
                            }
                        )
                    yield _build_text_delta(state.text_block_index, delta["content"])

                # Streaming tool calls.
                for tc in delta.get("tool_calls") or []:
                    oi = tc.get("index", 0)
                    if oi not in state.tool_block_indices:
                        idx = _alloc_block_index(state)
                        state.tool_block_indices[oi] = idx
                        state.input_json_buffers[oi] = ""
                        # First chunk for this tool call: emit start with id+name.
                        tc_id = tc.get("id") or f"toolu_{oi}_{abs(hash((oi, tc.get('id', ''))))}"
                        tc_name = (tc.get("function") or {}).get("name") or ""
                        state.input_json_ids[oi] = tc_id
                        state.input_json_names[oi] = tc_name
                        yield _sse(
                            {
                                "type": "content_block_start",
                                "index": idx,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tc_id,
                                    "name": tc_name,
                                    "input": {},
                                },
                            }
                        )
                    else:
                        idx = state.tool_block_indices[oi]
                    fn_delta = (tc.get("function") or {}).get("arguments")
                    if fn_delta:
                        state.input_json_buffers[oi] += fn_delta
                        yield _build_input_json_delta(idx, fn_delta)


# ---- OpenAIProvider --------------------------------------------------------


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def forward(
        self,
        body: bytes,
        *,
        headers: dict[str, str],
        stream: bool = False,
        target_url: str | None = None,
    ) -> httpx.Response:
        # ``target_url`` overrides the configured ``base_url`` for this
        # single request when set (per-model endpoint routing). We don't
        # append ``/v1/chat/completions`` in that case because the caller
        # passes a complete URL.
        if target_url:
            url = target_url
        else:
            url = urljoin(self._base_url + "/", "v1/chat/completions")

        # Strip Anthropic-specific auth/identity headers; inject OpenAI auth.
        out_headers: dict[str, str] = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk in {"x-api-key", "anthropic-api-key", "anthropic-version"}:
                continue
            out_headers[k] = v
        if self._api_key:
            out_headers["authorization"] = f"Bearer {self._api_key}"
        out_headers["content-type"] = "application/json"
        out_headers["accept-encoding"] = "gzip"

        request = httpx.Request(
            "POST",
            url,
            headers=out_headers,
            content=body,
        )
        return await self._client.send(request, stream=stream)

    async def aclose(self) -> None:
        await self._client.aclose()
