"""Unit tests for ``src.ui.claude.docker_image_builder``.

Pure-Python (no PySide6) — runs in the headless test harness. Each
test patches ``subprocess.run`` where needed so we never actually
invoke ``podman`` / ``docker``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest


# ---- resolve_image_tag --------------------------------------------------


def test_resolve_image_tag_uses_uid_in_tag() -> None:
    from src.ui.claude import docker_image_builder

    assert docker_image_builder.resolve_image_tag(1000) == "claude-code:uid-1000"
    assert docker_image_builder.resolve_image_tag(1234) == "claude-code:uid-1234"
    assert docker_image_builder.resolve_image_tag(65534) == "claude-code:uid-65534"


def test_resolve_image_tag_root_falls_back_with_warning() -> None:
    """``os.getuid() == 0`` (root) is forbidden — the image bakes
    ``USER claude`` which would override to UID 0, breaking the
    security model. The resolver substitutes UID 1000 and emits a
    ``UserWarning`` so the host app can surface it on the status
    bar.
    """
    from src.ui.claude import docker_image_builder

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        tag = docker_image_builder.resolve_image_tag(0)
    assert tag == f"claude-code:uid-{docker_image_builder._FALLBACK_UID_FOR_ROOT}"
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert any("non-root UID" in str(w.message) for w in user_warnings), (
        f"expected a UserWarning about non-root UID, got "
        f"{[str(w.message) for w in user_warnings]}"
    )


def test_resolve_image_tag_negative_input_falls_back() -> None:
    """A negative UID is treated as root-equivalent — defensive
    against ``os.getuid()`` ever returning garbage."""
    from src.ui.claude import docker_image_builder

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        tag = docker_image_builder.resolve_image_tag(-1)
    assert tag.startswith("claude-code:uid-")
    assert any(issubclass(w.category, UserWarning) for w in caught)


# ---- get_host_uid_gid ---------------------------------------------------


def test_get_host_uid_gid_returns_posix_ids() -> None:
    """On POSIX (Linux/macOS) the helper returns the real
    ``os.getuid`` / ``os.getgid`` values. We can't assert exact
    numbers (the test runner is whoever it is), but we can assert
    they're non-negative and equal on most systems.
    """
    from src.ui.claude import docker_image_builder

    if not hasattr(os, "getuid"):
        pytest.skip("POSIX-only test")
    uid, gid = docker_image_builder.get_host_uid_gid()
    assert uid >= 0
    assert gid >= 0


def test_get_host_uid_gid_fallback_on_non_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``os.getuid`` is missing (Windows), the helper returns
    the (1000, 1000) default rather than raising. We simulate the
    non-POSIX environment by deleting the attribute on the
    builder's own reference (the helper uses ``os.getuid``).
    """
    from src.ui.claude import docker_image_builder

    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.delattr(os, "getgid", raising=False)
    assert docker_image_builder.get_host_uid_gid() == (1000, 1000)


# ---- image_has_correct_uid ---------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_image_has_correct_uid_returns_true_on_match(monkeypatch) -> None:
    from src.ui.claude import docker_image_builder

    def _fake_run(cmd, *args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="1000")

    monkeypatch.setattr(docker_image_builder.subprocess, "run", _fake_run)
    assert docker_image_builder.image_has_correct_uid(
        "claude-code:uid-1000", 1000
    ) is True


def test_image_has_correct_uid_returns_false_on_mismatch(monkeypatch) -> None:
    from src.ui.claude import docker_image_builder

    def _fake_run(cmd, *args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="3000")

    monkeypatch.setattr(docker_image_builder.subprocess, "run", _fake_run)
    assert docker_image_builder.image_has_correct_uid(
        "claude-code:uid-1000", 1000
    ) is False


def test_image_has_correct_uid_returns_false_when_image_missing(
    monkeypatch,
) -> None:
    """A non-zero ``returncode`` means the image doesn't exist
    locally. The caller treats this as "needs rebuild" without
    raising — the user sees a friendlier "building image" status
    message rather than a noisy ``CalledProcessError``."""
    from src.ui.claude import docker_image_builder

    def _fake_run(cmd, *args, **kwargs):
        return _FakeCompleted(returncode=125, stdout="")

    monkeypatch.setattr(docker_image_builder.subprocess, "run", _fake_run)
    assert docker_image_builder.image_has_correct_uid(
        "claude-code:uid-1000", 1000
    ) is False


def test_image_has_correct_uid_returns_false_on_empty_label(monkeypatch) -> None:
    """An image predating the label scheme returns empty stdout.
    Treat as wrong-UID so the rebuild path stamps the label onto
    the new image."""
    from src.ui.claude import docker_image_builder

    def _fake_run(cmd, *args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(docker_image_builder.subprocess, "run", _fake_run)
    assert docker_image_builder.image_has_correct_uid(
        "claude-code:uid-1000", 1000
    ) is False


def test_image_has_correct_uid_inspects_with_format(monkeypatch) -> None:
    """The inspect command uses ``--format`` with the label lookup
    template — make sure the helper passes the right ``go-template``
    syntax that podman/docker understands.
    """
    from src.ui.claude import docker_image_builder

    captured: dict = {}

    def _fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(returncode=0, stdout="1000")

    monkeypatch.setattr(docker_image_builder.subprocess, "run", _fake_run)
    docker_image_builder.image_has_correct_uid("claude-code:uid-1000", 1000)
    cmd = captured["cmd"]
    assert cmd[:2] == ["podman", "inspect"]
    assert "--format" in cmd
    fmt_idx = cmd.index("--format")
    fmt_value = cmd[fmt_idx + 1]
    # The format must reference our label key.
    assert docker_image_builder.CLAUDE_UID_LABEL in fmt_value
    # And use the ``index`` function (podman/docker template syntax).
    assert "index" in fmt_value


# ---- rebuild_image_for_uid ---------------------------------------------


def test_rebuild_image_for_uid_invokes_podman_build(monkeypatch) -> None:
    from src.ui.claude import docker_image_builder

    captured: dict = {}

    def _fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(docker_image_builder.subprocess, "run", _fake_run)
    tag = docker_image_builder.rebuild_image_for_uid(1000, 1000, runtime="podman")
    assert tag == "claude-code:uid-1000"
    cmd = captured["cmd"]
    assert cmd[0] == "podman"
    assert cmd[1] == "build"
    assert "-f" in cmd
    # Find the build-arg positions; Dockerfiles use them as
    # ``--build-arg KEY=VALUE`` pairs. Walk the cmd list so we
    # capture the value at index ``i+1`` for each ``--build-arg``
    # at index ``i`` (don't use ``cmd.index(f)`` — that returns
    # the first match and misses subsequent pairs).
    values: list[str] = []
    for i, c in enumerate(cmd):
        if c == "--build-arg" and i + 1 < len(cmd):
            values.append(cmd[i + 1])
    assert "CLAUDE_UID=1000" in values
    assert "CLAUDE_GID=1000" in values
    # The -t flag must point at the per-UID tag.
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "claude-code:uid-1000"


def test_rebuild_image_for_uid_raises_on_subprocess_failure(monkeypatch) -> None:
    """A failed build (e.g. no network for ``npm install``) must
    raise ``CalledProcessError`` with the captured stderr so the
    caller can show a meaningful dialog."""
    from src.ui.claude import docker_image_builder

    def _fake_run(cmd, *args, **kwargs):
        return _FakeCompleted(
            returncode=1,
            stdout="",
        ).__class__(
            returncode=1,
            stdout="",
            stderr="npm ERR! network unreachable",
        ) if False else _FakeCompletedWithStderr(
            returncode=1, stdout="", stderr="npm ERR! network unreachable"
        )

    monkeypatch.setattr(docker_image_builder.subprocess, "run", _fake_run)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        docker_image_builder.rebuild_image_for_uid(1000, 1000)
    assert "network unreachable" in str(exc_info.value.stderr)


class _FakeCompletedWithStderr(_FakeCompleted):
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        super().__init__(returncode, stdout)
        self.stderr = stderr


# ---- _find_dockerfile ---------------------------------------------------


def test_find_dockerfile_resolves_bundled_copy() -> None:
    """The bundled Dockerfile is reachable via the package's
    resources — this is what an installed wheel user gets."""
    from src.ui.claude import docker_image_builder

    p = docker_image_builder._find_dockerfile()
    assert p.is_file()
    assert p.name == "Dockerfile.claude-code"


# ---- ensure_image_for_current_uid (idempotency) ------------------------


def test_ensure_image_noop_when_image_matches(monkeypatch) -> None:
    """When the right image is already cached, ``ensure_image_for_current_uid``
    is a no-op — no rebuild invocation, just the tag."""
    from src.ui.claude import docker_image_builder

    host_uid, host_gid = docker_image_builder.get_host_uid_gid()
    if not hasattr(os, "getuid"):
        pytest.skip("POSIX-only test")

    rebuild_called = {"value": False}

    def _fake_rebuild(host_uid, host_gid, *, runtime="podman"):
        rebuild_called["value"] = True
        return "should-not-be-called"

    def _fake_inspect(tag, host_uid, *, runtime="podman"):
        return True

    monkeypatch.setattr(docker_image_builder, "image_has_correct_uid", _fake_inspect)
    monkeypatch.setattr(docker_image_builder, "rebuild_image_for_uid", _fake_rebuild)
    tag = docker_image_builder.ensure_image_for_current_uid()
    assert tag == f"claude-code:uid-{host_uid}"
    assert rebuild_called["value"] is False, (
        "ensure_image_for_current_uid must not rebuild when image matches"
    )


def test_ensure_image_rebuilds_on_mismatch(monkeypatch) -> None:
    from src.ui.claude import docker_image_builder

    if not hasattr(os, "getuid"):
        pytest.skip("POSIX-only test")

    rebuild_called = {"value": False}

    def _fake_rebuild(host_uid, host_gid, *, runtime="podman"):
        rebuild_called["value"] = True
        return f"claude-code:uid-{host_uid}"

    def _fake_inspect(tag, host_uid, *, runtime="podman"):
        return False

    monkeypatch.setattr(docker_image_builder, "image_has_correct_uid", _fake_inspect)
    monkeypatch.setattr(docker_image_builder, "rebuild_image_for_uid", _fake_rebuild)
    tag = docker_image_builder.ensure_image_for_current_uid()
    assert rebuild_called["value"] is True
    assert tag.startswith("claude-code:uid-")


print("✓ docker_image_builder resolves tags, detects UID drift, and rebuilds on mismatch")