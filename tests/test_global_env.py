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
import json
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
    profile doesn't set them — UNLESS the user has explicitly
    opted out via the ``tui_mode`` setting (covered by
    ``test_tui_mode_default_drops_no_flicker``).

    Pre-fix: this test asserted ``CLAUDE_CODE_NO_FLICKER=1`` was
    unconditionally present. After Bug A's fix, the test sets
    ``tui_mode="fullscreen"`` explicitly so it still asserts the
    legacy default behaviour (which is now opt-out, not
    opt-in)."""
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore
    from src.profiles.models import ClaudeProfile

    # Default tui_mode is "fullscreen" — preserves the legacy
    # behaviour the existing tests documented. The newer
    # tui_mode-default test (below) covers the opt-out path.
    settings = GlobalSettings()
    assert settings.tui_mode == "fullscreen"
    GlobalSettingsStore(tmp_path / "claude_global.json").save(settings)

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


def test_tui_mode_default_drops_no_flicker(tmp_path) -> None:
    """Bug A regression: when the user picks ``tui_mode="default"``
    in Claude > Settings > General, ``CLAUDE_CODE_NO_FLICKER`` MUST
    be removed from the session env even if ``defaults.env``
    shipped with the flicker-free flag set to ``1``.

    User-reported symptom: "the /tui setting for Claude code
    sessions seems to be fullscreen instead of default" — root
    cause was ``defaults.env`` shipping ``CLAUDE_CODE_NO_FLICKER=1``
    with no user-facing way to disable it. The fix adds a
    ``tui_mode`` field to ``GlobalSettings`` that
    ``build_claude_session_env`` consults AFTER loading
    ``defaults.env``, removing the flag for ``"default"`` and
    ``"off"`` modes.

    Setup:
      1. Deploy the packaged ``defaults.env`` (sets the flag).
      2. Save a ``claude_global.json`` with ``tui_mode="default"``.
      3. Build the session env and assert the flag is absent."""
    from src.profiles import global_env
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore
    from src.profiles.models import ClaudeProfile

    # Step 1: deploy defaults.env so the flag IS set.
    global_env.ensure_global_env_file(tmp_path)
    assert global_env.load_global_env(tmp_path).get(
        "CLAUDE_CODE_NO_FLICKER"
    ) == "1", "sanity: defaults.env should ship CLAUDE_CODE_NO_FLICKER=1"

    # Step 2: user opts out via tui_mode="default".
    GlobalSettingsStore(tmp_path / "claude_global.json").save(
        GlobalSettings(tui_mode="default")
    )

    # Step 3: the session env must NOT carry the flag.
    profile = ClaudeProfile(name="p", slug="p")
    env = build_claude_session_env(
        profile=profile,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/home/claude/.claude",
        api_key=None,
        data_dir=tmp_path,
    )
    assert "CLAUDE_CODE_NO_FLICKER" not in env, (
        f"tui_mode='default' should remove CLAUDE_CODE_NO_FLICKER "
        f"from the session env even if defaults.env set it; "
        f"got env keys: {sorted(env.keys())!r}"
    )
    # The other shipped default should still be present — the
    # override targets ONLY the TUI flag, not gateway discovery.
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"


def test_tui_mode_off_drops_no_flicker(tmp_path) -> None:
    """``tui_mode="off"`` is an alias for ``"default"`` — both
    remove the flicker-free flag."""
    from src.profiles import global_env
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore
    from src.profiles.models import ClaudeProfile

    global_env.ensure_global_env_file(tmp_path)
    GlobalSettingsStore(tmp_path / "claude_global.json").save(
        GlobalSettings(tui_mode="off")
    )

    profile = ClaudeProfile(name="p", slug="p")
    env = build_claude_session_env(
        profile=profile,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/home/claude/.claude",
        api_key=None,
        data_dir=tmp_path,
    )
    assert "CLAUDE_CODE_NO_FLICKER" not in env


def test_tui_mode_fullscreen_keeps_no_flicker(tmp_path) -> None:
    """``tui_mode="fullscreen"`` (the legacy default) keeps the
    flag — opt-out, not opt-in. Users who upgrade keep the
    historic behaviour until they explicitly choose otherwise."""
    from src.profiles import global_env
    from src.profiles.env_builder import build_claude_session_env
    from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore
    from src.profiles.models import ClaudeProfile

    global_env.ensure_global_env_file(tmp_path)
    GlobalSettingsStore(tmp_path / "claude_global.json").save(
        GlobalSettings(tui_mode="fullscreen")
    )

    profile = ClaudeProfile(name="p", slug="p")
    env = build_claude_session_env(
        profile=profile,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/home/claude/.claude",
        api_key=None,
        data_dir=tmp_path,
    )
    assert env["CLAUDE_CODE_NO_FLICKER"] == "1"


def test_tui_mode_round_trips_through_settings_file(tmp_path) -> None:
    """``tui_mode`` must round-trip through
    ``claude_global.json`` — the legacy default
    (``"fullscreen"``) drops out of the file to keep the diff
    clean, but any non-default value persists."""
    from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore

    # Default: dropped from the file.
    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    store.save(GlobalSettings())
    data = json.loads((tmp_path / "claude_global.json").read_text())
    assert "tui_mode" not in data, (
        f"legacy default should not be persisted to keep the file "
        f"diff clean; got: {data!r}"
    )

    # Non-default values round-trip.
    for value in ("default", "off"):
        store.save(GlobalSettings(tui_mode=value))
        data = json.loads((tmp_path / "claude_global.json").read_text())
        assert data.get("tui_mode") == value, (
            f"tui_mode={value!r} should persist in claude_global.json; "
            f"got: {data!r}"
        )


def test_tui_mode_unknown_value_falls_back_to_fullscreen(tmp_path) -> None:
    """A hand-edited ``claude_global.json`` with a typo
    (``tui_mode="flicker-free"``) must not silently disable the
    flicker-free flag. The deserialize step validates against the
    known set ``{"fullscreen", "default", "off"}`` and falls back
    to ``"fullscreen"`` for anything else."""
    from src.profiles.global_settings import GlobalSettingsStore

    path = tmp_path / "claude_global.json"
    path.write_text(json.dumps({"tui_mode": "flicker-free"}))
    settings = GlobalSettingsStore(path).load()
    assert settings.tui_mode == "fullscreen", (
        f"unknown tui_mode value should fall back to 'fullscreen'; "
        f"got: {settings.tui_mode!r}"
    )


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