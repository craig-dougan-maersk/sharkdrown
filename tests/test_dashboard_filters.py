"""Tests for the proxy dashboard's filter toggles.

Covers:
  - storage.get_requests() drops 4xx/5xx when ``hide_failed=True``
  - storage.get_requests() drops NULL/0 token rows when ``hide_no_tokens=True``
  - filters compose with the existing ``model_filter`` (both AND)
  - dashboard endpoints parse checkbox-style query params with the
    correct default-on semantics
  - the index template wires filter state into Alpine + URL builder
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _make_seed_specs():
    """Spec rows: (id, status_code, total_tokens, model)."""
    return [
        # (id,                status, total,   model)
        ("r-ok",              200,    150,    "claude-opus"),    # healthy, with tokens
        ("r-500",             500,    10,     "claude-sonnet"),  # failed but has tokens
        ("r-404",             404,    None,   "claude-haiku"),   # failed and empty
        ("r-notok",           200,    None,   "claude-haiku"),   # 200 but no tokens
        ("r-zero",            200,    0,      "claude-opus"),    # zero tokens
    ]


async def _seed_and_get(**filters):
    """Create a fresh DB, seed it via the realistic two-step flow, then query.

    Uses ``save_request`` + ``update_request_with_response`` so the
    columns that filter logic depends on (``status_code``,
    ``input_tokens``/``output_tokens``/``total_tokens``) actually get
    populated — unlike seeding a single ``save_request`` call which
    only writes the request-side columns.
    """
    from ccproxy.app.db.storage import StorageService
    from ccproxy.app.models.storage import RequestLog, ResponseLog

    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "filter.db"
    storage = StorageService(db_path=db_path)
    await storage.connect()

    base_ts = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    for rid, status, total, model in _make_seed_specs():
        req = RequestLog(
            id=rid,
            timestamp=base_ts,
            method="POST",
            endpoint="/v1/messages",
            original_model=model,
            routed_model=model,
            routed_provider="anthropic",
            request_path="passthrough",
        )
        await storage.save_request(req)

        # Always persist a response row so status_code is filled in,
        # even when total_tokens is None (so the no-tokens row looks
        # distinct from a never-completed row).
        body = json.dumps({"id": f"msg-{rid}"})
        if total is not None:
            body = json.dumps(
                {
                    "id": f"msg-{rid}",
                    "usage": {
                        "input_tokens": total // 2,
                        "output_tokens": total - total // 2,
                    },
                }
            )
        response = ResponseLog(
            status_code=status,
            headers={"content-type": "application/json"},
            body_text=body,
            is_streaming=False,
        )
        await storage.update_request_with_response(rid, response)

    pages, total = await storage.get_requests(page=1, limit=100, **filters)
    await storage.close()
    return [r.id for r in pages], total


def test_hide_failed_drops_error_statuses() -> None:
    ids, total = asyncio.run(_seed_and_get(hide_failed=True))
    assert "r-500" not in ids, f"500 should be hidden, got {ids}"
    assert "r-404" not in ids, f"404 should be hidden, got {ids}"
    assert "r-ok" in ids
    assert "r-zero" in ids
    print(f"✓ hide_failed=True drops errors: kept {ids}")


def test_hide_no_tokens_drops_null_and_zero() -> None:
    ids, total = asyncio.run(_seed_and_get(hide_no_tokens=True))
    assert "r-ok" in ids
    assert "r-zero" not in ids, f"zero tokens should be hidden, got {ids}"
    assert "r-notok" not in ids, f"null tokens should be hidden, got {ids}"
    assert "r-500" in ids, "r-500 has tokens → visible"
    # r-404 has NULL tokens → hidden by hide_no_tokens=True
    assert "r-404" not in ids, f"r-404 (NULL tokens) should be hidden, got {ids}"
    print(f"✓ hide_no_tokens=True drops NULL/0 token rows: kept {ids}")


def test_both_filters_default_on_keeps_only_healthy_with_data() -> None:
    ids, total = asyncio.run(_seed_and_get(hide_failed=True, hide_no_tokens=True))
    # Only healthy rows with usable token data survive both filters.
    assert "r-ok" in ids, f"healthy+token row should pass, got {ids}"
    assert "r-zero" not in ids, "zero tokens dropped"
    assert "r-500" not in ids, "errors dropped"
    assert "r-notok" not in ids, "no-token dropped"
    assert "r-404" not in ids, "both fail"
    assert total == 1, f"total should be 1 (r-ok), got {total}"
    print(f"✓ default ON (hide_failed+hide_no_tokens) keeps only r-ok: {ids}")


def test_no_filters_shows_everything() -> None:
    ids, total = asyncio.run(_seed_and_get())
    assert len(ids) == 5, f"with both filters off, all 5 rows should show, got {ids}"
    print(f"✓ no filters: all {len(ids)} rows visible")


def test_filters_compose_with_model_filter() -> None:
    """hide_no_tokens should still apply when scoped to a single model."""
    ids, total = asyncio.run(
        _seed_and_get(model_filter="opus", hide_no_tokens=True)
    )
    # Only r-ok (opus + 150 tokens) survives; r-zero (opus + 0 tokens) is dropped.
    assert ids == ["r-ok"], f"only r-ok should pass, got {ids}"
    print("✓ filters compose with model_filter (AND semantics)")


def test_dashboard_filter_bool_defaults() -> None:
    """``_filter_bool`` resolves absent params against the declared default."""
    from ccproxy.app.api.dashboard import _filter_bool

    assert _filter_bool("hide_failed", None) is True
    assert _filter_bool("hide_no_tokens", None) is True
    assert _filter_bool("hide_failed", "") is True
    assert _filter_bool("hide_failed", "0") is False
    assert _filter_bool("hide_failed", "false") is False
    assert _filter_bool("hide_failed", "FALSE") is False
    assert _filter_bool("hide_failed", "1") is True
    assert _filter_bool("hide_failed", "true") is True
    print("✓ _filter_bool defaults: absent→True, '0'→False, '1'→True")


def test_index_template_wires_filter_state() -> None:
    """Confirm the index template exposes the Alpine state the dashboard needs."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = _PROJECT_ROOT / "src" / "ccproxy" / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    # Render the index with default-on filters and no model.
    tpl = env.get_template("dashboard/index.html")
    rendered = tpl.render(
        view="requests",
        model_filter="",
        hide_failed=True,
        hide_no_tokens=True,
    )
    # Alpine state wiring
    assert "x-data=" in rendered
    assert "hide_failed:" in rendered
    assert "hide_no_tokens:" in rendered
    # Checkboxes render with state bindings
    assert 'x-model="hide_failed"' in rendered
    assert 'x-model="hide_no_tokens"' in rendered
    # @change handlers
    assert "@change=\"refresh()\"" in rendered
    # buildQuery is reachable
    assert "buildQuery" in rendered
    # Initial /partials/requests URL embeds the filter state
    assert "hide_failed=1" in rendered
    assert "hide_no_tokens=1" in rendered
    print("✓ index template wires filter state into Alpine + URL builder")


def test_index_template_off_state_renders_zero() -> None:
    """When filters are explicitly off, the URL embeds hide_*=0."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = _PROJECT_ROOT / "src" / "ccproxy" / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    tpl = env.get_template("dashboard/index.html")
    rendered = tpl.render(
        view="requests",
        model_filter="",
        hide_failed=False,
        hide_no_tokens=False,
    )
    assert "hide_failed=0" in rendered
    assert "hide_no_tokens=0" in rendered
    print("✓ index template with filters off encodes hide_*=0")


def main() -> None:
    tests = [
        test_hide_failed_drops_error_statuses,
        test_hide_no_tokens_drops_null_and_zero,
        test_both_filters_default_on_keeps_only_healthy_with_data,
        test_no_filters_shows_everything,
        test_filters_compose_with_model_filter,
        test_dashboard_filter_bool_defaults,
        test_index_template_wires_filter_state,
        test_index_template_off_state_renders_zero,
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
    print("\n✅ Dashboard filter tests passed!")


if __name__ == "__main__":
    main()
