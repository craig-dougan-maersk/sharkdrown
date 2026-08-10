"""Proxy lifecycle mixin — start/stop/restart and status callbacks."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import yaml
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from src.proxy.event_bridge import EventBusBridge
from src.proxy.paths import data_dir, default_db_path
from src.proxy.server_thread import ProxyServerThread

logger = logging.getLogger(__name__)


# Suffix appended to non-Claude aliases to give them an ``id`` that
# passes Claude Code's ``/v1/models`` picker filter
# (``id`` contains ``claude`` or ``anthropic``, case-insensitive).
# Public so tests can assert against it without re-deriving the
# constant from a magic string.
SYNTHETIC_ALIAS_SUFFIX = "-non-anthropic"


def _passes_claude_picker_filter(alias: str) -> bool:
    """True when ``alias`` would survive Claude Code's picker filter.

    Mirrors the filter documented at
    https://code.claude.com/docs/en/llm-gateway-protocol:

        Claude Code keeps an entry when its ``id`` contains
        ``claude`` or ``anthropic`` anywhere in the string, matched
        case-insensitively.

    Anything that fails this filter is silently dropped from the
    picker's auto-discovered list. We replicate the same case-fold
    here so the augmentation only adds synthetic ids for the
    aliases that actually need them.
    """
    lowered = alias.lower()
    return "claude" in lowered or "anthropic" in lowered


def augment_proxy_aliases_for_discovery(
    proxy_aliases: dict[str, str],
) -> None:
    """Augment ``proxy_aliases`` in-place with synthetic ids.

    For each (alias, real) where ``alias`` would FAIL Claude Code's
    picker filter (no ``claude`` / ``anthropic`` substring), add a
    companion entry ``<alias>-non-anthropic -> real``. The
    companion id passes the picker filter; ``/v1/models`` emits it
    with ``display_name = <alias>`` so the user sees their typed-in
    label in the menu.

    User-facing state (``profiles.json``, the profile editor UI) is
    NOT modified — only the proxy's runtime view is widened. The
    augmentation is purely additive: any user-defined key
    (``minimax``, ``claude-alias``, ``minimax-non-anthropic`` from
    a power user) is preserved verbatim.

    Edge cases:
    - claude / anthropic substring check is case-insensitive.
    - If the user already defined ``<alias>-non-anthropic`` as a
      real alias, the augmentation is skipped for that alias
      (the user's explicit key wins).
    - ``list(proxy_aliases.items())`` snapshots the dict so the
      mid-loop mutation doesn't trip ``RuntimeError``.
    """
    for alias, real in list(proxy_aliases.items()):
        if _passes_claude_picker_filter(alias):
            continue
        synthetic = f"{alias}{SYNTHETIC_ALIAS_SUFFIX}"
        if synthetic not in proxy_aliases:
            proxy_aliases[synthetic] = real


class ProxyLifecycleMixin:
    """Manages the embedded FastAPI proxy thread lifecycle."""

    # ------------------------------------------------------------------
    # Embedded proxy control
    # ------------------------------------------------------------------
    def _start_server(self) -> None:
        """Start (or restart) the embedded proxy on its own QThread.

        Safe to call when the thread is already running — the early return
        prevents a double-start. The thread's bind address, port, config
        dir, and provider registry are read from the **global settings**
        (post-restructuring — Claude > Settings). The active profile
        contributes per-profile ``model_endpoints`` and aliases; the
        proxy merges both into its routing tables.
        """
        if self.proxy_thread is not None and self.proxy_thread.isRunning():
            return

        active = self._profile_store().get_active() or self._profile_store().ensure_default()
        # Global state (bind / port / config_dir / providers) lives in
        # the global settings store. Falls back to the dataclass
        # defaults when the file is missing.
        global_settings = self._global_settings_store().load()
        host = global_settings.bind_address or "127.0.0.1"
        # ---- Auto-rebind to 0.0.0.0 for container-mode sessions -------
        # When the active profile runs Claude Code inside a container,
        # the container reaches the host proxy via
        # ``host.docker.internal`` (which resolves to the host's
        # external interface on Linux, NOT loopback). The proxy must
        # be listening on a non-loopback interface for that to work —
        # otherwise the container connects to ``host.docker.internal``
        # but the kernel rejects the SYN because nothing is bound to
        # that IP locally, and Claude Code surfaces *"Invalid API
        # key"* because no request ever reaches us.
        #
        # We honour an explicit ``bind_address`` set by the user, but
        # fall back to ``0.0.0.0`` automatically when container mode
        # is in use. ``run_in_container`` defaults to True on every
        # profile (per the container-mode migration plan), so this
        # rebind happens for most users by default.
        if (
            host in ("", "127.0.0.1", "localhost")
            and getattr(active, "run_in_container", True)
        ):
            host = "0.0.0.0"
        port = int(global_settings.port or 3001)
        config_dir_path = (
            Path(global_settings.config_dir)
            if global_settings.config_dir
            else None
        )
        default_endpoint = ""
        if isinstance(active.claude_settings, dict):
            default_endpoint = str(
                active.claude_settings.get("proxy_default_endpoint", "") or ""
            )

        # ---- Build proxy-side inputs ---------------------------------
        # Providers come from the global registry, not the active
        # profile. Endpoints + aliases still come from the active
        # profile so each profile can route to different providers
        # for different models.
        proxy_providers: dict[str, dict] = {}
        providers_map: dict[str, object] = dict(global_settings.providers or {})
        credential_store = self._profile_store().credential_store
        for name, prov in providers_map.items():
            if isinstance(prov, dict):
                key_ref = prov.get("key_ref")
                url = str(prov.get("url") or "")
                available_models = list(prov.get("available_models") or ())
            else:
                key_ref = getattr(prov, "key_ref", None)
                url = str(getattr(prov, "url", "") or "")
                available_models = list(
                    getattr(prov, "available_models", ()) or ()
                )
            api_key: str | None = None
            if isinstance(key_ref, str) and key_ref:
                api_key = credential_store.get(key_ref)
            proxy_providers[str(name)] = {
                "url": url,
                "api_key": api_key,
                "available_models": available_models,
            }

        proxy_endpoints: dict[str, str] = {}
        proxy_aliases: dict[str, str] = {}
        for model, ep in (active.model_endpoints or {}).items():
            provider_name = ""
            if isinstance(ep, str):
                provider_name = "default"
            else:
                provider_name = str(getattr(ep, "provider_name", "") or "")
            if provider_name and provider_name in proxy_providers:
                proxy_endpoints[str(model)] = provider_name
            elif provider_name:
                pass
            if not isinstance(ep, str):
                for alias in (getattr(ep, "aliases", ()) or ()):
                    if isinstance(alias, str) and alias.strip():
                        proxy_aliases[alias.strip()] = str(model)

        # Augment the alias map with synthetic ids for non-Claude
        # aliases so Claude Code's gateway auto-discovery can surface
        # them. See ``augment_proxy_aliases_for_discovery`` for the
        # full rationale and edge-case handling.
        augment_proxy_aliases_for_discovery(proxy_aliases)

        self.proxy_host = host
        # Only commit the port when it's a known value. When port=0
        # (OS-assigned), proxy_port stays 0 until _on_proxy_started
        # delivers the actual bound port — _proxy_base_url() returns ""
        # while proxy_port is 0, preventing sessions from launching
        # with an unresolved URL.
        if port != 0:
            self.proxy_port = port

        if port == 0:
            self.proxy_status_label.setText(
                f"Proxy: starting on {host} (port pending)…"
            )
        else:
            self.proxy_status_label.setText(
                f"Proxy: starting on http://{host}:{port}…"
            )
        self.action_start_proxy.setEnabled(False)
        self.action_stop_proxy.setEnabled(True)

        # Honour ``run_proxy_in_container``: when set, the proxy
        # runs in a published container instead of a local
        # ``QThread``. Falls back to the local thread if the
        # container can't be started — never silently fails.
        global_settings = self._global_settings_store().load()
        if global_settings.run_proxy_in_container:
            try:
                from src.ui.claude.docker_proxy import (
                    start_proxy_container,
                    wait_for_proxy_ready,
                    get_container_logs,
                )

                handle = start_proxy_container(
                    global_settings,
                    bind_port=port or 3001,
                    extra_env={
                        # Point the container's uvicorn at the
                        # config.yaml we just wrote — see
                        # ``_write_proxy_config_yaml`` below. Without
                        # this the container falls back to an empty
                        # ``./config.yaml`` and routing resolves to
                        # the hardcoded default ``anthropic`` provider
                        # (api.anthropic.com) regardless of what the
                        # active profile is configured for, which
                        # silently breaks any non-Anthropic setup
                        # (MiniMax, OpenRouter, …).
                        "PROXY_CONFIG_YAML": "/etc/sharkdrown/config.yaml",
                    },
                )
                # Persist the per-profile routing config the local
                # thread would have passed via constructor kwargs.
                # The container reads ``PROXY_CONFIG_YAML`` from
                # env (see :class:`YamlSettingsSource`) and uses it
                # instead of the CWD-relative default. The file
                # lands at ``/etc/sharkdrown/config.yaml`` via
                # the bind-mount that ``start_proxy_container``
                # already wires.
                self._write_proxy_config_yaml(
                    proxy_endpoints, proxy_aliases, proxy_providers
                )
                self.proxy_status_label.setText(
                    f"Proxy: container {handle.container_name} starting…"
                )
                # Block until uvicorn has bound and /health
                # responds. ``docker run -d`` returns as soon
                # as the container exists — uvicorn's import-time
                # work (DB migrations, schema validation) can take
                # several seconds, and the rest of the app
                # immediately tries to reach the proxy on
                # 127.0.0.1:<port>. Without this wait the user
                # sees "cannot connect to the proxy" while the
                # container is still booting.
                ready = wait_for_proxy_ready(
                    self.proxy_host, handle.host_port
                )
                if not ready:
                    # Surface the container's stdout in the
                    # status bar so the user can see *why* the
                    # proxy didn't come up (uvicorn import error,
                    # missing config, DB migration failure).
                    logs = get_container_logs(global_settings)
                    tail = logs.strip().splitlines()[-5:]
                    snippet = (
                        " | ".join(line.strip() for line in tail)
                        if tail
                        else "(no logs)"
                    )
                    self.proxy_status_label.setText(
                        f"Proxy: container failed to start — {snippet}"
                    )
                    logger.warning(
                        "proxy container did not become ready; "
                        "falling back to local QThread. Last logs: %s",
                        snippet,
                    )
                    # Drop into the local-thread fallback below.
                else:
                    self.proxy_port = handle.host_port
                    self.proxy_status_label.setText(
                        f"Proxy: container {handle.container_name} on "
                        f"http://{self.proxy_host}:{handle.host_port}"
                    )
                    # Mark as "running" so the UI enables the
                    # downstream consumers (dashboard, sessions,
                    # browser button). We don't have a thread
                    # object to track — ``stop_server`` routes
                    # through ``_stop_proxy_container`` instead.
                    self.proxy_thread = None
                    self._on_proxy_started(handle.host_port)
                    return
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                logger.warning(
                    "could not start proxy container (%s); "
                    "falling back to local QThread", exc
                )
                self.proxy_status_label.setText(
                    f"Proxy: container start failed ({exc}); "
                    "falling back to local…"
                )

        thread = ProxyServerThread(
            host=host,
            port=port,
            storage_path=default_db_path(),
            config_dir=config_dir_path,
            model_endpoints=proxy_endpoints,
            default_endpoint=default_endpoint or None,
            model_aliases=proxy_aliases,
            providers_cfg=proxy_providers,
            parent=self,
        )
        thread.started_ok.connect(self._on_proxy_started)
        thread.failed.connect(self._on_proxy_failed)
        thread.stopped.connect(self._on_proxy_stopped)
        self.proxy_thread = thread
        thread.start()

    def _stop_server(self) -> None:
        """Ask the proxy thread (or container) to shut down.

        Routes to the right path based on which is running.
        When the proxy is in a container (``run_proxy_in_container``),
        ``self.proxy_thread`` is ``None`` and we call
        ``stop_proxy_container``; otherwise we route to the
        ``QThread.request_stop`` path.
        """
        if self.proxy_thread is None:
            # Possibly running in a container.
            try:
                from src.ui.claude.docker_proxy import stop_proxy_container

                stop_proxy_container(self._global_settings_store().load())
                self.proxy_status_label.setText("Proxy: container stopped.")
            except ImportError:
                pass
            return
        self.proxy_status_label.setText("Proxy: stopping…")
        self.action_stop_proxy.setEnabled(False)
        self.proxy_thread.request_stop()

    def _on_proxy_started(self, port: int) -> None:
        """Called by the worker thread once uvicorn is listening."""
        self.proxy_port = port
        self.proxy_status_label.setText(f"Proxy: running on {self.proxy_host}:{port}")
        self.action_start_proxy.setEnabled(False)
        self.action_stop_proxy.setEnabled(True)
        self.action_show_dashboard.setEnabled(True)
        self.action_show_recent.setEnabled(True)
        self.action_open_browser.setEnabled(True)
        self.action_new_claude_session.setEnabled(True)
        if self.activity_tile is not None:
            self.activity_tile.set_base_url(f"http://{self.proxy_host}:{port}")
        if self.claude_container is not None:
            self.claude_container.set_base_url(f"http://{self.proxy_host}:{port}")
        # Auto-show the Dashboard on every successful proxy start.
        # The dashboard is the most useful first view after the
        # proxy comes up — it surfaces live request captures and
        # provider health. The user can still toggle it off via
        # ``Claude → Show Dashboard`` after that. Must come *after*
        # ``set_base_url`` so the (lazy-built) ``DashboardView`` has
        # a URL to point at before we focus its inner tab.
        if not self.action_show_dashboard.isChecked():
            self.action_show_dashboard.setChecked(True)
            # ``setChecked`` only updates the action's internal
            # state — the ``triggered`` signal doesn't fire on
            # programmatic changes, so we invoke the slot directly
            # to update the inner-tab visibility.
            self._show_dashboard_panel(True)
        self.status.showMessage(
            f"Proxy ready — point Claude Code at http://{self.proxy_host}:{port}",
            5000,
        )
        if self.claude_container is not None:
            self.claude_container.flush_pending_launch()

        # Start the EventBus bridge for push-based UI updates.
        self._start_event_bridge()

    def _on_proxy_failed(self, msg: str) -> None:
        """Called when the thread or uvicorn fails to start."""
        self.proxy_status_label.setText("Proxy: failed")
        self.action_start_proxy.setEnabled(True)
        self.action_stop_proxy.setEnabled(False)
        self.action_show_dashboard.setEnabled(False)
        self.action_show_recent.setEnabled(False)
        self.action_open_browser.setEnabled(False)
        self.action_new_claude_session.setEnabled(False)
        QMessageBox.critical(self, "Proxy Error", msg)

    def _on_proxy_stopped(self) -> None:
        """Called after the lifespan has fully torn down."""
        self._stop_event_bridge()
        self.proxy_status_label.setText("Proxy: stopped")
        self.action_start_proxy.setEnabled(True)
        self.action_stop_proxy.setEnabled(False)
        self.action_show_dashboard.setEnabled(False)
        self.action_show_recent.setEnabled(False)
        self.action_open_browser.setEnabled(False)
        self.action_new_claude_session.setEnabled(False)
        if self.activity_tile is not None:
            self.activity_tile.stop()
        if self.claude_container is not None:
            self.claude_container.stop_polling()
        self.proxy_thread = None
        if getattr(self, "_restart_after_stop", False):
            self._restart_after_stop = False
            QTimer.singleShot(0, self._start_server)

    def _focus_proxy_tab(self) -> None:
        """Bring the Claude tab to the front and switch to the inner
        Dashboard sub-tab.
        """
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)
        if self.claude_container is not None:
            self.claude_container.show_dashboard()

    def _show_recent_activity(self, checked: bool) -> None:
        """Slot for Claude -> Show Recent Activity (checkable).

        Toggling the menu item shows/hides the Recent Activity inner
        tab AND makes the Claude tab the active top-level view. When
        the box is checked, the recent-activity pane becomes visible
        and selected; when unchecked, the pane is hidden and the
        tab strip falls forward to the next visible tab.
        """
        if self.claude_container is not None:
            self.claude_container.set_recent_activity_visible(checked)
        if checked:
            self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)
            if self.claude_container is not None:
                self.claude_container.show_recent_activity()
        self.action_show_recent.setChecked(checked)

    def _show_dashboard_panel(self, checked: bool) -> None:
        """Slot for Claude -> Show Dashboard (checkable)."""
        if self.claude_container is not None:
            self.claude_container.set_dashboard_visible(checked)
        if checked:
            self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)
            if self.claude_container is not None:
                self.claude_container.show_dashboard()
        self.action_show_dashboard.setChecked(checked)

    def _open_dashboard_external(self) -> None:
        """Open the dashboard in the user's default web browser."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        url = f"http://{self.proxy_host}:{self.proxy_port}/"
        QDesktopServices.openUrl(QUrl(url))

    def _is_proxy_running(self) -> bool:
        """True iff the proxy is currently serving requests.

        Two paths can run the proxy:

        - **Local QThread** (``self.proxy_thread is not None and
          self.proxy_thread.isRunning()``) — used when
          ``run_proxy_in_container=False``.
        - **Container** (``sharkdrown-proxy`` container is up) —
          used when ``run_proxy_in_container=True``. In this
          mode there's no ``proxy_thread``; the proxy lives in
          its own podman container and the GUI talks to it via
          the published port.

        Without the container check, the proxy-gate in
        ``ClaudeContainerView.new_session`` would *always* think
        the proxy is down when ``run_proxy_in_container=True``,
        which would (a) make ``_proxy_base_url()`` return ""
        even when the proxy IS up, and (b) queue the launch and
        ask the main window to start a proxy that's already
        running. Result: ``ANTHROPIC_BASE_URL`` is empty in the
        spawned Claude container, Claude Code can't reach the
        proxy, surfaces *"Invalid API key"*.
        """
        if self.proxy_thread is not None and self.proxy_thread.isRunning():
            return True
        # Container-mode proxy: ask podman whether the named
        # container is up. Cheaper than an HTTP probe; only
        # consulted on the proxy-gate / URL-builder paths, not
        # in tight loops.
        try:
            from src.ui.claude.docker_proxy import is_proxy_container_running
            from src.profiles.global_settings import GlobalSettings

            settings = GlobalSettings(
                container_runtime=self._proxy_container_runtime()
            )
            return is_proxy_container_running(settings, "sharkdrown-proxy")
        except (ImportError, FileNotFoundError, OSError):
            return False

    def _write_proxy_config_yaml(
        self,
        proxy_endpoints: dict[str, str],
        proxy_aliases: dict[str, str],
        proxy_providers: dict[str, dict],
    ) -> None:
        """Persist the per-profile proxy routing config to ``config.yaml``.

        Called from the ``run_proxy_in_container`` branch of
        :meth:`_start_server` before the container is spawned.
        Without this, the container-mode proxy has no aliases,
        endpoints, or providers — every request lands on the
        hardcoded default ``anthropic`` provider (api.anthropic.com)
        and Claude Code surfaces *"Invalid API key"* or
        *"model not found"* for any non-Anthropic setup.

        The local-thread path passes this data via the
        ``ProxyServerThread`` constructor; the container path has
        no such hook, so we materialise it to a YAML file the
        container reads via the ``PROXY_CONFIG_YAML`` env var
        (see :class:`YamlSettingsSource`).

        Atomic-write semantics: write to a sibling temp file and
        ``os.replace`` into place. ``chmod 600`` — the file
        contains plaintext API keys resolved from
        :class:`CredentialStore`, so it must not be world-readable.
        """
        config_payload = {
            "model_endpoints": {
                "endpoints": dict(proxy_endpoints),
                "model_aliases": dict(proxy_aliases),
                "providers": dict(proxy_providers),
            }
        }
        yaml_bytes = yaml.safe_dump(
            config_payload,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8")
        config_path = data_dir() / "config.yaml"

        # Atomic-write: temp file in the same directory, chmod
        # 600 *before* close (avoids the chmod-race window
        # documented in ``ProfileStore``), then ``os.replace``
        # so readers never see a half-written file.
        fd, tmp_path = tempfile.mkstemp(
            dir=str(config_path.parent),
            prefix=".config.yaml.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb", closefd=True) as fh:
                fh.write(yaml_bytes)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, config_path)
        except OSError:
            # Best-effort cleanup on failure — don't leave temp
            # files lying around. The caller (a user-visible start
            # operation) will surface the actual container-spawn
            # failure with a clearer message.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _proxy_container_runtime(self) -> str:
        """Resolve ``container_runtime`` from global settings.

        Falls back to ``"podman"`` (the default in
        :class:`GlobalSettings`) when the settings store can't be
        read. Kept separate from ``_is_proxy_running`` so the
        runtime resolution is one-line-callable from elsewhere.
        """
        try:
            return str(
                self._global_settings_store().load().container_runtime or "podman"
            )
        except (OSError, ValueError):
            return "podman"

    def _proxy_base_url(self) -> str:
        """URL Claude Code sessions should use. Empty if proxy isn't up.

        When ``run_proxy_in_container=True``, the proxy runs in
        its own podman container and the URL is always
        ``http://127.0.0.1:<port>`` — the published port on the
        host loopback, which is how Claude Code sessions (running
        on the host or in their own containers) reach it.
        ``host.docker.internal`` rewriting for in-container
        Claude sessions happens later in
        :func:`env_builder.build_claude_session_env` when
        ``container_aware=True``.
        """
        if not self._is_proxy_running() or self.proxy_port == 0:
            return ""
        return f"http://{self.proxy_host}:{self.proxy_port}"

    def _request_proxy_start(self) -> None:
        """Start the proxy if not already running."""
        if not self._is_proxy_running():
            self._start_server()

    # ------------------------------------------------------------------
    # EventBus bridge
    # ------------------------------------------------------------------
    def _start_event_bridge(self) -> None:
        """Wire up the EventBusBridge to the proxy's EventBus."""
        thread = self.proxy_thread
        if thread is None:
            return
        event_bus = thread.event_bus
        if event_bus is None:
            return

        bridge = EventBusBridge(parent=self)
        if self.activity_tile is not None:
            bridge.request_created.connect(self.activity_tile.on_request_created)
            bridge.response_complete.connect(self.activity_tile.on_response_complete)
        bridge.start(event_bus)
        self._event_bridge = bridge

    def _stop_event_bridge(self) -> None:
        """Shut down the EventBusBridge cleanly."""
        bridge = getattr(self, "_event_bridge", None)
        if bridge is not None:
            bridge.stop()
            self._event_bridge = None
