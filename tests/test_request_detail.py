"""Tests for the rewritten request detail pane.

Verifies the two changes the user asked for:

1. **Conversation priority**
   - User/claude messages appear at the top of the rendered pane.
   - Response text appears next, before any metadata.
   - Headers / Request details / Tools / Raw body are rendered in
     collapsed ``<details>`` elements (no ``open`` attribute on
     either the section toggle or its inner raw-JSON toggle), so they
     never dominate the first paint.

2. **Collapsible system-reminder**
   - Text blocks whose content starts with ``<system-reminder>`` are
     rendered inside ``<details>`` (no ``open`` attribute ⇒ collapsed).
   - Text blocks that only mention ``<system-reminder>`` *inline* (do
     not start with the tag) still render as a normal paragraph — so
     the trigger is precise.
   - String content passed through ``render_content`` (a non-list
     ``content``) collapses the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _env() -> "jinja2.Environment":
    """Build a Jinja environment with the dashboard templates loaded."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = _PROJECT_ROOT / "src" / "ccproxy" / "app" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    return env


def _fixture_request():
    """Build a typical anthropic_request_view dict with system-reminders, tools, etc."""
    from ccproxy.app.models.anthropic import AnthropicRequest

    req = AnthropicRequest.model_validate(
        {
            "model": "claude-opus",
            "max_tokens": 1024,
            "temperature": 0.5,
            "stream": True,
            "system": "You are Claude Code, an AI assistant.",
            "tools": [
                {"name": "Bash", "description": "Run shell"},
                {"name": "Read", "description": "Read files"},
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please fix the bug."},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "<system-reminder>\n"
                                "your mode is now 'edit'.\n"
                                "</system-reminder>"
                            ),
                        },
                        {"type": "text", "text": "Looking at the file now."},
                    ],
                },
                {
                    "role": "user",
                    "content": "Look at <system-reminder> too",  # inline mention — must NOT collapse
                },
            ],
        }
    )
    return req


def _fixture_request_view(req):
    """Build the ``request_view`` shape the dashboard endpoint hands the template."""
    return {"shape": "anthropic", "anthropic": req, "raw": {}}


def _fixture_response_view():
    """Build the ``response_view`` shape for an anthropic response."""
    from ccproxy.app.models.anthropic import AnthropicResponse

    resp = AnthropicResponse.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus",
            "content": [{"type": "text", "text": "I have fixed the bug."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 12},
        }
    )
    return {"shape": "anthropic", "anthropic": resp, "raw": ""}


def _fixture_headers():
    return [
        {"name": "x-api-key", "value": "sha256:abc", "is_hashed": True},
        {"name": "anthropic-version", "value": "2023-06-01", "is_hashed": False},
    ]


def _render_request_detail(req, request_view, response_view, headers_view):
    """Render dashboard/request_detail.html with the fixtures above."""
    env = _env()
    return env.get_template("dashboard/request_detail.html").render(
        request=req,
        request_view=request_view,
        response_view=response_view,
        headers_view=headers_view,
    )


def test_conversation_appears_before_metadata() -> None:
    """The rendered pane puts the conversation above headers/details/raw JSON."""
    from ccproxy.app.models.storage import RequestLog, ResponseLog
    from datetime import UTC, datetime

    req_log = RequestLog(
        id="req1",
        timestamp=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        method="POST",
        endpoint="/v1/messages",
        original_model="claude-opus",
        routed_model="claude-opus",
        request_path="passthrough",
        input_tokens=50,
        output_tokens=12,
        total_tokens=62,
    )
    req_log.response = ResponseLog(
        status_code=200,
        headers={"content-type": "application/json"},
        body_text='{"id":"msg_test"}',
        is_streaming=False,
    )

    req = _fixture_request()
    rendered = _render_request_detail(
        req_log,
        _fixture_request_view(req),
        _fixture_response_view(),
        _fixture_headers(),
    )

    # Conversation must come before headers/details/raw.
    conv_pos = rendered.find("Conversation")
    headers_pos = rendered.find("Headers (")
    details_pos = rendered.find("Request details")
    raw_pos = rendered.find("Raw request body")

    assert conv_pos != -1, "Conversation heading missing"
    assert headers_pos != -1, "Headers toggle missing"
    assert details_pos != -1, "Request details toggle missing"
    assert raw_pos != -1, "Raw request body toggle missing"

    assert conv_pos < headers_pos, (
        f"conversation ({conv_pos}) should come before headers ({headers_pos})"
    )
    assert conv_pos < details_pos, (
        f"conversation ({conv_pos}) should come before details ({details_pos})"
    )
    assert conv_pos < raw_pos, (
        f"conversation ({conv_pos}) should come before raw body ({raw_pos})"
    )
    print(f"✓ Conversation appears first (pos {conv_pos}),"
          f" headers ({headers_pos}), details ({details_pos}),"
          f" raw ({raw_pos})")


def test_response_section_appears_before_metadata() -> None:
    """Response text is the second visible block, before headers/details/raw."""
    from ccproxy.app.models.storage import RequestLog, ResponseLog
    from datetime import UTC, datetime

    req_log = RequestLog(
        id="req1",
        timestamp=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        method="POST",
        endpoint="/v1/messages",
        original_model="claude-opus",
        routed_model="claude-opus",
        request_path="passthrough",
    )
    req_log.response = ResponseLog(
        status_code=200,
        headers={"content-type": "application/json"},
        body_text='{"id":"msg_test","type":"message","role":"assistant",'
                  '"content":[{"type":"text","text":"OK"}],"stop_reason":"end_turn"}',
        is_streaming=False,
    )

    req = _fixture_request()
    rendered = _render_request_detail(
        req_log,
        _fixture_request_view(req),
        _fixture_response_view(),
        _fixture_headers(),
    )

    response_pos = rendered.find("Response (")
    headers_pos = rendered.find("Headers (")
    details_pos = rendered.find("Request details")

    assert response_pos != -1, "Response heading missing"
    assert response_pos < headers_pos, (
        f"response ({response_pos}) should be before headers ({headers_pos})"
    )
    assert response_pos < details_pos
    print(f"✓ Response appears before metadata ({response_pos} vs {headers_pos})")


def test_metadata_sections_are_closed_by_default() -> None:
    """Headers, Request details, Raw body must be collapsed (no `open` attr).

    Each is a ``<details>`` so the absence of ``open`` ⇒ collapsed.
    """
    from ccproxy.app.models.storage import RequestLog, ResponseLog
    from datetime import UTC, datetime

    req_log = RequestLog(
        id="req1",
        timestamp=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        method="POST",
        endpoint="/v1/messages",
        original_model="claude-opus",
        routed_model="claude-opus",
        request_path="passthrough",
    )
    req_log.response = ResponseLog(
        status_code=200,
        headers={},
        body_text="{}",
        is_streaming=False,
    )

    req = _fixture_request()
    rendered = _render_request_detail(
        req_log,
        _fixture_request_view(req),
        _fixture_response_view(),
        _fixture_headers(),
    )

    # Locate the headers section's <details>. The summary that follows
    # should be `Headers (2)` for our 2-row fixture.
    import re

    headers_section = re.search(
        r"<details>\s*<summary[^>]*>\s*Headers \(2\)", rendered
    )
    assert headers_section, "Headers details block missing or already open"
    details_section = re.search(
        r"<details>\s*<summary[^>]*>\s*Request details", rendered
    )
    assert details_section, "Request details details block missing or already open"
    raw_section = re.search(
        r"<details>\s*<summary[^>]*>\s*Raw request body", rendered
    )
    assert raw_section, "Raw request body details block missing or already open"
    # Each opens with bare ``<details>``, never ``<details open>``.
    assert "<details open>" not in rendered, (
        f"Found <details open>; metadata must be collapsed by default.\n"
        f"Snippet: {rendered[:400]!r}"
    )
    print("✓ Headers / Request details / Raw body all default-collapsed")


def test_system_reminder_block_is_collapsible() -> None:
    """A text block starting with <system-reminder> collapses by default."""
    env = _env()
    src = (
        _PROJECT_ROOT
        / "src"
        / "ccproxy"
        / "app"
        / "templates"
        / "dashboard"
        / "components"
        / "conversation_view.html"
    ).read_text()
    # Just render the template as a snippet that exercises the macro
    # via a single system-reminder block.
    rendered = env.get_template(
        "dashboard/components/conversation_view.html"
    ).module.render_text_block(
        "<system-reminder>\nyour mode is now 'edit'.\n</system-reminder>"
    )
    assert rendered.lstrip().startswith("<details"), (
        f"system-reminder should render as <details>, got: {rendered[:80]!r}"
    )
    # No `open` attribute → collapsed by default.
    assert "<details open>" not in rendered, (
        f"system-reminder must default-collapsed: {rendered[:120]!r}"
    )
    # The original text is preserved inside.
    assert "your mode is now 'edit'" in rendered
    # The user-facing label says "system-reminder".
    assert "system-reminder" in rendered.lower()
    print("✓ system-reminder text block renders as collapsed <details>")


def test_normal_text_is_not_collapsible() -> None:
    """A normal user message doesn't get wrapped in <details>."""
    env = _env()
    rendered = env.get_template(
        "dashboard/components/conversation_view.html"
    ).module.render_text_block("Please fix the bug.")
    assert "<details" not in rendered, (
        f"regular text must not collapse, got: {rendered!r}"
    )
    print("✓ normal text renders as a plain paragraph")


def test_inline_system_reminder_mention_does_not_collapse() -> None:
    """A user message that mentions the tag inline should NOT be wrapped."""
    env = _env()
    # The text contains `<system-reminder>` but does NOT start with it.
    rendered = env.get_template(
        "dashboard/components/conversation_view.html"
    ).module.render_text_block(
        "Look at <system-reminder> too"
    )
    assert "<details" not in rendered, (
        f"inline mention must not collapse, got: {rendered!r}"
    )
    print("✓ inline mention of <system-reminder> renders as plain text")


def test_full_conversation_renders_user_and_assistant_bubbles() -> None:
    """End-to-end: the conversation macro renders user + assistant bubbles."""
    env = _env()
    req = _fixture_request()
    rendered = env.get_template(
        "dashboard/components/conversation_view.html"
    ).module.conversation(req.messages)

    # 3 messages — 2 user, 1 assistant — with proper role-color boundaries.
    assert rendered.count("bg-blue-50") == 2, f"expected 2 user bubbles, got {rendered.count('bg-blue-50')}"
    assert rendered.count("bg-emerald-50") == 1, f"expected 1 assistant bubble, got {rendered.count('bg-emerald-50')}"
    # The conversation contains both visible texts.
    assert "Please fix the bug" in rendered
    assert "Looking at the file now" in rendered
    # And it folds the system-reminder.
    assert "<details" in rendered
    print("✓ full conversation renders user+assistant bubbles + folded reminder")


def test_string_content_path_also_collapses_system_reminder() -> None:
    """render_content collapses system-reminders when content is a plain string."""
    env = _env()
    rendered = env.get_template(
        "dashboard/components/conversation_view.html"
    ).module.render_content(
        "<system-reminder>\nplain-string form\n</system-reminder>"
    )
    assert rendered.lstrip().startswith("<details")
    assert "<details open>" not in rendered
    print("✓ string-content system-reminders also collapse")


def test_anthropic_request_view_delegates_to_conversation_macro() -> None:
    """anthropic_request_view.html must render the same conversation as the new component."""
    env = _env()
    req = _fixture_request()
    # Render via anthropic_request_view.
    rendered = env.get_template(
        "dashboard/components/anthropic_request_view.html"
    ).module.anthropic_request_view(req)
    # It must include the assistant bubble and a <details> (the system-reminder fold).
    assert "bg-emerald-50" in rendered, "anthropic_request_view lost the assistant bubble"
    assert "<details" in rendered, "anthropic_request_view lost the system-reminder fold"
    print("✓ anthropic_request_view delegates to conversation_view")


def main() -> None:
    tests = [
        test_conversation_appears_before_metadata,
        test_response_section_appears_before_metadata,
        test_metadata_sections_are_closed_by_default,
        test_system_reminder_block_is_collapsible,
        test_normal_text_is_not_collapsible,
        test_inline_system_reminder_mention_does_not_collapse,
        test_full_conversation_renders_user_and_assistant_bubbles,
        test_string_content_path_also_collapses_system_reminder,
        test_anthropic_request_view_delegates_to_conversation_macro,
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
    print("\n✅ Request detail tests passed!")


if __name__ == "__main__":
    main()
