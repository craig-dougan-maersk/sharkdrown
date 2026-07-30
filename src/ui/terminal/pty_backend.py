"""PTY-backed shell session running on its own QThread.

Models ``ProxyServerThread`` (``src/proxy/server_thread.py:56``): a
``QThread`` subclass with class-scope ``Signal``s and an explicit
``request_stop()`` that wakes the reader loop without resorting to
``QThread.terminate()`` (which Qt docs warn against — it can leave
fds and child processes in undefined states).

Lifecycle
---------
1. ``start_pty()`` — fork a child via :func:`pty.fork`, set the child up as
   a session leader, exec the requested shell. The parent's master fd is
   kept and used for I/O. After fork, ``start()`` launches the QThread
   which runs the reader loop in ``run()``.
2. ``run()`` — blocks in ``select([master_fd, wake_r])`` then ``os.read`` on
   whichever fd is ready. Bytes read from the master are emitted via
   ``data_received(bytes)``. A 1-byte write to ``wake_w`` (the self-pipe)
   unblocks ``select`` so ``request_stop()`` is responsive even when the
   child is idle.
3. ``shutdown()`` — idempotent; called both from the ``run()`` exit path
   and from the GUI's ``closeEvent``. Sends ``SIGHUP`` then ``SIGKILL`` to
   the child, closes the master fd, joins the thread (bounded).

POSIX-only
----------
This module imports ``pty``, ``fcntl``, ``termios`` — all POSIX. On
non-POSIX (Windows) ``start_pty()`` will raise ``NotImplementedError``;
callers should guard with ``hasattr(os, "fork")`` before instantiating.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
from typing import Sequence

from PySide6.QtCore import QThread, Signal


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Tell the kernel the PTY size so it can raise SIGWINCH in the child.

    Layout of the winsize struct: ``unsigned short ws_row, ws_col, ws_xpixel,
    ws_ypixel``. We send the pixel fields as 0; the terminal ignores them.
    """
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PtySession(QThread):
    """One shell session, parent-side, running on its own QThread."""

    # Raw bytes read from the master fd. Emitted from the reader thread;
    # Qt's signal system queues these to the GUI thread automatically.
    data_received = Signal(bytes)
    # Child exited; payload is the waitpid status (0 if normal exit,
    # signal-encoded otherwise).
    process_exited = Signal(int)
    # Hard error (not child-exit-related). The session is dead after this.
    read_error = Signal(str)

    def __init__(
        self,
        argv: Sequence[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if argv:
            self._argv = list(argv)
        else:
            # Default to the user's $SHELL in **interactive** mode. Without
            # ``-i``, bash/sh run in non-interactive mode: they read from
            # stdin but don't print a prompt and don't echo input. The user
            # would see "I type but nothing happens" — exactly the symptom
            # this default prevents.
            #
            # We don't pass ``--norc`` / ``--noprofile`` because users
            # generally *want* their PATH / aliases set up. If a particular
            # .bashrc hangs, they can pass ``argv=["bash", "-i"]`` (or any
            # explicit args) to skip the auto-detection and avoid the
            # interactive flag conflict with --norc.
            shell = os.environ.get("SHELL") or "/bin/sh"
            base = shell.rsplit("/", 1)[-1]
            if base.startswith("bash") or base in ("sh", "dash", "ksh", "zsh"):
                self._argv = [shell, "-i"]
            else:
                self._argv = [shell]
        self._cwd = cwd
        self._env = env
        self._rows = rows
        self._cols = cols

        # Fork state (set in start_pty()).
        self._pid: int = -1
        self._master_fd: int = -1
        # Self-pipe for waking select() on shutdown.
        self._wake_r: int = -1
        self._wake_w: int = -1
        # Stop flag — checked at the top of each reader iteration.
        self._stopped = False
        # Reap-once flag + lock: both the GUI thread (is_alive) and the
        # reader QThread (_reap_in_thread) call os.waitpid(); the lock makes
        # the check-then-waitpid atomic so only one call ever lands.
        self._reaped = False
        self._reap_lock = threading.Lock()

    # ---- public API ---------------------------------------------------------

    def start_pty(self) -> None:
        """Fork the child and start the reader QThread.

        After this returns the parent process can ``write()`` to the PTY
        and receive ``data_received`` signals. Safe to call once per
        instance — calling twice raises.
        """
        if self._master_fd != -1:
            raise RuntimeError("PtySession.start_pty() already called")

        # Set up the self-pipe BEFORE fork so the child doesn't inherit
        # CLOEXEC-less fds. Both ends must be CLOEXEC so the child's exec
        # closes them.
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        fcntl.fcntl(self._wake_r, fcntl.FD_CLOEXEC)
        fcntl.fcntl(self._wake_w, fcntl.FD_CLOEXEC)

        pid, master_fd = pty.fork()
        if pid == 0:
            # ---- child branch ---------------------------------------------
            # We are now in the forked child, which is already a session
            # leader (pty.fork does setsid) with the slave pty as its
            # controlling terminal. Set the size, then exec.
            try:
                _set_winsize(0, self._rows, self._cols)
            except OSError:
                pass
            try:
                if self._cwd is not None:
                    os.chdir(self._cwd)
                if self._env is not None:
                    # Ensure TERM is set for proper capability detection.
                    if "TERM" not in self._env:
                        self._env["TERM"] = "xterm-256color"
                    os.execvpe(self._argv[0], self._argv, self._env)
                else:
                    # Ensure TERM is set even when inheriting parent env.
                    if "TERM" not in os.environ:
                        os.environ["TERM"] = "xterm-256color"
                    os.execvp(self._argv[0], self._argv)
            except OSError:
                os._exit(127)
            # Unreachable.
            os._exit(127)

        # ---- parent branch ----------------------------------------------
        self._pid = pid
        self._master_fd = master_fd
        os.set_blocking(self._master_fd, False)
        # Match the child's reported size in case pty.fork disagreed.
        try:
            _set_winsize(self._master_fd, self._rows, self._cols)
        except OSError:
            pass

        # Kick off the reader QThread.
        self.start()

    @property
    def is_alive(self) -> bool:
        """True if the child process is still running (or being reaped)."""
        if self._pid <= 0:
            return False
        with self._reap_lock:
            if self._reaped:
                return False
            try:
                wpid, _ = os.waitpid(self._pid, os.WNOHANG)
                if wpid == self._pid:
                    self._reaped = True
                    return False
            except ChildProcessError:
                self._reaped = True
                return False
        return True

    def write(self, data: bytes) -> None:
        """Write user input to the PTY (called from the GUI thread)."""
        if self._master_fd == -1 or self._stopped:
            return
        try:
            os.write(self._master_fd, data)
        except OSError as e:
            # EPIPE / EIO means the child has died mid-write. Surface as a
            # read_error so the GUI can clean up.
            self.read_error.emit(f"write failed: {e}")

    def resize(self, rows: int, cols: int) -> None:
        """Update the PTY size and notify the kernel to raise SIGWINCH."""
        if self._master_fd == -1:
            return
        rows = max(1, int(rows))
        cols = max(1, int(cols))
        if rows == self._rows and cols == self._cols:
            return
        self._rows = rows
        self._cols = cols
        try:
            _set_winsize(self._master_fd, rows, cols)
        except OSError:
            pass

    def request_stop(self) -> None:
        """Ask the reader loop to exit and signal the child to terminate.

        Idempotent. Safe to call from any thread.
        """
        if self._stopped:
            return
        self._stopped = True
        # Wake the blocking select() so the loop iterates and notices the
        # stop flag.
        if self._wake_w != -1:
            try:
                os.write(self._wake_w, b"\x00")
            except OSError:
                pass

    def terminate_child(self) -> None:
        """SIGHUP → SIGKILL the child, then reap it. Idempotent."""
        pid = self._pid
        if pid <= 0:
            return
        with self._reap_lock:
            if self._reaped:
                return
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                with self._reap_lock:
                    self._reaped = True
                break
            except PermissionError:
                # Not our child — best-effort terminate.
                pass
            # Give the child a brief window to die between SIGHUP and
            # SIGKILL. Don't busy-wait.
            for _ in range(20):  # ~200ms total
                with self._reap_lock:
                    if self._reaped:
                        return
                    try:
                        wpid, status = os.waitpid(pid, os.WNOHANG)
                        if wpid == pid:
                            self._reaped = True
                            self.process_exited.emit(self._encode_status(status))
                            return
                    except ChildProcessError:
                        self._reaped = True
                        self.process_exited.emit(0)
                        return
                import time as _t

                _t.sleep(0.01)

    def shutdown(self) -> None:
        """Idempotent full-shutdown: stop, terminate, join, close fds."""
        self.request_stop()
        # Kill the child (this also reaps and emits process_exited).
        self.terminate_child()
        # Wait briefly for the reader QThread to finish.
        if self.isRunning():
            self.wait(500)
            if self.isRunning():
                # Last resort.
                self.terminate()
                self.wait(200)
        self._close_fds()

    # ---- QThread entry point ------------------------------------------------

    def run(self) -> None:
        """Reader loop. Lives on the QThread; emits queued signals to GUI."""
        try:
            while not self._stopped:
                # Wait for either the master fd or the wake pipe.
                try:
                    rlist, _, _ = select.select(
                        [self._master_fd, self._wake_r], [], [], 0.5
                    )
                except (OSError, ValueError):
                    # Bad fd — likely the child closed master.
                    break
                if self._wake_r in rlist:
                    # Drain the wake pipe (a single byte is enough).
                    try:
                        os.read(self._wake_r, 64)
                    except OSError:
                        pass
                if self._master_fd in rlist:
                    try:
                        chunk = os.read(self._master_fd, 4096)
                    except OSError as e:
                        if e.errno == errno.EIO:
                            # Linux: master end closed because the child
                            # exited. Graceful exit path.
                            self._reap_in_thread()
                            break
                        self.read_error.emit(f"read failed: {e}")
                        break
                    if not chunk:
                        # EOF on the master fd.
                        self._reap_in_thread()
                        break
                    self.data_received.emit(chunk)
        finally:
            # Best-effort cleanup if we exit the loop without a reaper.
            self._reap_in_thread()

    # ---- internals ----------------------------------------------------------

    def _reap_in_thread(self) -> None:
        """Reap the child if it has exited. Safe to call multiple times."""
        if self._pid <= 0:
            return
        with self._reap_lock:
            if self._reaped:
                return
            try:
                wpid, status = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                self._reaped = True
                self.process_exited.emit(0)
                return
            except OSError:
                return
            if wpid == self._pid:
                self._reaped = True
                self.process_exited.emit(self._encode_status(status))

    @staticmethod
    def _encode_status(status: int) -> int:
        """Turn an ``os.waitpid`` status into a small integer for the signal.

        ``status`` from ``waitpid`` packs the exit code + signal info into a
        single int via ``os.W*`` macros. For the GUI we only need to
        distinguish "exited normally" (returns 0) from "killed by signal"
        (returns non-zero). Callers that care about the exact exit code
        can use ``os.WEXITSTATUS`` / ``os.WIFSIGNALED`` themselves.
        """
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            # Encode signal number so non-zero indicates abnormal exit.
            return 128 + os.WTERMSIG(status)
        return 0

    def _close_fds(self) -> None:
        """Close all kernel fds this PtySession owns. Idempotent."""
        for attr in ("_master_fd", "_wake_r", "_wake_w"):
            fd = getattr(self, attr)
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, -1)
