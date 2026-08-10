"""Build the Claude Code container image with the host user's UID.

The Claude Code image embeds a non-root ``claude`` user whose UID
must match the *host user* that owns the bind-mounted profile
directory. Rootless podman does NOT remap host UIDs into the
container, so a host UID mismatch leaves the in-container
``claude`` user unable to write to ``~/.claude`` — Claude Code
surfaces *"EACCES: permission denied, mkdir '...'"* followed by
*"Invalid API key"* (because the session never reaches the
network layer).

The fix: on each container launch, the app inspects
``os.getuid()``, compares it against the image's
``io.sharkdrown.claude-uid`` label (set at build time), and
rebuilds the image with the right ``--build-arg`` if they don't
match. Each host UID gets its own image tag (``claude-code:uid-<n>``)
so multi-user hosts don't fight over a shared tag.

Pure Python (no PySide6 imports) so the unit tests can run
headless.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("sharkdrown.docker_image_builder")


#: Image-label key the builder reads to detect UID drift. Set in
#: the Dockerfile via ``LABEL io.sharkdrown.claude-uid="${CLAUDE_UID}"``.
CLAUDE_UID_LABEL = "io.sharkdrown.claude-uid"

#: Fallback UID when ``os.getuid() == 0``. Running the GUI as root
#: is unusual under rootless podman, but the Docker image bakes a
#: non-root ``claude`` user — baking UID 0 would override ``USER
#: claude`` and break the security model. We substitute 1000 (the
#: typical first-user UID on Linux) and emit a ``UserWarning`` so
#: the host app can surface it on the status bar.
_FALLBACK_UID_FOR_ROOT = 1000

#: Bundle-relative path to the bundled Dockerfile. ``podman build
#: -f <path>`` reads it directly without needing a repo checkout.
_BUNDLED_DOCKERFILE = "Dockerfile.claude-code"


# ---- Public API -----------------------------------------------------------


def resolve_image_tag(host_uid: int) -> str:
    """Return the image tag that has ``host_uid`` baked in.

    Tag format: ``claude-code:uid-<n>``. The UID 0 special case
    falls back to ``_FALLBACK_UID_FOR_ROOT`` and emits a
    ``UserWarning`` — see the module docstring for why we can't
    bake UID 0.

    >>> resolve_image_tag(1000)
    'claude-code:uid-1000'
    >>> resolve_image_tag(1234)
    'claude-code:uid-1234'
    """
    if host_uid <= 0:
        warnings.warn(
            f"host UID is {host_uid}; the Claude Code image requires "
            f"a non-root UID. Falling back to "
            f"{_FALLBACK_UID_FOR_ROOT} for the baked-in user.",
            UserWarning,
            stacklevel=2,
        )
        host_uid = _FALLBACK_UID_FOR_ROOT
    return f"claude-code:uid-{host_uid}"


def get_host_uid_gid() -> tuple[int, int]:
    """Return ``(uid, gid)`` for the current process.

    Falls back to ``(1000, 1000)`` on platforms without
    ``os.getuid`` (Windows) so callers don't have to gate on
    platform. The fallback UID matches the image's default so a
    Windows-built image keeps working.
    """
    if hasattr(os, "getuid"):
        uid = int(os.getuid())
        gid = int(os.getgid()) if hasattr(os, "getgid") else uid
        return uid, gid
    return 1000, 1000


def image_has_correct_uid(tag: str, host_uid: int, *, runtime: str = "podman") -> bool:
    """Return True if the image at ``tag`` was built with ``host_uid``.

    Runs ``<runtime> inspect --format '{{ index .Config.Labels "<label>" }}'``
    and parses the result. Returns False on any subprocess error
    (image missing, runtime missing, label absent) so callers can
    fall through to the rebuild path without distinguishing "image
    missing" from "wrong UID".

    The ``subprocess.run`` call uses ``check=False`` and reads
    stdout/stderr — we don't want a missing image to surface as a
    noisy ``CalledProcessError`` in the user's log; the rebuild
    path will produce a clearer error if it itself fails.
    """
    fmt = f'{{{{ index .Config.Labels "{CLAUDE_UID_LABEL}" }}}}'
    proc = subprocess.run(
        [runtime, "inspect", "--format", fmt, tag],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    raw = (proc.stdout or "").strip()
    # An empty result means the label is absent (image predates the
    # label scheme). Treat as wrong-UID so the rebuild path stamps
    # the label onto the new image.
    if not raw:
        return False
    try:
        baked = int(raw)
    except ValueError:
        # Label was set to a non-integer (e.g. an interpolated build
        # arg gone wrong). Treat as wrong-UID and rebuild.
        return False
    return baked == host_uid


def rebuild_image_for_uid(
    host_uid: int,
    host_gid: int,
    *,
    runtime: str = "podman",
) -> str:
    """Run ``podman build`` to bake ``host_uid`` into a fresh image.

    Returns the resolved image tag on success. Raises
    :class:`FileNotFoundError` if the bundled Dockerfile can't be
    found (e.g. installed package is missing the resource) and
    :class:`subprocess.CalledProcessError` if the build itself
    fails — the caller is expected to surface the captured stderr
    to the user.

    Network is required: the Dockerfile runs ``npm install`` for
    the Anthropic Claude Code CLI. On air-gapped hosts the build
    fails; users can opt out via
    ``GlobalSettings.claude_image_auto_rebuild = False``.
    """
    if host_uid <= 0:
        # Resolve via the same logic that picked the image tag so
        # the build-arg matches what's stamped onto the image.
        host_uid = _FALLBACK_UID_FOR_ROOT
        host_gid = host_gid or _FALLBACK_UID_FOR_ROOT
    tag = resolve_image_tag(host_uid)
    dockerfile = _find_dockerfile()
    build_ctx = _find_build_context()
    cmd: list[str] = [
        runtime,
        "build",
        "-f", str(dockerfile),
        "--build-arg", f"CLAUDE_UID={host_uid}",
        "--build-arg", f"CLAUDE_GID={host_gid}",
        "-t", tag,
        str(build_ctx),
    ]
    logger.info("rebuilding Claude Code image: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Re-raise with the captured stderr so the caller can show
        # a meaningful error in the UI. ``CalledProcessError`` is
        # the natural fit — it carries stdout/stderr/returncode.
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd,
            output=proc.stdout,
            stderr=proc.stderr,
        )
    return tag


def ensure_image_for_current_uid(*, runtime: str = "podman") -> str:
    """One-call wrapper: detect the host UID, build if needed, return tag.

    Idempotent. On a non-POSIX host (``os.getuid`` missing), returns
    the unresolved tag without rebuilding — Windows users get the
    static image they shipped, not a per-UID rebuild dance.

    Returns the tag (whether we rebuilt or the existing image was
    already correct).
    """
    host_uid, host_gid = get_host_uid_gid()
    tag = resolve_image_tag(host_uid)
    if image_has_correct_uid(tag, host_uid, runtime=runtime):
        return tag
    rebuild_image_for_uid(host_uid, host_gid, runtime=runtime)
    return tag


# ---- Resource resolution --------------------------------------------------


def _find_dockerfile() -> Path:
    """Resolve the bundled Dockerfile.

    Order: ``importlib.resources`` (works in installed packages)
    first, then fall back to the repo-root copy for development
    workflows where the bundle isn't present.

    Raises ``FileNotFoundError`` if neither is found — the caller
    surfaces this as a friendly "Dockerfile missing" message.
    """
    try:
        # ``importlib.resources.files`` returns a ``Traversable``;
        # ``as_file`` gives a real path inside a context manager.
        # For a long-lived builder we materialise the file to a
        # stable location (``_BUNDLED_DOCKERFILE`` next to the
        # package) so ``podman build -f`` can read it after the
        # context exits. The materialisation is cheap (one read).
        traversable = importlib.resources.files(
            "src.ui.claude.resources"
        ).joinpath(_BUNDLED_DOCKERFILE)
        with importlib.resources.as_file(traversable) as p:
            if p.is_file():
                return Path(p)
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    # Fall back to the repo-root copy (development workflow).
    repo_root = _find_repo_root()
    if repo_root is not None:
        candidate = repo_root / "Dockerfile.claude-code"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Dockerfile.claude-code not found in the installed package "
        "(src.ui.claude.resources) or at the repo root. Reinstall "
        "SharkDrown or check that the package was built with the "
        "bundled resources."
    )


def _find_build_context() -> Path:
    """Resolve the build context directory.

    The bundled Dockerfile does not COPY any host files — the
    context can be a near-empty directory. We default to the
    directory holding the bundled Dockerfile (``src/ui/claude/
    resources/``) so the build works in installed packages.

    Falls back to the repo root in development workflows where
    the bundle isn't present, mirroring ``_find_dockerfile``.
    """
    try:
        traversable = importlib.resources.files("src.ui.claude.resources")
        with importlib.resources.as_file(traversable) as p:
            if p.is_dir():
                return Path(p)
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    repo_root = _find_repo_root()
    if repo_root is not None:
        return repo_root
    # Last resort: cwd. ``podman build`` accepts any directory;
    # the Dockerfile doesn't need a populated context.
    return Path(os.getcwd())


def _find_repo_root() -> Path | None:
    """Walk up from this file looking for a repo-root sentinel.

    Used as a fallback when the bundled resources aren't
    available. The sentinel is the ``Dockerfile.proxy`` file at
    the repo root — its presence indicates a developer checkout
    rather than an installed wheel.
    """
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "Dockerfile.proxy").is_file():
            return parent
    return None