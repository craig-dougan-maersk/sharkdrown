"""Integration tests for the Anthropic ↔ OpenAI protocol conversion.

Verifies:
  - convert_anthropic_to_openai produces a valid OpenAI request shape
  - transform_openai_response_to_anthropic produces a valid Anthropic response shape
  - End-to-end: Anthropic request → proxy → mock OpenAI → Anthropic response
  - Tool call round-trip (Anthropic tools → OpenAI functions → Anthropic tool_use)
  - O-series model constraints (max_completion_tokens cap, no temperature)
  - Streaming SSE translation (OpenAI → Anthropic event shapes)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from ccproxy.app.models.anthropic import (
    AnthropicContentBlockText,
    AnthropicContentBlockToolResult,
    AnthropicContentBlockToolUse,
    AnthropicMessage,
    AnthropicRequest,
    AnthropicSystemMessage,
    AnthropicTool,
)
from ccproxy.app.providers.openai import (
    convert_anthropic_to_openai,
    transform_openai_response_to_anthropic,
    transform_openai_stream_to_anthropic,
)


def _basic_request(model: str = "gpt-4o", **kwargs) -> AnthropicRequest:
    """Build a minimal AnthropicRequest for testing."""
    defaults = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            AnthropicMessage(role="user", content="Hello, world!"),
        ],
        "stream": False,
    }
    defaults.update(kwargs)
    return AnthropicRequest(**defaults)


# ---- Request conversion tests -----------------------------------------------


def test_basic_request_conversion() -> None:
    """A simple user message converts to valid OpenAI shape."""
    req = _basic_request(
        system=[AnthropicSystemMessage(type="text", text="You are helpful.")],
    )
    result = convert_anthropic_to_openai(req)

    assert result["model"] == "gpt-4o"
    assert result["stream"] is False
    assert result["max_tokens"] == 4096
    assert len(result["messages"]) == 2
    assert result["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert result["messages"][1] == {"role": "user", "content": "Hello, world!"}
    assert "tools" not in result
    print("  convert_anthropic_to_openai: basic request shape")


def test_multi_system_messages_concatenated() -> None:
    """Multiple system blocks are joined with double-newlines."""
    req = _basic_request(
        system=[
            AnthropicSystemMessage(type="text", text="First system."),
            AnthropicSystemMessage(type="text", text="Second system."),
        ],
    )
    result = convert_anthropic_to_openai(req)
    assert result["messages"][0]["content"] == "First system.\n\nSecond system."
    print("  convert_anthropic_to_openai: multi-system concatenation")


def test_tool_conversion() -> None:
    """Anthropic tools become OpenAI function-calling tools."""
    req = _basic_request(
        tools=[
            AnthropicTool(
                name="get_weather",
                description="Get weather for a city",
                input_schema={
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                    },
                    "required": ["city"],
                },
            ),
        ],
        tool_choice="auto",
    )
    result = convert_anthropic_to_openai(req)

    assert "tools" in result
    assert len(result["tools"]) == 1
    tool = result["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "get_weather"
    assert tool["function"]["description"] == "Get weather for a city"
    assert tool["function"]["parameters"]["properties"]["city"]["type"] == "string"
    assert result["tool_choice"] == "auto"
    print("  convert_anthropic_to_openai: tool definitions")


def test_tool_choice_any_becomes_required() -> None:
    """Anthropic tool_choice='any' maps to OpenAI 'required'."""
    req = _basic_request(
        tools=[AnthropicTool(name="x", input_schema={"type": "object"})],
        tool_choice="any",
    )
    result = convert_anthropic_to_openai(req)
    assert result["tool_choice"] == "required"
    print("  convert_anthropic_to_openai: tool_choice 'any' → 'required'")


def test_o_series_constraints() -> None:
    """O-series models: max_tokens capped, no temperature, uses max_completion_tokens."""
    req = _basic_request(model="o1-preview", max_tokens=100_000, temperature=0.7)
    result = convert_anthropic_to_openai(req)

    assert "max_tokens" not in result
    assert result["max_completion_tokens"] == 16384
    assert "temperature" not in result
    print("  convert_anthropic_to_openai: o-series constraints enforced")


def test_o3_model_constraints() -> None:
    """o3 models also get the o-series treatment."""
    req = _basic_request(model="o3-mini", max_tokens=8000)
    result = convert_anthropic_to_openai(req)

    assert "max_tokens" not in result
    assert result["max_completion_tokens"] == 8000  # under cap, unchanged
    assert "temperature" not in result
    print("  convert_anthropic_to_openai: o3 model constraints")


def test_tool_result_message_conversion() -> None:
    """Tool results become role:'tool' messages with tool_call_id (OpenAI format)."""
    req = _basic_request(
        messages=[
            AnthropicMessage(
                role="user",
                content=[
                    AnthropicContentBlockToolResult(
                        type="tool_result",
                        tool_use_id="toolu_123",
                        content="The weather is sunny, 22C",
                    ),
                ],
            ),
        ],
    )
    result = convert_anthropic_to_openai(req)
    tool_msg = result["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "toolu_123"
    assert "sunny" in tool_msg["content"]
    print("  convert_anthropic_to_openai: tool_result blocks → role:tool")


def test_stop_sequences_become_stop() -> None:
    """Anthropic stop_sequences maps to OpenAI stop."""
    req = _basic_request(stop_sequences=["END", "STOP"])
    result = convert_anthropic_to_openai(req)
    assert result["stop"] == ["END", "STOP"]
    print("  convert_anthropic_to_openai: stop_sequences → stop")


def test_streaming_enables_usage() -> None:
    """When stream=True, stream_options.include_usage is set."""
    req = _basic_request(stream=True)
    result = convert_anthropic_to_openai(req)
    assert result["stream"] is True
    assert result["stream_options"] == {"include_usage": True}
    print("  convert_anthropic_to_openai: streaming options")


# ---- Response conversion tests -----------------------------------------------


def test_basic_response_conversion() -> None:
    """A simple OpenAI response converts to valid Anthropic shape."""
    openai_resp = json.dumps({
        "id": "chatcmpl-abc123",
        "model": "gpt-4o-2024-08-06",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello! How can I help?"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }).encode()

    result = json.loads(transform_openai_response_to_anthropic(openai_resp))

    assert result["id"] == "chatcmpl-abc123"
    assert result["type"] == "message"
    assert result["role"] == "assistant"
    assert result["model"] == "gpt-4o-2024-08-06"
    assert result["stop_reason"] == "end_turn"
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 8
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "Hello! How can I help?"
    print("  transform_openai_response_to_anthropic: basic response")


def test_tool_call_response_conversion() -> None:
    """OpenAI tool_calls become Anthropic tool_use content blocks."""
    openai_resp = json.dumps({
        "id": "chatcmpl-tools",
        "model": "gpt-4o",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "London"}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
    }).encode()

    result = json.loads(transform_openai_response_to_anthropic(openai_resp))

    assert result["stop_reason"] == "tool_use"
    assert len(result["content"]) == 1
    block = result["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_abc"
    assert block["name"] == "get_weather"
    assert block["input"] == {"city": "London"}
    print("  transform_openai_response_to_anthropic: tool calls")


def test_finish_reason_mappings() -> None:
    """All OpenAI finish_reasons map to correct Anthropic stop_reasons."""
    mappings = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "refusal",
    }
    for openai_reason, expected_anthropic in mappings.items():
        resp = json.dumps({
            "id": "chatcmpl-x",
            "model": "gpt-4o",
            "choices": [{"message": {"content": "x"}, "finish_reason": openai_reason}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        result = json.loads(transform_openai_response_to_anthropic(resp))
        assert result["stop_reason"] == expected_anthropic, (
            f"{openai_reason} → expected {expected_anthropic}, got {result['stop_reason']}"
        )
    print("  transform_openai_response_to_anthropic: finish_reason mapping")


# ---- Streaming SSE conversion ------------------------------------------------


def test_streaming_text_conversion() -> None:
    """OpenAI SSE text chunks become Anthropic content_block_delta events."""
    chunks = [
        'data: {"id":"chatcmpl-s","model":"gpt-4o","choices":[{"delta":{"content":"Hello"}}]}\n\n',
        'data: {"id":"chatcmpl-s","model":"gpt-4o","choices":[{"delta":{"content":" world"}}]}\n\n',
        'data: {"id":"chatcmpl-s","model":"gpt-4o","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]

    async def _stream():
        for c in chunks:
            yield c.encode()

    async def go():
        events = []
        async for frame in transform_openai_stream_to_anthropic(
            _stream(), message_id="msg_test", model="gpt-4o"
        ):
            decoded = frame.decode()
            for line in decoded.strip().split("\n"):
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))

        # First event: message_start
        assert events[0]["type"] == "message_start"
        assert events[0]["message"]["model"] == "gpt-4o"

        # Next: content_block_start
        assert events[1]["type"] == "content_block_start"
        assert events[1]["content_block"]["type"] == "text"

        # Text deltas
        text_deltas = [e for e in events if e.get("type") == "content_block_delta"]
        assert len(text_deltas) == 2
        assert text_deltas[0]["delta"]["text"] == "Hello"
        assert text_deltas[1]["delta"]["text"] == " world"

        # Final events: content_block_stop, message_delta, message_stop
        assert events[-3]["type"] == "content_block_stop"
        assert events[-2]["type"] == "message_delta"
        assert events[-2]["delta"]["stop_reason"] == "end_turn"
        assert events[-1]["type"] == "message_stop"

    asyncio.run(go())
    print("  transform_openai_stream_to_anthropic: text streaming")


def test_streaming_tool_call_conversion() -> None:
    """OpenAI streaming tool_calls become Anthropic tool_use blocks."""
    chunks = [
        'data: {"id":"chatcmpl-t","model":"gpt-4o","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":""}}]}}]}\n\n',
        'data: {"id":"chatcmpl-t","model":"gpt-4o","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\""}}]}}]}\n\n',
        'data: {"id":"chatcmpl-t","model":"gpt-4o","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":": \\"foo.txt\\"}"}}]}}]}\n\n',
        'data: {"id":"chatcmpl-t","model":"gpt-4o","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}\n\n',
        "data: [DONE]\n\n",
    ]

    async def _stream():
        for c in chunks:
            yield c.encode()

    async def go():
        events = []
        async for frame in transform_openai_stream_to_anthropic(
            _stream(), message_id="msg_tool", model="gpt-4o"
        ):
            decoded = frame.decode()
            for line in decoded.strip().split("\n"):
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))

        # message_start
        assert events[0]["type"] == "message_start"

        # content_block_start for tool_use
        tool_starts = [e for e in events if e.get("type") == "content_block_start"]
        assert len(tool_starts) == 1
        assert tool_starts[0]["content_block"]["type"] == "tool_use"
        assert tool_starts[0]["content_block"]["name"] == "read_file"
        assert tool_starts[0]["content_block"]["id"] == "call_1"

        # input_json_delta events
        json_deltas = [
            e for e in events
            if e.get("type") == "content_block_delta"
            and e.get("delta", {}).get("type") == "input_json_delta"
        ]
        assert len(json_deltas) == 2
        full_json = "".join(d["delta"]["partial_json"] for d in json_deltas)
        assert json.loads(full_json) == {"path": "foo.txt"}

    asyncio.run(go())
    print("  transform_openai_stream_to_anthropic: tool call streaming")


# ---- End-to-end integration (ASGI transport) ---------------------------------


def test_e2e_openai_routing_via_proxy() -> None:
    """Full pipeline: Anthropic request → proxy route → mock OpenAI → Anthropic response.

    Uses httpx's ASGI transport to send a real HTTP request through the FastAPI
    app with a mock OpenAI backend (httpx mock transport).
    """
    import tempfile
    import httpx

    async def go():
        from ccproxy.app.config import Settings
        from ccproxy.app.main import create_app
        from ccproxy.app.router.subagent import SubagentRouter
        from ccproxy.app.live.bus import EventBus

        # Mock OpenAI backend that returns a canned response.
        openai_response = json.dumps({
            "id": "chatcmpl-mock",
            "model": "gpt-4o-2024-08-06",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "I'm GPT-4o!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20},
        })

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["model"] == "gpt-4o"
            assert body["messages"][-1]["content"] == "What model are you?"
            assert "authorization" in request.headers
            return httpx.Response(200, json=json.loads(openai_response))

        mock_transport = httpx.MockTransport(mock_handler)
        mock_client = httpx.AsyncClient(transport=mock_transport)

        tmp = Path(tempfile.mkdtemp())
        db_path = tmp / "e2e.db"

        settings = Settings()

        # Build a mock OpenAI provider with our mock transport.
        from ccproxy.app.providers.openai import OpenAIProvider
        mock_provider = OpenAIProvider(
            base_url="https://api.openai.com",
            api_key="sk-test-key",
            client=mock_client,
        )
        providers = {"openai": mock_provider}

        app = create_app(settings=settings, providers=providers, storage_path=str(db_path))

        # Wire up state that normally happens in the lifespan (the ASGI
        # transport doesn't trigger lifespan events by default).
        await app.state.storage.connect()
        app.state.router = SubagentRouter(settings, providers)
        app.state.event_bus = EventBus()
        app.state.storage._event_bus = app.state.event_bus

        # Send an Anthropic-shaped request for a GPT model.
        anthropic_request = {
            "model": "gpt-4o",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "What model are you?"}],
            "stream": False,
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(
                "/v1/messages",
                json=anthropic_request,
                headers={
                    "x-api-key": "test-key",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

        assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
        body = resp.json()

        # Verify Anthropic response shape.
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["model"] == "gpt-4o-2024-08-06"
        assert body["stop_reason"] == "end_turn"
        assert body["usage"]["input_tokens"] == 15
        assert body["usage"]["output_tokens"] == 5
        assert body["content"][0]["type"] == "text"
        assert body["content"][0]["text"] == "I'm GPT-4o!"

        await mock_client.aclose()
        await app.state.storage.close()

    asyncio.run(go())
    print("  e2e: Anthropic → proxy → OpenAI → Anthropic round-trip")


def main() -> None:
    tests = [
        test_basic_request_conversion,
        test_multi_system_messages_concatenated,
        test_tool_conversion,
        test_tool_choice_any_becomes_required,
        test_o_series_constraints,
        test_o3_model_constraints,
        test_tool_result_message_conversion,
        test_stop_sequences_become_stop,
        test_streaming_enables_usage,
        test_basic_response_conversion,
        test_tool_call_response_conversion,
        test_finish_reason_mappings,
        test_streaming_text_conversion,
        test_streaming_tool_call_conversion,
        test_e2e_openai_routing_via_proxy,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"  {t.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    if failed:
        print(f"\n  {failed} test(s) failed")
        sys.exit(1)
    print("\n  OpenAI conversion tests passed!")


if __name__ == "__main__":
    main()
