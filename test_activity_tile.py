"""Tests for the status-bar activity tile stack.

Covers:
  - StorageService.count_total / count_failed / count_no_tokens
  - StorageService.last_request_info timestamp + model
  - /api/stats endpoint returns the expected JSON shape
  - ActivityTile QLabel constructs and renders idle text without a URL
  - ActivityTile.set_base_url/stop transitions correctly
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


async def _build_seeded_storage():
    """Build a fresh StorageService and seed it via the realistic flow."""
    from ccproxy.app.db.storage import StorageService
    from ccproxy.app.models.storage import RequestLog, ResponseLog

    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "activity.db"
    storage = StorageService(db_path=db_path)
    await storage.connect()

    base = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    seeds = [
        # (id, status, total_tokens, model, delta_seconds)
        ("r1", 200, 100, "claude-opus", 0),
        ("r2", 500, 10, "claude-sonnet", 1),
        ("r3", 200, None, "claude-haiku", 2),       # no tokens
        ("r4", 404, None, "claude-haiku", 3),       # failed + no tokens
        ("r5", 200, 0, "claude-opus", 4),           # zero tokens (empty)
    ]
    for rid, status, total, model, dt in seeds:
        ts = base.replace(second=base.second + dt)
        req = RequestLog(
            id=rid,
            timestamp=ts,
            method="POST",
            endpoint="/v1/messages",
            original_model=model,
            routed_model=model,
            routed_provider="anthropic",
            request_path="passthrough",
        )
        await storage.save_request(req)
        if total is None:
            body = json.dumps({"id": f"msg-{rid}"})
        else:
            body = json.dumps(
                {
                    "id": f"msg-{rid}",
                    "usage": {
                        "input_tokens": total // 2,
                        "output_tokens": total - total // 2,
                    },
                }
            )
        resp = ResponseLog(
            status_code=status,
            headers={"content-type": "application/json"},
            body_text=body,
            is_streaming=False,
        )
        await storage.update_request_with_response(rid, resp)
    return storage


def test_storage_count_total() -> None:
    async def go() -> None:
        storage = await _build_seeded_storage()
        total = await storage.count_total()
        assert total == 5, f"expected 5 rows, got {total}"
        await storage.close()

    asyncio.run(go())
    print("✓ count_total: 5 rows")


def test_storage_count_failed() -> None:
    async def go() -> None:
        storage = await _build_seeded_storage()
        failed = await storage.count_failed()
        # r2=500 + r4=404 → 2
        assert failed == 2, f"expected 2 failed rows, got {failed}"
        await storage.close()

    asyncio.run(go())
    print("✓ count_failed: only rows with status ≥ 400")


def test_storage_count_no_tokens() -> None:
    async def go() -> None:
        storage = await _build_seeded_storage()
        empty = await storage.count_no_tokens()
        # r3=null, r4=null, r5=0 → 3 (NULL or zero)
        assert empty == 3, f"expected 3 empty-token rows, got {empty}"
        await storage.close()

    asyncio.run(go())
    print("✓ count_no_tokens: NULL or 0 token rows counted")


def test_storage_last_request_info() -> None:
    async def go() -> None:
        storage = await _build_seeded_storage()
        ts_iso, model = await storage.last_request_info()
        # r5 is the most recent (4 seconds after r1)
        assert model == "claude-opus", f"expected claude-opus, got {model}"
        assert ts_iso is not None
        # ISO string contains the date and time of r5
        assert "2026-07-19T12:00:04" in ts_iso, f"unexpected timestamp {ts_iso}"
        await storage.close()

    asyncio.run(go())
    print("✓ last_request_info: most-recent row by timestamp")


def test_storage_stats_on_empty_db() -> None:
    async def go() -> None:
        from ccproxy.app.db.storage import StorageService

        storage = StorageService(db_path=Path(tempfile.mkdtemp()) / "empty.db")
        await storage.connect()
        assert await storage.count_total() == 0
        assert await storage.count_failed() == 0
        assert await storage.count_no_tokens() == 0
        ts, model = await storage.last_request_info()
        assert ts is None and model is None
        await storage.close()

    asyncio.run(go())
    print("✓ all counters return zero/None on an empty table")


def test_api_stats_endpoint_returns_expected_shape() -> None:
    """Integration: spin up the FastAPI app via ASGI transport and call /api/stats.

    Seed and request both use ``app.state.storage`` (the create_app
    factory attaches it eagerly) so there's no race between two
    StorageService instances pointing at the same file.
    """
    import httpx

    async def go() -> None:
        from ccproxy.app.config import Settings
        from ccproxy.app.main import create_app
        from ccproxy.app.models.storage import RequestLog, ResponseLog

        tmp = Path(tempfile.mkdtemp())
        db_path = tmp / "stats.db"
        app = create_app(settings=Settings(), storage_path=str(db_path))
        # Open the storage create_app attached, then seed through it.
        storage = app.state.storage
        await storage.connect()

        base = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
        seeds = [
            ("r1", 200, 100, "claude-opus", 0),
            ("r2", 500, 10, "claude-sonnet", 1),
            ("r3", 200, None, "claude-haiku", 2),
            ("r4", 404, None, "claude-haiku", 3),
            ("r5", 200, 0, "claude-opus", 4),
        ]
        for rid, status, total, model, dt in seeds:
            ts = base.replace(second=base.second + dt)
            req = RequestLog(
                id=rid, timestamp=ts, method="POST", endpoint="/v1/messages",
                original_model=model, routed_model=model,
                routed_provider="anthropic", request_path="passthrough",
            )
            await storage.save_request(req)
            if total is None:
                body = json.dumps({"id": f"msg-{rid}"})
            else:
                body = json.dumps({
                    "id": f"msg-{rid}",
                    "usage": {
                        "input_tokens": total // 2,
                        "output_tokens": total - total // 2,
                    },
                })
            await storage.update_request_with_response(
                rid,
                ResponseLog(
                    status_code=status,
                    headers={"content-type": "application/json"},
                    body_text=body,
                    is_streaming=False,
                ),
            )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/stats")
        assert r.status_code == 200, f"got status {r.status_code}"
        body = r.json()
        assert set(body.keys()) >= {"total", "failed", "empty", "last"}, body
        assert body["total"] == 5, body
        assert body["failed"] == 2, body
        assert body["empty"] == 3, body
        assert body["last"]["model"] == "claude-opus", body
        await storage.close()

    asyncio.run(go())
    print("✓ /api/stats endpoint returns the expected JSON shape")


def _qapp():
    """Build (or reuse) a headless QApplication for the tile tests."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_activity_tile_idle_state() -> None:
    """Without a base_url the tile renders the idle text."""
    _qapp()
    from src.proxy.activity_tile import ActivityTile, _IDLE_TEXT

    tile = ActivityTile()
    assert tile.text() == _IDLE_TEXT, f"expected idle text, got {tile.text()!r}"
    # Stop is safe to call without a URL.
    tile.stop()
    print("✓ ActivityTile starts in idle state, stop() is a no-op when not running")


def test_activity_tile_offline_state() -> None:
    """Setting base_url to an unreachable host renders the offline marker."""
    _qapp()
    from src.proxy.activity_tile import ActivityTile

    tile = ActivityTile()
    # 127.0.0.1:1 has no service listening — set_base_url seeds from /api/stats
    # which will fail and render the offline marker.
    tile.set_base_url("http://127.0.0.1:1")
    assert "offline" in tile.text().lower(), f"expected offline marker, got {tile.text()!r}"
    tile.stop()
    print("✓ ActivityTile renders offline marker when the proxy is unreachable")


def test_activity_tile_event_driven_updates() -> None:
    """Push events from the EventBusBridge update counters correctly."""
    _qapp()
    from src.proxy.activity_tile import ActivityTile

    tile = ActivityTile()
    # Simulate the bridge pushing events directly (no HTTP needed)
    tile.on_request_created({"type": "request_created", "id": "r1"})
    assert "1 req" in tile.text()

    # Response arrives with tokens — empty count decreases, cost accumulates
    tile.on_response_complete({
        "type": "response_complete", "id": "r1", "model": "claude-sonnet-4-20250514",
        "status_code": 200, "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500,
    })
    assert "$" in tile.text(), f"expected cost in tile, got {tile.text()!r}"
    assert tile._total_cost > 0

    # A second request that fails
    tile.on_request_created({"type": "request_created", "id": "r2"})
    tile.on_response_complete({
        "type": "response_complete", "id": "r2", "model": "claude-sonnet-4-20250514",
        "status_code": 500, "input_tokens": 500, "output_tokens": 200, "total_tokens": 700,
    })
    assert "2 req" in tile.text()
    assert "1 err" in tile.text()

    tile.stop()
    print("✓ ActivityTile event-driven updates track counts and cost correctly")


def main() -> None:
    tests = [
        test_storage_count_total,
        test_storage_count_failed,
        test_storage_count_no_tokens,
        test_storage_last_request_info,
        test_storage_stats_on_empty_db,
        test_api_stats_endpoint_returns_expected_shape,
        test_activity_tile_idle_state,
        test_activity_tile_offline_state,
        test_activity_tile_event_driven_updates,
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
    print("\n✅ Activity tile tests passed!")


if __name__ == "__main__":
    main()
