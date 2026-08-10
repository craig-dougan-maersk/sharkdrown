"""Docker-backed Claude Code session.

Mirrors :class:`src.ui.terminal.pty_backend.PtySession`'s public
contract (``start_pty()`` / ``start()``, ``shutdown()``, ``data_received``,
``process_exited``) so :class:`src.ui.terminal.terminal_session_view.TerminalSessionView`
can construct either transparently when given a session factory.

Implementation: ``subprocess.Popen`` against ``docker exec -i -t <container>
<argv...>`` with stdin/stdout/stderr all set to a host-side pty slave
allocated via ``pty.openpty()``. The host's pty master is what the
QThread reads from / writes to. ``-t`` on the container side allocates
a TTY in the container, and the line discipline on the host's pty
slave makes ``isatty(0)`` true inside the container — without that,
the Claude CLI detects "no TTY" and switches to ``--print`` mode,
which then errors out with "Input must be provided either through
stdin or as a prompt argument". The pty bridge also gives us
real resize semantics (``TIOCSWINSZ`` on the master fd propagates
through to the container's child via SIGWINCH).

A reader ``QThread`` (this class itself) pumps ``master_fd`` into
``data_received``. ``-i`` keeps stdin connected so the user can
type into the terminal; ``TERM=xterm-256color`` is set in the env
so Claude Code inside the container uses a recognised terminal type.

Auto-create
-----------
If the container doesn't exist (``docker inspect`` returns
non-zero), the launcher calls :func:`ensure_container` to create
it via ``docker create -i --mount ... <image>`` before
``Popen``. The create is idempotent — repeated calls are no-ops.
Existing-but-stopped containers are restarted by ``podman start``
(see :func:`ensure_container`'s docstring).

Persistence of ``run_in_container`` / ``container_image`` /
``container_name`` lives on the :class:`ClaudeProfile`; this class
is intentionally stateless beyond the running process.
"""

from __future__ import annotations

import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import termios
import threading
from typing import Sequence

from PySide6.QtCore import QThread, Signal

from src.profiles.models import ClaudeProfile
from src.profiles.paths_helpers import profile_config_base_dir
from src.ui.claude.docker_claude_md import sync_profile_claude_md


# Bind-mount target inside the container. The host's
# ``<profile>/`` dir is mounted at this path, so the in-container
# ``.claude`` subdir lands at ``/home/claude/.claude/``. Used as
# the ``target=`` argument to ``<runtime> create --mount``.
_IN_CONTAINER_CLAUDE_HOME = "/home/claude"


# Where Claude Code reads its config inside the container. With
# the bind-mount source being the entire ``<profile>/`` dir, the
# config files live at ``/home/claude/.claude/`` (relative to
# the in-container HOME). Claude Code's own convention is to
# default to ``<HOME>/.claude`` but we set the env var explicitly
# so the path is independent of whatever HOME resolves to inside
# (some images set ``HOME=/`` for the root user, etc.).
_IN_CONTAINER_CLAUDE_CONFIG_DIR = "/home/claude/.claude"


# Default pty size used when the host side has no preference. The
# real value is patched post-creation via ``TIOCSWINSZ`` from
# ``resize()`` once the terminal view has its actual dimensions.
_DEFAULT_PTY_ROWS = 40
_DEFAULT_PTY_COLS = 120


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Tell the kernel the PTY size so it can raise SIGWINCH in the child.

    Layout of the winsize struct: ``unsigned short ws_row, ws_col,
    ws_xpixel, ws_ypixel``. We send the pixel fields as 0; the
    terminal ignores them. The kernel forwards the new size to the
    slave, which in turn delivers ``SIGWINCH`` to the foreground
    process group — through the container's pty bridge all the way
    to the Claude CLI. Without this, the CLI thinks the terminal is
    24x80 and word-wraps the output incorrectly.
    """
    fcntl.ioctl(
        fd, termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, cols, 0, 0),
    )


def _resolve_docker_binary() -> str:
    """Locate the ``docker`` executable, raising if missing.

    Caller's responsibility to gate on the result — the launcher
    surfaces a friendly ``QMessageBox`` when this returns ``""``.

    The :class:`ClaudeContainerView` overrides ``container_runtime``
    via ``set_runtime_binary``; this default is the fallback when
    nothing is installed (e.g. test environments without Qt).
    """
    runtime = globals().get("_RUNTIME_BINARY_OVERRIDE") or "docker"
    return shutil.which(runtime) or ""


def set_runtime_binary(name: str) -> None:
    """Override the container runtime binary used by ``ensure_container``.

    Reads from :attr:`GlobalSettings.container_runtime` (set in
    Claude > Settings). Setting an empty string falls back to
    ``"docker"``. Tests can use this to point at a stubbed
    binary.
    """
    globals()["_RUNTIME_BINARY_OVERRIDE"] = name or "docker"


def ensure_image_for_current_uid(*, runtime: str | None = None) -> str:
    """Resolve — and rebuild if needed — the Claude Code image for the host UID.

    Thin wrapper around :func:`src.ui.claude.docker_image_builder.ensure_image_for_current_uid`
    that honours the runtime override (``podman`` / ``docker``)
    set by :func:`set_runtime_binary`. Returns the image tag
    (e.g. ``"claude-code:uid-1000"``) for the caller to pass to
    ``podman create``.

    Idempotent — a no-op when the right image is already cached,
    a one-time rebuild otherwise. See
    :mod:`src.ui.claude.docker_image_builder` for the full
    UID-matching rationale and tag scheme.
    """
    from src.ui.claude import docker_image_builder

    rt = runtime or globals().get("_RUNTIME_BINARY_OVERRIDE") or "podman"
    return docker_image_builder.ensure_image_for_current_uid(runtime=rt)


#: Image-tag values that mean "auto-resolve to the per-host-UID
#: tag" rather than "use this exact image". The legacy placeholder
#: ``"claude-code:latest"`` is included so profiles saved before
#: the per-UID scheme was introduced still trigger the rebuild on
#: their next launch. An empty string is also a sentinel (the new
#: dataclass default).
_AUTO_RESOLVE_IMAGE_SENTINELS: frozenset[str] = frozenset(
    {"", "claude-code:latest"}
)


def _resolve_image_for_launch(
    profile_container_image: str, *, runtime: str
) -> str:
    """Pick the image tag for a container launch.

    Returns the user's explicit ``profile_container_image`` when
    it's a real tag (anything outside the auto-resolve sentinel
    set). Returns the per-host-UID tag from
    :func:`ensure_image_for_current_uid` otherwise — which
    rebuilds the image if the cached version's baked-in UID
    doesn't match the host user.

    The "auto-resolve" set is intentionally narrow: just the
    empty string and the legacy ``claude-code:latest`` placeholder.
    Test fixtures use ``"alpine:latest"`` (a real image); users
    with custom builds use whatever tag they pushed. Neither
    should be silently rewritten.
    """
    if profile_container_image in _AUTO_RESOLVE_IMAGE_SENTINELS:
        return ensure_image_for_current_uid(runtime=runtime)
    return profile_container_image


def ensure_container(profile: ClaudeProfile) -> str:
    """Create the per-profile container if it doesn't exist.

    Idempotent across three states:

    - **Missing** — runs ``<runtime> create`` with the bind mount
      and network setup, then returns the name. The entrypoint is
      overridden to ``sleep infinity`` so the container stays alive
      without running Claude — see the long note in the ``create``
      block below for why.
    - **Stopped / exited** — runs ``<runtime> start`` to bring the
      existing container back up. With the entrypoint override this
      is a near no-op: the container transitions to ``Running``
      with ``sleep infinity`` as PID 1, and our subsequent ``exec``
      can attach. Without this, ``docker exec`` on a stopped
      container fails with *"can only create exec sessions on
      running containers: container state improper"*. Triggered
      by host reboot, ``podman stop`` from another shell, or a
      previous session that exited without a new one starting.
    - **Running** — returns the name as-is.

    The bind mount sources the profile's scoped
    ``<data_dir>/claude_profiles/<slug>/`` to
    ``/home/claude`` inside the container. SharkDrown writes
    ``settings.json``, ``agents/``, ``plugins/``, and
    ``.mcp.json`` under ``<slug>/.claude/`` (so the bind-mount
    exposes them at ``/home/claude/.claude/``); Claude Code's own
    runtime state (``history.jsonl``, ``projects/``, ``daemon/``,
    ``jobs/``, ``.claude.json``, etc.) also lives under
    ``<slug>/.claude/`` because Claude treats ``/home/claude`` as
    its home directory.

    The image tag is read from
    ``profile.container_image``; empty value falls back to
    ``claude-code:latest``. ``DockerSession.run`` reads the
    global default at session-launch time, so this helper just
    defaults to ``claude-code:latest`` for safety.

    Returns the resolved container name.
    """
    runtime = _resolve_docker_binary()
    if not runtime:
        raise FileNotFoundError(
            "Container runtime binary not found on PATH. "
            "Configure Claude > Settings > Container Runtime."
        )
    name = profile.container_name or profile.slug
    # Inspect — exit 0 means exists, anything else means missing.
    inspect = subprocess.run(
        [runtime, "inspect", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspect.returncode == 0:
        # Container exists — is it running? The bare ``inspect``
        # exit-0 check above used to silently accept a stopped
        # container, which then crashed the subsequent ``exec``
        # with "container state improper". Read ``State.Running``
        # explicitly and start the container if needed.
        state_proc = subprocess.run(
            [runtime, "inspect", "--format", "{{.State.Running}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        running = state_proc.stdout.strip().lower() == "true"
        if not running:
            # ``start`` is idempotent on a stopped container and
            # a no-op on an already-running one (the running case
            # is filtered above). We don't pass ``-a`` /
            # ``--attach`` — the child process we exec into is
            # the Claude Code session, not the container's
            # original entrypoint.
            subprocess.run(
                [runtime, "start", name],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return name
    # Make sure the scoped CLAUDE.md is on disk so the container
    # sees it on first launch. ``write_profile_config`` would also
    # create ``<slug>/.claude`` (which contains ``settings.json``,
    # ``agents/``, etc.), but ``ensure_container`` is sometimes
    # called without a write — and a missing ``<slug>/.claude/``
    # would leave Claude Code with no settings inside the
    # container.
    sync_profile_claude_md(profile)
    host_profile_dir = profile_config_base_dir() / profile.slug
    host_profile_dir.mkdir(parents=True, exist_ok=True)
    # Resolve the per-host-UID image tag (or honour the user's
    # explicit ``container_image`` override). Each host UID gets
    # its own tag (``claude-code:uid-<n>``) so the in-container
    # ``claude`` user can write to the bind-mounted profile dir
    # without a UID-mismatch EACCES — see
    # :mod:`src.ui.claude.docker_image_builder`.
    #
    # ``_resolve_image_for_launch`` treats the legacy placeholder
    # ``"claude-code:latest"`` (and the empty string) as "auto
    # resolve" so profiles saved before the per-UID scheme was
    # introduced still trigger the rebuild on their next launch.
    # Anything else — e.g. ``"alpine:latest"`` in tests, or a
    # user's custom image — is honoured verbatim.
    image = _resolve_image_for_launch(
        profile.container_image, runtime=runtime or "podman"
    )
    # Override the entrypoint to ``sleep infinity``. The image's
    # default ``CMD ["claude"]`` would otherwise run Claude as
    # PID 1 the moment the container starts — but at that point
    # the container has no stdin attached (``podman start`` does
    # not pass ``-i``), so Claude hits its 3-s "no stdin"
    # fallback, switches to ``--print`` mode, errors with
    # *"Input must be provided either through stdin or as a
    # prompt argument"*, and exits. The container then exits
    # with status 1, and our subsequent ``exec`` fails with
    # *"container state improper"* — the original Bug 3.
    #
    # With ``sleep infinity`` as the entrypoint, ``podman start``
    # just keeps the container alive. The actual Claude process
    # is launched exclusively through ``podman exec -i -t
    # <container> claude`` from :class:`DockerSession.run`, where
    # the host-side pty bridge (Bug 4 fix) makes ``isatty(0)``
    # true inside the container so Claude stays in interactive
    # mode and the prompt loop never times out.
    #
    # ``--userns=keep-id`` is the critical fix for the
    # bind-mount-permission bug. Without it, rootless podman
    # shifts the host's UID 1000 (``craig``) into the container's
    # UID 0 (``root``) for the bind-mount source — so the
    # in-container ``claude`` user (UID 1000, baked into the
    # image) sees the host files as owned by *root* and can't
    # write to ``~/.claude``. ``--userns=keep-id`` maps the host
    # UID 1:1 into the container, so the bind-mount source's
    # owner (``craig``, UID 1000) appears in the container as
    # UID 1000, matching the baked-in ``claude`` user.
    #
    # We deliberately don't pass ``--user`` — the image's
    # ``USER claude`` directive already sets the entrypoint
    # user, and our per-host-UID image build means that user is
    # always UID 1000 / GID 1000 / etc. as appropriate. Hardcoding
    # ``--user 1000:1000`` here would break for any host user
    # with a different UID.
    subprocess.run(
        [
            runtime, "create",
            "-i",
            "--name", name,
            "--userns=keep-id",
            "--mount",
            f"type=bind,source={host_profile_dir},target={_IN_CONTAINER_CLAUDE_HOME}",
            "--network", "bridge",
            "--hostname", f"claude-{profile.slug}",
            # Make ``host.docker.internal`` resolve to the host's
            # loopback on Linux. Docker Desktop (macOS / Windows)
            # does this automatically; the explicit ``--add-host``
            # is harmless there. Without it, Linux containers
            # can't reach the host proxy.
            "--add-host", "host.docker.internal:host-gateway",
            "--entrypoint", "[\"sleep\", \"infinity\"]",
            image,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return name


class DockerSession(QThread):
    """One Claude Code session running in a per-profile Docker container.

    Public API mirrors :class:`PtySession`:

    - ``start()`` → ``self.start()`` (QThread.start); spawns the
      reader loop which calls ``_run_reader``.
    - ``data_received(bytes)`` — emitted on each stdout chunk.
    - ``process_exited(int)`` — emitted when the child exits.
    - ``read_error(str)`` — fatal I/O / spawn error.
    - ``shutdown()`` — idempotent termination (SIGTERM, then SIGKILL
      after a grace period).

    Construction does *not* spawn the child — the caller calls
    ``start()``. The container is auto-created via
    :func:`ensure_container` on the first ``start()``.
    """

    data_received = Signal(bytes)
    process_exited = Signal(int)
    read_error = Signal(str)

    def __init__(
        self,
        profile: ClaudeProfile,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._argv = list(argv)
        self._env = dict(env) if env is not None else {}
        self._cwd = cwd
        self._process: subprocess.Popen | None = None
        self._master_fd: int = -1
        self._shutting_down = False
        self._stop_lock = threading.Lock()
        # Container lifecycle: by default the per-profile container
        # is stopped when the LAST session for that profile is
        # closed (``_close_session`` clears the flag on intermediate
        # closes). The container itself stays running between
        # sessions (PID 1 = ``sleep infinity``) so subsequent
        # ``docker exec`` calls skip the container-start cost; the
        # stop happens only on the final close.
        #
        # User-reported bug: closing a Claude session left the
        # container running indefinitely. Root cause was that
        # ``shutdown`` only killed the ``docker exec`` subprocess
        # — the container itself was never stopped because no
        # code path called ``docker stop <container>``.
        self._container_name: str | None = None
        self._stop_container_on_shutdown: bool = True

    def _reader_thread_target(self) -> None:
        """Pump ``master_fd`` (the host-side pty) into ``data_received``.

        Runs inside the QThread. Reads in 4 KB chunks; on EOF
        (zero-byte read after the child closes its pty slave),
        emits ``process_exited`` with the child's return code.

        We read from the pty master — not ``process.stdout`` —
        because the child's stdin/stdout/stderr are all routed
        through the pty slave. The master sees the post-line-
        -discipline output (cooked for echo, raw passthrough for
        ctrl-c, etc.) which is what the terminal view expects.
        """
        master_fd = self._master_fd
        if master_fd < 0:
            return
        try:
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.data_received.emit(chunk)
        finally:
            proc = self._process
            if proc is not None:
                try:
                    rc = proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    rc = proc.wait()
                self.process_exited.emit(rc)

    def run(self) -> None:
        """QThread entry point — opens a pty, spawns ``docker exec
        -i -t``, then pumps the pty master into ``data_received``.

        The host-side pty master ↔ pty slave ↔ container's ``-t``
        bridge gives the child a TTY end-to-end. Without this, the
        Claude CLI inside the container sees ``isatty(0) == false``
        and switches to ``--print`` mode, which then errors out
        with "Input must be provided either through stdin or as a
        prompt argument when using --print" because no prompt
        argument was passed and the user hasn't typed anything yet.
        """
        try:
            container = ensure_container(self._profile)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            self.read_error.emit(f"container: {exc}")
            return
        runtime = _resolve_docker_binary()
        # Open the host-side pty. The master fd is what we read
        # from / write to / resize; the slave fd is wired to the
        # child's stdin/stdout/stderr via ``Popen``. The kernel
        # line discipline on the slave makes ``isatty(0)`` return
        # true inside the container.
        try:
            master_fd, slave_fd = pty.openpty()
        except OSError as exc:
            self.read_error.emit(f"openpty: {exc}")
            return
        try:
            _set_winsize(master_fd, _DEFAULT_PTY_ROWS, _DEFAULT_PTY_COLS)
        except OSError:
            # winsize is best-effort; non-fatal if it fails (e.g.,
            # the kernel returns ENOTTY on some headless setups).
            pass
        # ``-t`` allocates a TTY on the container side. The host
        # pty slave + container pty slave together form a TTY
        # chain that the Claude CLI recognises as a terminal.
        #
        # ``-e KEY=VALUE`` flags must be passed for every env var
        # the inner ``claude`` process needs. ``Popen(env=...)`` below
        # only sets the env on the *outer* ``podman exec`` process —
        # and ``podman exec`` (like ``docker exec``) does NOT inherit
        # host env vars by default. The inner process only sees what
        # the container's PID 1 has plus what we pass via ``-e``.
        # Without these flags the session reaches claude with NO
        # ANTHROPIC_BASE_URL, no auth, and no CLAUDE_CONFIG_DIR —
        # and the CLI either errors out or silently routes to
        # Anthropic's real API instead of the local proxy.
        env_args: list[str] = []
        if self._env:
            for key, value in self._env.items():
                env_args.extend(["-e", f"{key}={value}"])
        cmd = [runtime, "exec", "-i", "-t", *env_args, container, *self._argv]
        try:
            self._master_fd = master_fd
            self._process = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self._cwd,
                env=self._env or None,
                close_fds=True,
            )
        except OSError as exc:
            self.read_error.emit(f"spawn: {exc}")
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                os.close(slave_fd)
            except OSError:
                pass
            self._master_fd = -1
            return
        # The child has its own copy of the pty slave via fork.
        # We close our copies so the master's read EOF fires when
        # the child exits.
        try:
            os.close(slave_fd)
        except OSError:
            pass
        # Record the container name so ``shutdown`` can call
        # ``<runtime> stop <container>`` after the exec process
        # dies. Stored as an instance attribute (not a closure)
        # so a future "kill the container right now" path can
        # read it without re-deriving the name from the profile.
        self._container_name = container
        self._reader_thread_target()

    def set_stop_container_on_shutdown(self, value: bool) -> None:
        """Tell this session whether to stop its container on exit.

        ``True`` (default) — when ``shutdown`` runs, run
        ``<runtime> stop <container>`` after the exec process
        dies so the container does not leak.

        ``False`` — leave the container running. The caller (the
        Claude container view) sets this when other live sessions
        for the same profile still need the container; only the
        LAST session's shutdown actually stops the container.

        Thread-safe: this can be called from the GUI thread while
        ``shutdown`` runs in the QThread's run loop. The shutdown
        path takes ``_stop_lock`` before reading the flag, so a
        late setter call is either applied before shutdown
        snapshots the value or after the container stop has been
        scheduled — never in between.
        """
        with self._stop_lock:
            self._stop_container_on_shutdown = bool(value)

    def stop_container(self) -> None:
        """Run ``<runtime> stop <container>`` if we have a container.

        Best-effort: failures are logged but don't raise, because
        the caller is already in the shutdown path and there's
        nothing useful it can do with the exception. Idempotent —
        a second call is a no-op because ``_container_name`` is
        cleared after the first run.

        Called by ``shutdown`` when ``_stop_container_on_shutdown``
        is True (i.e. this is the last session for the profile).
        External callers (e.g. ``ClaudeContainerView`` during
        application exit) can call it directly to force-stop a
        leaked container.
        """
        name = self._container_name
        if not name:
            return
        # Clear before issuing the stop — if the runtime hangs we
        # don't want subsequent calls to issue duplicate stops.
        self._container_name = None
        try:
            runtime = _resolve_docker_binary()
            subprocess.run(
                [runtime, "stop", "--time", "5", name],
                check=False,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Don't raise — shutdown callers can't usefully recover.
            # The container may leak, but the exec process is
            # already dead and the GUI session is closing.
            import warnings as _warnings

            _warnings.warn(
                f"could not stop container {name!r}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    def write(self, data: bytes) -> None:
        """Forward user keystrokes to the pty master.

        ``TerminalSessionView`` calls this for every text-edit
        keystroke. Returns silently if the session isn't running
        (the GUI already saw ``process_exited`` and tore down).

        Writing to the master (rather than the child's stdin pipe)
        routes the bytes through the kernel's line discipline, so
        special characters like ``Ctrl-C`` (``\x03``) are translated
        into ``SIGINT`` for the foreground process group — and
        cooked-mode echo is handled by the kernel, not by the
        container's child.
        """
        master_fd = self._master_fd
        if master_fd < 0:
            return
        try:
            os.write(master_fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        """Resize the container's TTY.

        ``TIOCSWINSZ`` on the master fd updates the kernel's
        record of the pty size; the kernel then delivers
        ``SIGWINCH`` to the foreground process group inside the
        container. The container's pty slave forwards the size
        change to the Claude CLI, which re-flows its output.
        """
        master_fd = self._master_fd
        if master_fd < 0:
            return
        try:
            _set_winsize(master_fd, rows, cols)
        except OSError:
            pass

    def shutdown(self) -> None:
        """Idempotent shutdown: SIGTERM, then SIGKILL after 5 s.

        ``process_exited`` will fire from the reader loop when the
        child actually exits; we don't emit it manually here so
        the GUI sees exactly one exit event per session.

        When ``_stop_container_on_shutdown`` is True (the default,
        set by ``set_stop_container_on_shutdown``), this also
        runs ``<runtime> stop <container>`` after the exec process
        dies so the container does not leak. The container-start
        cost was previously paid once per session; now it's paid
        once per "burst" of sessions for the same profile, with
        container-stop only on the final close.
        """
        with self._stop_lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            stop_container = self._stop_container_on_shutdown
        proc = self._process
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
        # Bounded wait — 5 s grace before SIGKILL. The reader
        # loop will emit process_exited once the child exits.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        # Stop the container ONLY when this is the last session
        # for the profile (see ``set_stop_container_on_shutdown``).
        # ``stop_container`` is itself idempotent — a None /
        # already-cleared ``_container_name`` is a no-op, so a
        # session that exited before ``run`` recorded the
        # container name doesn't trigger a bogus stop.
        if stop_container:
            self.stop_container()
        # Close the pty master so the reader loop's ``os.read`` hits
        # EOF (rather than blocking on a fd that no longer has a
        # writer). Idempotent — the reader may already have closed
        # it after the child exited.
        master_fd = self._master_fd
        if master_fd >= 0:
            try:
                os.close(master_fd)
            except OSError:
                pass
            self._master_fd = -1