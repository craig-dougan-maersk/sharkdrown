"""Tests for the ``_resolve_image_for_launch`` helper in
``src.ui.claude.docker_session``.

Covers the auto-resolve sentinels (empty string and the legacy
``"claude-code:latest"`` placeholder) and confirms that explicit
user-supplied image tags are honoured verbatim — including test
fixtures that use ``"alpine:latest"``.
"""

from __future__ import annotations

import pytest

from src.ui.claude import docker_session


def test_resolve_image_routes_empty_to_per_uid(monkeypatch) -> None:
    """Empty ``container_image`` (the new dataclass default)
    triggers ``ensure_image_for_current_uid`` — which rebuilds
    when the cached image's baked-in UID doesn't match."""
    called: dict = {}

    def _fake_ensure(*, runtime):
        called["runtime"] = runtime
        return "claude-code:uid-1000"

    monkeypatch.setattr(
        docker_session, "ensure_image_for_current_uid", _fake_ensure
    )
    tag = docker_session._resolve_image_for_launch("", runtime="podman")
    assert tag == "claude-code:uid-1000"
    assert called == {"runtime": "podman"}


def test_resolve_image_routes_legacy_placeholder_to_per_uid(monkeypatch) -> None:
    """``"claude-code:latest"`` was the dataclass default before
    the per-UID scheme was introduced. Profiles saved back then
    have this value persisted in ``profiles.json`` — we must
    still trigger the rebuild on launch, not silently fall
    through to the broken (UID 3000) image.
    """
    called: dict = {"value": False}

    def _fake_ensure(*, runtime):
        called["value"] = True
        return "claude-code:uid-1000"

    monkeypatch.setattr(
        docker_session, "ensure_image_for_current_uid", _fake_ensure
    )
    tag = docker_session._resolve_image_for_launch(
        "claude-code:latest", runtime="podman"
    )
    assert tag == "claude-code:uid-1000"
    assert called["value"] is True, (
        "legacy 'claude-code:latest' must route to "
        "ensure_image_for_current_uid, not pass through"
    )


def test_resolve_image_honours_explicit_user_tag(monkeypatch) -> None:
    """Anything outside the auto-resolve sentinel set — e.g.
    ``"alpine:latest"`` in tests or a user's custom image in
    advanced setups — passes through verbatim. ``ensure_image_for_current_uid``
    is NOT called.
    """
    called: dict = {"value": False}

    def _fake_ensure(*, runtime):
        called["value"] = True
        return "should-not-be-called"

    monkeypatch.setattr(
        docker_session, "ensure_image_for_current_uid", _fake_ensure
    )
    tag = docker_session._resolve_image_for_launch(
        "alpine:latest", runtime="podman"
    )
    assert tag == "alpine:latest"
    assert called["value"] is False, (
        "explicit user-supplied image must NOT trigger the rebuild path"
    )


def test_resolve_image_honours_custom_user_tag(monkeypatch) -> None:
    """A user who built and tagged their own image
    (``"ghcr.io/me/custom-claude:v3"``) gets that exact tag —
    no rewrite, no rebuild call.
    """
    called: dict = {"value": False}

    def _fake_ensure(*, runtime):
        called["value"] = True
        return "should-not-be-called"

    monkeypatch.setattr(
        docker_session, "ensure_image_for_current_uid", _fake_ensure
    )
    tag = docker_session._resolve_image_for_launch(
        "ghcr.io/me/custom-claude:v3", runtime="podman"
    )
    assert tag == "ghcr.io/me/custom-claude:v3"
    assert called["value"] is False


def test_resolve_image_routes_runtime_through(monkeypatch) -> None:
    """The ``runtime`` kwarg is passed through to
    ``ensure_image_for_current_uid`` so the rebuild runs against
    the same binary (podman vs docker) as the eventual
    ``podman create`` call. ``docker`` users shouldn't trigger
    a ``podman build``."""
    captured: dict = {}

    def _fake_ensure(*, runtime):
        captured["runtime"] = runtime
        return "claude-code:uid-1000"

    monkeypatch.setattr(
        docker_session, "ensure_image_for_current_uid", _fake_ensure
    )
    docker_session._resolve_image_for_launch("", runtime="docker")
    assert captured == {"runtime": "docker"}


print("✓ _resolve_image_for_launch routes sentinels to per-UID builder, honours explicit tags")