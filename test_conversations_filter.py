"""Tests for the "Hide 0-message conversations" filter.

Covers:
  - ConversationReader now reports accurate ``message_count`` in the
    metadata-only list view (was always 0; broke ``hide_empty``)
  - ``hide_empty=True`` drops zero-message JSONL files before
    pagination so ``has_more`` stays honest
  - Dashboard route parses ``hide_empty`` like the other checkbox
    filters — default ON, "0"/"false" → False
  - The conversations index template renders the new checkbox, binds
    it to Alpine, encodes ``hide_empty=1`` in the initial-load URL by
    default, and renders ``hide_empty=0`` when explicitly disabled
  - The "Load more" button carries the same filter via
    ``hx-on::config-request`` (so a user who turns the filter off on
    page 1 doesn't get re-defaulted when paging forward)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _seed_conversations(base: Path) -> None:
    """Create a small fixture tree under ``base/<project>/*.jsonl``.

    Mix of:
      - empty.jsonl: zero parseable lines  (subagent that never sent)
      - header-only.jsonl: one parseable header line (also 0 messages
        on our count, since we treat parseable lines uniformly)
      - real.jsonl: three real messages
    """
    proj = base / "-home-craig-test"
    proj.mkdir(parents=True)

    (proj / "empty.jsonl").write_text("", encoding="utf-8")
    (proj / "header-only.jsonl").write_text(
        json.dumps(
            {
                "type": "header",
                "timestamp": "2026-07-19T12:00:00Z",
                "message": {"role": "system"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    real_lines = []
    for i in range(3):
        real_lines.append(
            json.dumps(
                {
                    "type": "user" if i % 2 == 0 else "assistant",
                    "timestamp": f"2026-07-19T12:00:0{i}Z",
                    "message": {
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": f"line {i}",
                    },
                }
            )
        )
    (proj / "real.jsonl").write_text("\n".join(real_lines) + "\n", encoding="utf-8")


def test_metadata_now_reports_real_message_count() -> None:
    """``_build_metadata`` must surface ``message_count`` (was always 0)."""
    from ccproxy.app.conversations.reader import ConversationReader

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        _seed_conversations(base)
        reader = ConversationReader(base_path=base)

        rows, total = reader.list_conversations(page=1, limit=10)
        assert total == 3
        by_session = {r.session_id: r for r in rows}
        assert by_session["empty"].message_count == 0
        assert by_session["header-only"].message_count == 1
        assert by_session["real"].message_count == 3
    print("✓ metadata reports accurate message counts (0/1/3)")


def test_hide_empty_drops_zero_message_sessions() -> None:
    from ccproxy.app.conversations.reader import ConversationReader

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        _seed_conversations(base)
        reader = ConversationReader(base_path=base)

        all_rows, all_total = reader.list_conversations(page=1, limit=10)
        assert all_total == 3, f"baseline should show all 3, got {all_total}"

        rows, total = reader.list_conversations(page=1, limit=10, hide_empty=True)
        kept = sorted(r.session_id for r in rows)
        assert total == 2, f"hide_empty should keep 2, got {total}: {kept}"
        assert "empty" not in kept, "empty.jsonl should be hidden"
        # header-only has 1 parseable line ⇒ counted as a message ⇒ kept
        assert "header-only" in kept, "header-only has 1 line → kept"
        assert "real" in kept
    print("✓ hide_empty=True drops the truly empty file")


def test_hide_empty_paginates_after_filtering() -> None:
    """Page boundaries should reflect filtered counts, not raw counts.

    Without this, ``has_more`` would point the user at a Load more
    button that comes back empty.
    """
    from ccproxy.app.conversations.reader import ConversationReader

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        proj = base / "-home-craig-test"
        proj.mkdir(parents=True)
        # 3 empty + 2 real. With hide_empty the total must be 2, not 5.
        for i in range(3):
            (proj / f"empty-{i}.jsonl").write_text("", encoding="utf-8")
        for i in range(2):
            (proj / f"real-{i}.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-07-19T12:00:00Z",
                        "message": {"role": "user", "content": "hi"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        reader = ConversationReader(base_path=base)
        rows_p1, total_p1 = reader.list_conversations(
            page=1, limit=2, hide_empty=True
        )
        assert total_p1 == 2, f"filtered total must be 2, got {total_p1}"
        assert len(rows_p1) == 2
        rows_p2, total_p2 = reader.list_conversations(
            page=2, limit=2, hide_empty=True
        )
        assert len(rows_p2) == 0, "page 2 past the filtered end is empty"
    print("✓ hide_empty paginates over filtered rows, not raw")


def test_dashboard_filter_bool_default_for_hide_empty() -> None:
    """Absent param defaults True; '0' / 'false' resolve to False."""
    from ccproxy.app.api.dashboard import _filter_bool

    assert _filter_bool("hide_empty", None) is True
    assert _filter_bool("hide_empty", "") is True
    assert _filter_bool("hide_empty", "1") is True
    assert _filter_bool("hide_empty", "true") is True
    assert _filter_bool("hide_empty", "0") is False
    assert _filter_bool("hide_empty", "false") is False
    print("✓ _filter_bool(hide_empty) default-on + standard 0/false handling")


def test_index_template_renders_hide_empty_default_on() -> None:
    """When view=conversations, the new checkbox must be in the markup
    and the initial URL must carry hide_empty=1."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = _PROJECT_ROOT / "src" / "ccproxy" / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    tpl = env.get_template("dashboard/index.html")
    rendered = tpl.render(
        view="conversations",
        model_filter="",
        hide_failed=True,
        hide_no_tokens=True,
        hide_empty=True,
    )
    assert "hide_empty" in rendered
    assert 'x-model="hide_empty"' in rendered, "checkbox must bind to Alpine state"
    assert "hx-trigger=\"change\"" in rendered, "checkbox must re-fetch on toggle"
    # Initial-load URL embeds hide_empty=1
    assert "/partials/conversations?page=1&hide_empty=1" in rendered
    # buildQuery forwards hide_empty too
    assert "this.hide_empty" in rendered
    print("✓ conversations view: checkbox rendered + initial URL is hide_empty=1")


def test_index_template_renders_hide_empty_default_off() -> None:
    """If the user turns the filter off server-side, the URL must be 0."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = _PROJECT_ROOT / "src" / "ccproxy" / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    tpl = env.get_template("dashboard/index.html")
    rendered = tpl.render(
        view="conversations",
        model_filter="",
        hide_failed=True,
        hide_no_tokens=True,
        hide_empty=False,
    )
    assert "/partials/conversations?page=1&hide_empty=0" in rendered
    print("✓ conversations view with hide_empty=False encodes hide_empty=0")


def test_load_more_button_keeps_filter_state() -> None:
    """``Load more`` uses ``hx-on::config-request`` to inject the
    current ``hide_empty`` choice into the outbound URL, so users
    who turn the filter off on page 1 don't get un-defaulted on
    subsequent pages."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = _PROJECT_ROOT / "src" / "ccproxy" / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    tpl = env.get_template("dashboard/conversation_list.html")
    # At least one row + has_more=True so the Load more branch renders.
    row = {
        "session_id": "abc",
        "project_name": "p",
        "project_path": "p",
        "message_count": 3,
        "start_time": None,
        "file_path": None,
    }
    rendered = tpl.render(
        rows=[row], has_more=True, next_page=2, hide_empty=True
    )
    assert "hx-on::config-request" in rendered
    assert "hide_empty" in rendered
    print("✓ Load more button carries the current hide_empty state")


def main() -> None:
    tests = [
        test_metadata_now_reports_real_message_count,
        test_hide_empty_drops_zero_message_sessions,
        test_hide_empty_paginates_after_filtering,
        test_dashboard_filter_bool_default_for_hide_empty,
        test_index_template_renders_hide_empty_default_on,
        test_index_template_renders_hide_empty_default_off,
        test_load_more_button_keeps_filter_state,
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
    print("\n✅ All conversations-filter tests passed!")


if __name__ == "__main__":
    main()
