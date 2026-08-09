"""Regression tests for the global Claude env file (PR4, issues #1 + #8).

Covers:
- ``parse_env_file`` round-trip with comments / blank lines / export prefix.
- ``ensure_global_env_file`` is idempotent and does NOT overwrite an
  existing user-edited file (the safety property).
- ``save_global_env`` writes atomically with ``chmod 600``.
- ``build_claude_session_env`` layers global defaults above session env
  but below the infra-var safety-net.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def test_parse_env_file_handles_comments_blanks_export() -> None:
    """``parse_env_file`` returns a dict that ignores comments / blank
    lines and strips the ``export `` prefix."""
    from src.profiles.global_env import parse_env_file

    text = (
        "# top comment\n"
        "\n"
        "CLAUDE_CODE_NO_FLICKER=0\n"
        "  export ANTHROPIC_API_KEY=sk-test  \n"
        "# trailing comment\n"
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1\n"
    )
    parsed = parse_env_file_text(text)
    assert parsed == {
        "CLAUDE_CODE_NO_FLICKER": "0",
        "ANTHROPIC_API_KEY": "sk-test",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }


def test_ensure_global_env_file_deploys_on_first_run(tmp_path) -> None:
    """First-launch deploy path: file is created from the packaged
    template and is ``chmod 600``."""
    from src.profiles import global_env

    target = tmp_path / "defaults.env"
    assert not target.exists()
    path = global_env.ensure_global_env_file(tmp_path)
    assert path == target
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text(encoding="utf-8")
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1" in text
    assert "CLAUDE_CODE_NO_FLICKER=1" in text


def test_ensure_global_env_file_does_not_overwrite_existing(tmp_path) -> None:
    """Regression: must NOT overwrite an existing user-edited file.

    The user explicitly chose "no hardcoded variables" — the deployed
    file is the user's source of truth after the first launch.
    ``ensure_global_env_file`` must be a no-op when the file already
    exists, regardless of its contents.
    """
    from src.profiles import global_env

    target = tmp_path / "defaults.env"
    user_text = "# my customisations\nCLAUDE_CODE_NO_FLICKER=0\n"
    target.write_text(user_text, encoding="utf-8")
    mtime_before = target.stat().st_mtime_ns
    content_before = target.read_text(encoding="utf-8")

    global_env.ensure_global_env_file(tmp_path)

    assert target.stat().st_mtime_ns == mtime_before, "file was modified"
    assert target.read_text(encoding="utf-8") == content_before


def test_save_global_env_round_trip_and_chmod_600(tmp_path) -> None:
    """``save_global_env`` writes back what was parsed, with chmod 600."""
    from src.profiles import global_env

    env = {
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        "CLAUDE_CODE_NO_FLICKER": "0",
        "CUSTOM_VAR": "hello world",
    }
    global_env.save_global_env(tmp_path, env)
    target = tmp_path / "defaults.env"
    assert target.exists()
    assert target.stat().st_mode & 0o777 == 0o600
    parsed = global_env.parse_env_file(target)
    assert parsed == env


def test_env_builder_includes_global_defaults(tmp_path) -> None:
    """``build_claude_session_env`` layers the deployed defaults
    into the env dict. The two shipped vars appear even when the
    profile doesn't set them."""
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.models import ClaudeProfile

    profile = ClaudeProfile(name="p", slug="p")
    env = build_claude_session_env(
        profile=profile,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/home/claude/.claude",
        api_key=None,
        data_dir=tmp_path,
    )
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["CLAUDE_CODE_NO_FLICKER"] == "1"


def test_env_builder_user_can_override_via_defaults_env(tmp_path) -> None:
    """A user-edited ``defaults.env`` overrides the shipped default."""
    from src.profiles import global_env
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.models import ClaudeProfile

    # First deploy, then edit, then rebuild.
    global_env.ensure_global_env_file(tmp_path)
    target = tmp_path / "defaults.env"
    env = global_env.parse_env_file(target)
    env["CLAUDE_CODE_NO_FLICKER"] = "0"
    global_env.save_global_env(tmp_path, env)

    profile = ClaudeProfile(name="p", slug="p")
    result = build_claude_session_env(
        profile=profile,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/home/claude/.claude",
        api_key=None,
        data_dir=tmp_path,
    )
    assert result["CLAUDE_CODE_NO_FLICKER"] == "0"


def test_env_builder_globals_never_break_proxy_routing(tmp_path) -> None:
    """User tries to set ``ANTHROPIC_BASE_URL`` in ``defaults.env``;
    the env-builder safety-net re-asserts the integration value."""
    import warnings

    from src.profiles import global_env
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.models import ClaudeProfile

    global_env.save_global_env(
        tmp_path,
        {
            "CLAUDE_CODE_NO_FLICKER": "0",
            "ANTHROPIC_BASE_URL": "http://evil.example/",
        },
    )
    profile = ClaudeProfile(name="p", slug="p")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = build_claude_session_env(
            profile=profile,
            proxy_base_url="http://127.0.0.1:3001",
            config_dir="/home/claude/.claude",
            api_key=None,
            data_dir=tmp_path,
        )
    # The safety-net must win over the user's globals.
    assert result["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:3001"
    assert result["CLAUDE_CODE_NO_FLICKER"] == "0"


def test_env_builder_data_dir_none_skips_global_layer() -> None:
    """Backward-compat: legacy callers that don't pass ``data_dir``
    see no global layer at all. This is the property that lets
    the existing test suite (which doesn't thread a data dir) keep
    passing without changes."""
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.models import ClaudeProfile

    profile = ClaudeProfile(name="p", slug="p")
    result = build_claude_session_env(
        profile=profile,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/home/claude/.claude",
        api_key=None,
        # data_dir=None explicitly
    )
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in result


def test_load_global_env_round_trip(tmp_path) -> None:
    """``load_global_env`` is ``ensure + parse``."""
    from src.profiles import global_env

    env = global_env.load_global_env(tmp_path)
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["CLAUDE_CODE_NO_FLICKER"] == "1"


# ---- helpers --------------------------------------------------------------


def parse_env_file_text(text: str) -> dict[str, str]:
    """Helper to parse inline text using the same regex as the file
    parser, without needing a temp file."""
    out: dict[str, str] = {}
    line_re = re.compile(
        r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
    )
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = line_re.match(raw)
        if not m:
            continue
        out[m.group(1)] = m.group(2)
    return out