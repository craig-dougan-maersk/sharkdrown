"""Backing store for global Claude Code settings (shared across profiles).

Three categories of state live in this single JSON file:

- **General** — bind address, proxy port, config_dir, default session CWD
  (formerly per-profile on the old ``ProfileEditorDialog`` General tab;
  now truly global so every profile routes through the same proxy).
- **Providers** — the named :class:`Provider` registry. Endpoints on every
  profile reference providers by name; the catalog + URL + ``key_ref``
  live here once.
- **CLAUDE.md path** — read-only. The file lives at the user's literal
  ``~/.claude/CLAUDE.md``; this store just remembers *where* it is so the
  global settings dialog can show the resolved path.

Persistence mirrors :class:`src.profiles.store.ProfileStore`: atomic
``tempfile.mkstemp`` + ``os.replace`` + ``os.fchmod(fd, 0o600)`` on POSIX,
RLock around reads and writes (re-entrant because save() calls into
load() to preserve active selections).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

_IS_POSIX = sys.platform != "win32"


@dataclass
class GlobalSettings:
    """Truly-global Claude Code settings (per-user, not per-profile).

    Attributes
    ----------
    bind_address:
        Proxy bind address. ``"127.0.0.1"`` (loopback) by default —
        ``"0.0.0.0"`` is opted-in to LAN exposure (typically when a
        Docker session needs to reach the proxy via ``host.docker.internal``).
    port:
        Proxy TCP port. ``0`` means "let the OS pick a free port".
    config_dir:
        Proxy config dir (where ``config.yaml`` / ``.env`` live).
        Empty string means "auto-discover".
    session_cwd:
        Default working directory for Claude Code sessions. ``None``
        means "inherit GUI process cwd".
    providers:
        Named :class:`Provider` registry shared by every profile.
    container_runtime:
        Path to the container runtime binary used for Claude Code
        sessions and (optionally) the proxy. Defaults to ``"docker"``
        (resolved via ``$PATH``); set to ``"podman"`` for a Podman
        install, or an absolute path like
        ``"/usr/local/bin/docker"`` when the binary lives outside
        ``$PATH``. Empty string falls back to ``"docker"``.
    default_claude_image:
        Default container image used for Claude Code sessions
        when a profile's ``container_image`` is empty. The
        companion Dockerfile at the repo root builds this image
        locally (``docker build -f Dockerfile.claude-code -t
        claude-code:latest .``).
    proxy_image:
        Container image used when ``run_proxy_in_container`` is
        True. The companion ``Dockerfile.proxy`` builds this image
        locally (``docker build -f Dockerfile.proxy -t
        sharkdrown-proxy:latest .``).
    run_proxy_in_container:
        When True, the embedded FastAPI proxy runs inside a
        container spawned via the configured runtime, instead of
        the local ``ProxyServerThread``. Falls back to local if
        the container can't be started.
    tui_mode:
        Controls Claude Code's TUI rendering mode for new sessions.

        - ``"fullscreen"`` (default) — sets ``CLAUDE_CODE_NO_FLICKER=1``,
          which makes Claude Code enter its fullscreen / alt-screen
          flicker-free rendering path. This matches the upstream
          ``/tui fullscreen`` slash command and the historic default
          for SharkDrown sessions.
        - ``"default"`` — leaves Claude Code in its standard
          non-fullscreen mode; equivalent to running
          ``/tui default`` (a documented Claude Code slash command
          that disables the alt-screen rendering path).
        - ``"off"`` — adds no env var, identical to ``"default"``;
          kept as an alias because ``defaults.env`` shipped the
          flicker-free flag for several releases and users may want
          to disable it without explicit knowledge of the slash
          command.

        The override is applied per-session in
        :func:`src.profiles.env_builder.build_claude_session_env`
        after ``defaults.env`` is merged, so a user who picks
        ``"default"`` doesn't need to hand-edit ``defaults.env`` to
        escape fullscreen mode (Bug A — user reported that ``/tui``
        "seems to be fullscreen instead of default" before this
        setting existed).
    """

    bind_address: str = "127.0.0.1"
    port: int = 3001
    config_dir: str = ""
    session_cwd: str | None = None
    providers: dict = field(default_factory=dict)
    container_runtime: str = "docker"
    default_claude_image: str = "claude-code:latest"
    proxy_image: str = "sharkdrown-proxy:latest"
    run_proxy_in_container: bool = False
    # When True (default), the launcher rebuilds the Claude Code
    # image with ``--build-arg CLAUDE_UID=$(id -u)`` if the cached
    # image's baked-in UID doesn't match the host user. Set to
    # False on air-gapped hosts where ``npm install`` can't run at
    # build time — the launcher then uses whatever tag
    # ``default_claude_image`` resolves to without rebuilding.
    # See ``src/ui/claude/docker_image_builder.py`` for the full
    # rationale and tag scheme.
    claude_image_auto_rebuild: bool = True
    # ``"fullscreen"`` (legacy default — keeps the historic
    # flicker-free rendering), ``"default"`` (uses Claude Code's
    # own default non-fullscreen mode), or ``"off"`` (alias of
    # ``"default"``).
    tui_mode: str = "fullscreen"


GLOBAL_SETTINGS_FILENAME = "claude_global.json"
GLOBAL_CLAUDE_MD_PATH = Path.home() / ".claude" / "CLAUDE.md"


class GlobalSettingsStore:
    """CRUD facade over ``<data_dir>/claude_global.json``.

    Same atomic-write + chmod 600 + ``threading.RLock`` pattern as
    :class:`ProfileStore`. Re-entrant because ``save()`` may call
    ``_load_unlocked()`` to preserve existing fields on partial writes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> GlobalSettings:
        """Read global settings from disk.

        Returns dataclass defaults on first launch (file missing) or
        on corrupt JSON — same fall-through as :class:`ProfileStore.load`.
        """
        with self._lock:
            data = self._read_raw()
            if data is None:
                return GlobalSettings()
            return _deserialize(data)

    def save(self, settings: GlobalSettings) -> None:
        """Persist the full ``GlobalSettings`` record atomically."""
        with self._lock:
            payload = _serialize(settings)
            self._save_raw(payload)

    def _read_raw(self) -> dict | None:
        if not self._path.is_file():
            return None
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _save_raw(self, data: dict) -> None:
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            suffix=".tmp",
            prefix=self._path.stem + "_",
        )
        tmp = Path(tmp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                if _IS_POSIX:
                    fh.flush()
                    os.fsync(fh.fileno())
                    os.fchmod(fh.fileno(), 0o600)
            os.replace(tmp, self._path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


def _serialize(s: GlobalSettings) -> dict:
    out: dict = {
        "schema_version": 1,
        "bind_address": s.bind_address,
        "port": s.port,
        "config_dir": s.config_dir,
        "session_cwd": s.session_cwd,
        "providers": _serialize_providers(s.providers),
        "container_runtime": s.container_runtime,
        "default_claude_image": s.default_claude_image,
        "proxy_image": s.proxy_image,
        "run_proxy_in_container": s.run_proxy_in_container,
        "claude_image_auto_rebuild": s.claude_image_auto_rebuild,
        "tui_mode": s.tui_mode,
    }
    # Drop empty optional fields to keep the file diff clean.
    if not s.session_cwd:
        out.pop("session_cwd", None)
    if not s.config_dir:
        out.pop("config_dir", None)
    if s.bind_address == "127.0.0.1":
        out.pop("bind_address", None)
    if s.port == 3001:
        out.pop("port", None)
    if s.container_runtime == "docker":
        out.pop("container_runtime", None)
    if s.default_claude_image == "claude-code:latest":
        out.pop("default_claude_image", None)
    if s.proxy_image == "sharkdrown-proxy:latest":
        out.pop("proxy_image", None)
    if not s.run_proxy_in_container:
        out.pop("run_proxy_in_container", None)
    if s.claude_image_auto_rebuild:
        # Default — drop to keep the file diff clean.
        out.pop("claude_image_auto_rebuild", None)
    if s.tui_mode == "fullscreen":
        # Legacy default — drop to keep the file diff clean for
        # existing installs that haven't explicitly opted out of
        # the flicker-free rendering.
        out.pop("tui_mode", None)
    return out


def _serialize_providers(providers: dict) -> dict:
    out: dict = {}
    for name, prov in providers.items():
        if not isinstance(prov, dict):
            continue
        prov_d = {k: v for k, v in prov.items() if v is not None and v != ()}
        if prov_d.get("url"):
            out[str(name)] = prov_d
    return out


def _deserialize(data: dict) -> GlobalSettings:
    bind = str(data.get("bind_address") or "127.0.0.1") or "127.0.0.1"
    try:
        port = int(data.get("port", 3001) or 3001)
    except (TypeError, ValueError):
        port = 3001
    config_dir = str(data.get("config_dir") or "")
    cwd_raw = data.get("session_cwd")
    session_cwd = str(cwd_raw) if cwd_raw is not None else None
    providers_raw = data.get("providers") or {}
    providers: dict = {}
    if isinstance(providers_raw, dict):
        for name, raw in providers_raw.items():
            if not isinstance(raw, dict):
                continue
            if not raw.get("url"):
                continue
            cleaned = {
                k: v for k, v in raw.items() if v is not None and v != ()
            }
            providers[str(name)] = cleaned
    runtime = str(data.get("container_runtime") or "docker") or "docker"
    claude_img = (
        str(data.get("default_claude_image") or "claude-code:latest")
        or "claude-code:latest"
    )
    proxy_img = (
        str(data.get("proxy_image") or "sharkdrown-proxy:latest")
        or "sharkdrown-proxy:latest"
    )
    run_proxy_in_container = bool(data.get("run_proxy_in_container", False))
    claude_image_auto_rebuild = bool(data.get("claude_image_auto_rebuild", True))
    # ``tui_mode`` is new in this version; pre-existing files lack
    # the key and should keep the legacy default ("fullscreen").
    # Validate the value to defend against typos in hand-edited
    # ``claude_global.json`` — anything unrecognised falls back to
    # the safe legacy default rather than disabling the env-var
    # override entirely.
    raw_tui = str(data.get("tui_mode", "fullscreen") or "fullscreen")
    tui_mode = raw_tui if raw_tui in {"fullscreen", "default", "off"} else "fullscreen"
    return GlobalSettings(
        bind_address=bind,
        port=port,
        config_dir=config_dir,
        session_cwd=session_cwd,
        providers=providers,
        container_runtime=runtime,
        default_claude_image=claude_img,
        proxy_image=proxy_img,
        run_proxy_in_container=run_proxy_in_container,
        claude_image_auto_rebuild=claude_image_auto_rebuild,
        tui_mode=tui_mode,
    )