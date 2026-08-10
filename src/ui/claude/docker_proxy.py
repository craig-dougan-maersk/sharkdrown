"""Containerised proxy launcher.

When ``Claude > Settings > Container Runtime > Run proxy in
container`` is enabled, the embedded FastAPI proxy is spawned
inside a per-app container instead of the local
``ProxyServerThread``. This module provides the spawn / supervise
machinery; the lifecycle in
``src/ui/main_window/_proxy_lifecycle.py`` calls into it.

Architecture
------------
A long-running container (``sharkdrown-proxy:latest`` by default)
hosts the proxy. We publish the proxy's port to the host with
``docker run -p <port>:<port> ...``. The host's Claude Code
sessions connect via the published port — either
``127.0.0.1:<port>`` (when the container publishes to the host's
loopback) or ``host.docker.internal:<port>`` (when Claude
sessions are themselves containerised and need the docker
bridge).

The proxy inside the container reads its config from
``/etc/sharkdrown/`` (config.yaml, .env, credentials.json) which
the host bind-mounts at launch. This keeps the data flow
identical to the local ``QThread`` mode — the proxy reads the
same files, just from a container path.

Lifecycle
---------
- :func:`start_proxy_container` spawns the container and returns
  the resolved ``(host_port, container_name)`` tuple.
- :func:`wait_for_proxy_ready` polls the ``/health`` endpoint
  until the proxy responds (with a configurable timeout) so the
  GUI doesn't try to connect before uvicorn has bound.
- :func:`stop_proxy_container` sends ``SIGTERM`` (via
  ``docker stop``) and waits for graceful exit.
- :func:`is_proxy_container_running` returns the live state
  (``docker inspect`` — cheap).

The container is *reused* across restarts: we never remove it
unless the user explicitly opts in to a fresh start. The
credentials bind-mount is mounted ``rw`` so the proxy can write
the SQLite capture DB.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

from src.profiles.global_settings import GlobalSettings
from src.proxy.paths import data_dir


logger = logging.getLogger(__name__)

_PROXY_DATA_PATH = "/etc/sharkdrown"

# How long :func:`wait_for_proxy_ready` will keep polling the
# /health endpoint before giving up. 30 s is generous — uvicorn
# imports + DB schema migrations typically take <5 s on a warm
# host. Bumping this only delays the failure surfacing in the
# status bar.
_READY_TIMEOUT_S = 30.0
_READY_POLL_INTERVAL_S = 0.5


class ProxyContainerHandle(NamedTuple):
    """Returned by :func:`start_proxy_container`.

    ``host_port`` is the port published to the host's network
    namespace (the value Claude Code sessions use to reach the
    proxy). ``container_name`` is the docker container's name
    (``sharkdrown-proxy`` by default).
    """

    host_port: int
    container_name: str


def _resolve_runtime(settings: GlobalSettings) -> str:
    """Resolve the container runtime binary from the global settings.

    Empty / unset ``container_runtime`` falls back to
    ``"docker"``; the resolved name is run through ``shutil.which``
    to surface a clean error when the binary is missing.
    """
    name = settings.container_runtime or "docker"
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(
            f"Container runtime {name!r} not found on PATH. "
            "Configure Claude > Settings > Container Runtime."
        )
    return found


def is_proxy_container_running(
    settings: GlobalSettings, container_name: str = "sharkdrown-proxy"
) -> bool:
    """Return True iff the named proxy container is currently up."""
    try:
        runtime = _resolve_runtime(settings)
    except FileNotFoundError:
        return False
    proc = subprocess.run(
        [runtime, "inspect", "--format", "{{.State.Running}}", container_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def start_proxy_container(
    settings: GlobalSettings,
    *,
    bind_port: int,
    container_name: str = "sharkdrown-proxy",
    extra_env: dict[str, str] | None = None,
) -> ProxyContainerHandle:
    """Spawn the proxy container if it's not already running.

    Idempotent — if a container with the given name already
    exists and is up, returns its published port without
    relaunching. Otherwise runs ``<runtime> run`` with:

    - ``-p <bind_port>:<bind_port>`` so the host's loopback can
      reach the proxy on the published port.
    - ``--mount type=bind,source=<data_dir>,target=/etc/sharkdrown``
      so the container sees the same config.yaml / .env /
      credentials.json / ccproxy-requests.db the host's local
      ``ProxyServerThread`` would read.
    - ``--restart unless-stopped`` so a transient host reboot
      doesn't lose the proxy.
    - ``-e KEY=VALUE`` (one per entry in ``extra_env``) so the
      caller can inject per-launch configuration that doesn't
      belong on disk. The desktop GUI uses this to point the
      container at a freshly-written ``config.yaml`` via the
      ``PROXY_CONFIG_YAML`` env var; ``Settings`` reads that var
      in :class:`YamlSettingsSource`.

    Returns ``(host_port, container_name)``. Raises
    :class:`FileNotFoundError` if the runtime binary is missing,
    :class:`subprocess.CalledProcessError` if the spawn fails.

    Callers should chain :func:`wait_for_proxy_ready` before
    assuming the proxy is actually serving requests — the
    container's uvicorn process takes a few seconds to bind,
    and ``docker run -d`` returns as soon as the container
    is created, not when its main process is ready.
    """
    runtime = _resolve_runtime(settings)
    # If a container already exists and is up, reuse it.
    if is_proxy_container_running(settings, container_name):
        return ProxyContainerHandle(
            host_port=bind_port, container_name=container_name
        )
    # Stop + remove any stopped container with the same name so
    # ``docker run`` doesn't fail with "name in use".
    subprocess.run(
        [runtime, "rm", "-f", container_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    host_data = data_dir()
    host_data.mkdir(parents=True, exist_ok=True)
    image = settings.proxy_image or "sharkdrown-proxy:latest"
    cmd: list[str] = [
        runtime,
        "run",
        "-d",  # detached
        "--name", container_name,
        "-p", f"{bind_port}:{bind_port}",
        "--mount",
        f"type=bind,source={host_data},target={_PROXY_DATA_PATH}",
        "--restart", "unless-stopped",
    ]
    # Inject per-launch env vars. ``-e KEY=VALUE`` flags must
    # come BEFORE the image name (podman / docker argv ordering);
    # podman doesn't pass host env into containers by default
    # (same root cause as the Claude-session bug fixed in
    # ``docker_session.py``), so each var needs an explicit flag.
    if extra_env:
        for k, v in extra_env.items():
            cmd.extend(["-e", f"{k}={v}"])
    cmd.append(image)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    return ProxyContainerHandle(
        host_port=bind_port, container_name=container_name
    )


def wait_for_proxy_ready(
    host: str,
    port: int,
    *,
    timeout_s: float = _READY_TIMEOUT_S,
    poll_interval_s: float = _READY_POLL_INTERVAL_S,
) -> bool:
    """Poll the proxy's ``/health`` endpoint until it responds.

    The proxy is considered ready when a TCP connection to the
    published port succeeds AND the ``GET /health`` endpoint
    returns an HTTP 200. We start with a bare TCP probe (faster
    feedback that uvicorn is bound) and then upgrade to the HTTP
    probe — the ``/health`` round-trip also exercises the proxy's
    import-time DB setup, which is the most common failure mode
    for first-launch.

    Returns ``True`` if the proxy became ready within
    ``timeout_s``, ``False`` otherwise. Logs at WARNING level
    on timeout so the status bar can be cross-referenced with
    the proxy container's stdout.
    """
    deadline = time.monotonic() + timeout_s
    health_url = f"http://{host}:{port}/health"
    while time.monotonic() < deadline:
        # Phase 1: TCP probe. uvicorn binds before any middleware
        # / DB migration runs, so a successful TCP connect means
        # the proxy is at least starting up. Faster than HTTP for
        # a tight loop.
        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
        except OSError:
            time.sleep(poll_interval_s)
            continue
        # Phase 2: HTTP probe. The TCP port may be open but the
        # app's startup hooks (DB migrations, schema validation)
        # might still be running.
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(poll_interval_s)
    logger.warning(
        "proxy container did not respond on %s within %.1fs",
        health_url, timeout_s,
    )
    return False


def stop_proxy_container(
    settings: GlobalSettings, container_name: str = "sharkdrown-proxy"
) -> None:
    """Gracefully stop the proxy container (if any).

    Sends ``SIGTERM`` via ``docker stop`` with a 10-second grace
    period. Idempotent — silently no-ops if the container
    doesn't exist.
    """
    try:
        runtime = _resolve_runtime(settings)
    except FileNotFoundError:
        return
    subprocess.run(
        [runtime, "stop", "--time", "10", container_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def get_container_logs(
    settings: GlobalSettings,
    container_name: str = "sharkdrown-proxy",
    *,
    tail: int = 80,
) -> str:
    """Return the last ``tail`` lines of container stdout/stderr.

    Used by the lifecycle to surface the real reason a
    container failed to start (uvicorn import error, DB
    migration failure, etc.) in the status bar / a message box.
    Returns ``""`` if the runtime binary is missing or the
    container doesn't exist.
    """
    try:
        runtime = _resolve_runtime(settings)
    except FileNotFoundError:
        return ""
    proc = subprocess.run(
        [runtime, "logs", "--tail", str(tail), container_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return proc.stdout or ""