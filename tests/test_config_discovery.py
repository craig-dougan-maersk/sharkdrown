"""Tests for config-file discovery in the embedded proxy.

Verifies that ``find_config_yaml`` / ``find_env_file`` in
``src.proxy.paths`` correctly locate ``config.yaml`` and ``.env`` in the
search order documented in the module, and that ``Settings`` picks up
the upstream URL from the discovered YAML.

Runs without QApplication — pure filesystem/Pydantic logic.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Add both the project root and src/ so ``src.proxy.paths`` and
# ``ccproxy.app.config`` are importable regardless of CWD.
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from src.proxy.paths import (  # noqa: E402
    _candidate_config_dirs,
    find_config_yaml,
    find_env_file,
)


def test_candidate_dirs_are_unique() -> None:
    """`_candidate_config_dirs` must not return duplicates."""
    dirs = _candidate_config_dirs()
    resolved = [d.resolve() for d in dirs]
    assert len(resolved) == len(set(resolved)), f"duplicates in {resolved}"


def test_find_config_yaml_with_explicit_dir() -> None:
    """When an explicit config_dir is given, look only there first."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "config.yaml").write_text("providers: {}\n")
        result = find_config_yaml(td_path)
        assert result == td_path / "config.yaml", f"got {result}"


def test_find_env_file_with_explicit_dir() -> None:
    """Same for .env."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / ".env").write_text("OPENAI_API_KEY=test\n")
        result = find_env_file(td_path)
        assert result == td_path / ".env", f"got {result}"


def test_find_returns_none_when_missing() -> None:
    """If no candidate contains the file, return None."""
    # Use an empty directory as the explicit override; should miss and
    # fall through to other candidates (which we then assert don't have
    # either file in this test's temp dir).
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # No config.yaml or .env in td_path
        explicit = find_config_yaml(td_path)
        # If explicit override is set and file is missing, return None
        # rather than scanning other candidates.
        assert explicit is None, f"expected None, got {explicit}"


def test_settings_loads_discovered_config() -> None:
    """Settings should use the upstream URL from the discovered YAML."""
    yaml_path = find_config_yaml()
    if yaml_path is None:
        print("⚠ no config.yaml discovered in candidate dirs — skipping")
        return
    print(f"✓ discovered config.yaml at {yaml_path}")
    from ccproxy.app.config import Settings

    s = Settings(config_yaml_path=yaml_path)
    # Whichever upstream is configured, just verify it loaded.
    base = str(s.providers.anthropic.base_url)
    assert base.startswith("http"), f"unexpected base_url: {base}"
    print(f"✓ Settings loaded anthropic base_url = {base}")


def main() -> None:
    tests = [
        test_candidate_dirs_are_unique,
        test_find_config_yaml_with_explicit_dir,
        test_find_env_file_with_explicit_dir,
        test_find_returns_none_when_missing,
        test_settings_loads_discovered_config,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n❌ {failed} test(s) failed")
        sys.exit(1)
    print("\n✅ Config discovery tests passed!")


if __name__ == "__main__":
    main()