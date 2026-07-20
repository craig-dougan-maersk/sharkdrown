"""Anthropic Messages API request/response types.

These mirror the public shapes documented at https://docs.anthropic.com/en/api/messages.
Unknown fields on incoming requests are accepted (``extra="allow"``) so the proxy
doesn't drop fields Claude Code adds later; they round-trip through the
forwarder unmodified.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---- Roles and stop reasons -------------------------------------------------

AnthropicRole = Literal["user", "assistant"]

AnthropicStopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "refusal",
]


# ---- System messages --------------------------------------------------------


class AnthropicSystemMessage(BaseModel):
    """Anthropic accepts a list of system messages; each is just a string."""

    model_config = ConfigDict(extra="allow")

    type: Literal["text"] = "text"
    text: str
    cache_control: dict[str, Any] | None = None


# ---- Content blocks (discriminated by `type`) -------------------------------


class AnthropicContentBlockText(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["text"] = "text"
    text: str


class AnthropicContentBlockImage(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["image"] = "image"
    source: dict[str, Any]


class AnthropicContentBlockToolUse(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class AnthropicContentBlockToolResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[dict[str, Any]] | dict[str, Any]
    is_error: bool | None = None


class AnthropicContentBlockThinking(BaseModel):
    """Anthropic's extended-thinking content block.

    Carries the model's internal reasoning alongside its response. The
    ``signature`` field is a cryptographic signature the API uses to
    verify the thinking wasn't tampered with across a multi-turn
    conversation — the proxy must pass it through verbatim, dropping it
    would invalidate the next turn.
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str | None = None


class AnthropicContentBlockRedactedThinking(BaseModel):
    """Redacted variant of the thinking block.

    Anthropic redacts reasoning that contains sensitive content; the proxy
    must still forward ``data`` so the upstream can stitch the
    conversation back together.
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


class AnthropicContentBlockUnknown(BaseModel):
    """Catch-all for content blocks with a ``type`` we don't model yet.

    Anthropic's API evolves — extended thinking added ``thinking`` and
    ``redacted_thinking`` mid-2025, more block types will come. Modelling
    every new variant as a typed class would mean a code change every
    time the API ships one, with no real benefit on the proxy side: we
    just forward whatever the client sends.

    This variant accepts any non-empty ``type`` string and preserves all
    extra fields. Behaviour in the proxy:

      - Anthropic→Anthropic forwarding: round-trips verbatim.
      - Anthropic→OpenAI conversion: silently dropped (no equivalent).
      - Dashboard rendering: falls through to a generic JSON view.

    It MUST be the last variant in the ``AnthropicContentBlock`` union
    and the union MUST use ``union_mode="left_to_right"`` — otherwise
    this catch-all would match known variants too because both sides
    have ``extra="allow"``.
    """

    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1)


AnthropicContentBlock = Annotated[
    AnthropicContentBlockText
    | AnthropicContentBlockImage
    | AnthropicContentBlockToolUse
    | AnthropicContentBlockToolResult
    | AnthropicContentBlockThinking
    | AnthropicContentBlockRedactedThinking
    | AnthropicContentBlockUnknown,
    Field(union_mode="left_to_right"),
]


# ---- Tools ------------------------------------------------------------------


class AnthropicTool(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


# ---- Messages ---------------------------------------------------------------


class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: AnthropicRole
    content: str | list[AnthropicContentBlock] | AnthropicContentBlock


# ---- Request ----------------------------------------------------------------


class AnthropicRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[AnthropicMessage]
    max_tokens: int = 1024
    system: str | list[AnthropicSystemMessage] | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: str | dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _lift_system_messages_to_top_level(cls, data: Any) -> Any:
        """Lift ``role='system'`` messages out of ``messages[]`` into the
        top-level ``system`` field.

        Some Claude Code configurations (notably ``?beta=true`` requests) embed
        the system prompt inside ``messages[]`` rather than at the top level
        the Anthropic Messages API expects. By lifting them here, every code
        path downstream (proxy forward, OpenAI conversion, subagent routing)
        can rely on ``req.system`` being the single source of truth, and the
        field-level validation of ``AnthropicMessage.role`` doesn't need to
        be loosened to accept ``"system"``.
        """
        if not isinstance(data, dict):
            return data
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return data
        system_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
        if not system_msgs:
            return data

        new_blocks: list[dict[str, Any]] = []
        for m in system_msgs:
            for text in _extract_text_from_content(m.get("content")):
                new_blocks.append({"type": "text", "text": text})

        # Shallow-copy so we never mutate the caller's dict.
        data = dict(data)
        existing = data.get("system")
        if existing is None:
            data["system"] = new_blocks
        elif isinstance(existing, str):
            data["system"] = [{"type": "text", "text": existing}, *new_blocks]
        elif isinstance(existing, list):
            data["system"] = [*existing, *new_blocks]
        # If ``existing`` is something unexpected, leave it alone — field
        # validation will raise the appropriate error.

        data["messages"] = [
            m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")
        ]
        return data


# ---- Response ---------------------------------------------------------------


class AnthropicUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


class AnthropicResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[AnthropicContentBlock]
    model: str
    stop_reason: AnthropicStopReason | None = None
    stop_sequence: str | None = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)


# ---- Streaming events -------------------------------------------------------


StreamingEventType = Literal[
    "message_start",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
    "message_delta",
    "message_stop",
    "ping",
    "error",
]


class StreamingEvent(BaseModel):
    """One SSE event in the Anthropic streaming protocol."""

    model_config = ConfigDict(extra="allow")

    type: StreamingEventType
    # The remaining fields are intentionally loose: every event variant has
    # different content, and the proxy forwards them verbatim.
    message: dict[str, Any] | None = None
    index: int | None = None
    content_block: dict[str, Any] | None = None
    delta: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    received_at: datetime | None = None  # internal-use only


# ---- Helpers ----------------------------------------------------------------


def _extract_text_from_content(content: Any) -> list[str]:
    """Pull plain-text strings out of a message ``content`` field.

    Accepts the three shapes the Anthropic spec allows: a bare ``str``, a
    single content block, or a list of content blocks. Only ``text`` blocks
    contribute — images / tool_use / tool_result are skipped.
    """
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    out.append(text)
        return out
    if isinstance(content, dict) and content.get("type") == "text":
        text = content.get("text", "")
        if isinstance(text, str):
            return [text]
    return []
