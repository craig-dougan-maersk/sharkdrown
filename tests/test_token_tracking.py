"""Tests for token-count tracking in the embedded proxy.

Verifies that:
  - Non-streaming Anthropic responses: usage.input_tokens / output_tokens
    are extracted and persisted as top-level columns on the request.
  - Streaming Anthropic responses: usage is extracted from the
    ``message_start`` and ``message_delta`` SSE events.
  - The total_tokens column is computed as input + output.
  - Old requests without token info still load (migration compatibility).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _make_storage(tmp_path: Path):
    """Create a StorageService against a fresh sqlite file."""
    import aiosqlite
    from ccproxy.app.db.schema import create_tables
    from ccproxy.app.db.storage import StorageService

    db_path = tmp_path / "test.db"

    async def setup() -> StorageService:
        async with aiosqlite.connect(str(db_path)) as conn:
            await create_tables(conn)
        return StorageService(db_path=db_path)

    return setup, db_path


def test_non_streaming_token_extraction() -> None:
    """Extract usage from a non-streaming Anthropic response body."""
    from ccproxy.app.db.storage import StorageService
    from ccproxy.app.models.storage import RequestLog, ResponseLog
    from datetime import UTC, datetime

    setup, db_path = _make_storage(Path(tempfile.mkdtemp()))
    storage = asyncio.run(setup())

    async def go() -> None:
        await storage.connect()
        req_log = RequestLog(
            id="req1",
            timestamp=datetime.now(UTC),
            method="POST",
            endpoint="/v1/messages",
            original_model="claude-test",
            routed_model="claude-test",
        )
        await storage.save_request(req_log)
        # Anthropic-shaped response body
        body = json.dumps({
            "id": "msg_test",
            "type": "message",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
            },
            "content": [{"type": "text", "text": "Hello"}],
        })
        response = ResponseLog(
            status_code=200,
            headers={"content-type": "application/json"},
            body_text=body,
            is_streaming=False,
        )
        await storage.update_request_with_response("req1", response)
        # Re-read and verify token columns
        updated = await storage.get_request_by_id("req1")
        assert updated is not None
        assert updated.input_tokens == 100, f"got {updated.input_tokens}"
        assert updated.output_tokens == 50, f"got {updated.output_tokens}"
        assert updated.total_tokens == 150, f"got {updated.total_tokens}"
        await storage.close()

    asyncio.run(go())
    print("✓ non-streaming: input_tokens=100, output_tokens=50, total=150")


def test_streaming_token_extraction() -> None:
    """Extract usage from SSE message_start + message_delta events."""
    from ccproxy.app.db.storage import StorageService
    from ccproxy.app.models.storage import RequestLog, ResponseLog
    from datetime import UTC, datetime

    setup, db_path = _make_storage(Path(tempfile.mkdtemp()))
    storage = asyncio.run(setup())

    async def go() -> None:
        await storage.connect()
        req_log = RequestLog(
            id="req2",
            timestamp=datetime.now(UTC),
            method="POST",
            endpoint="/v1/messages",
            original_model="claude-test",
            routed_model="claude-test",
        )
        await storage.save_request(req_log)
        chunks = [
            "event: message_start\ndata: " + json.dumps({
                "type": "message_start",
                "message": {"usage": {"input_tokens": 200}},
            }),
            "event: message_delta\ndata: " + json.dumps({
                "type": "message_delta",
                "usage": {"output_tokens": 75},
            }),
        ]
        response = ResponseLog(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            streaming_chunks=chunks,
            is_streaming=True,
            body_text="\n\n".join(chunks),
        )
        await storage.update_request_with_response("req2", response)
        updated = await storage.get_request_by_id("req2")
        assert updated is not None
        assert updated.input_tokens == 200, f"got input={updated.input_tokens}"
        assert updated.output_tokens == 75, f"got output={updated.output_tokens}"
        assert updated.total_tokens == 275, f"got total={updated.total_tokens}"
        await storage.close()

    asyncio.run(go())
    print("✓ streaming: input_tokens=200 (from message_start), output_tokens=75 (from message_delta)")


def test_missing_usage_does_not_crash() -> None:
    """Responses without usage info should leave token columns None."""
    from ccproxy.app.db.storage import StorageService
    from ccproxy.app.models.storage import RequestLog, ResponseLog
    from datetime import UTC, datetime

    setup, db_path = _make_storage(Path(tempfile.mkdtemp()))
    storage = asyncio.run(setup())

    async def go() -> None:
        await storage.connect()
        req_log = RequestLog(
            id="req3",
            timestamp=datetime.now(UTC),
            method="POST",
            endpoint="/v1/messages",
        )
        await storage.save_request(req_log)
        # 500 error — no usage block
        response = ResponseLog(
            status_code=500,
            headers={},
            body_text=json.dumps({"type": "error", "error": {"type": "api_error"}}),
            is_streaming=False,
        )
        await storage.update_request_with_response("req3", response)
        updated = await storage.get_request_by_id("req3")
        assert updated is not None
        assert updated.input_tokens is None
        assert updated.output_tokens is None
        assert updated.total_tokens is None
        await storage.close()

    asyncio.run(go())
    print("✓ missing usage: token columns stay NULL (no crash)")


def test_template_renders_token_badge() -> None:
    """Sanity-check the Jinja template renders the token badge."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = (
        _PROJECT_ROOT
        / "src"
        / "ccproxy"
        / "app"
        / "templates"
    )
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    tpl = env.get_template("dashboard/request_list.html")
    macros_src = (template_dir / "macros.html").read_text()
    # We don't have a real RequestLog; just render with a mock dict.
    # Render a single row with tokens and check the output.
    ctx = {
        "rows": [
            {
                "id": "req1",
                "method": "POST",
                "original_model": "claude-test",
                "routed_model": "claude-test",
                "request_path": "passthrough",
                "status_code": 200,
                "timestamp": __import__("datetime").datetime.now(),
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            }
        ],
        "has_more": False,
        "next_page": 2,
        "model_filter": None,
    }
    rendered = tpl.render(**ctx)
    assert "150 tok" in rendered, f"missing token count: {rendered[:200]}"
    assert "100" in rendered
    assert "50" in rendered
    print("✓ template renders '150 tok' badge with input/output breakdown")


def main() -> None:
    tests = [
        test_non_streaming_token_extraction,
        test_streaming_token_extraction,
        test_missing_usage_does_not_crash,
        test_template_renders_token_badge,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    if failed:
        print(f"\n❌ {failed} test(s) failed")
        sys.exit(1)
    print("\n✅ Token tracking tests passed!")


if __name__ == "__main__":
    main()