"""ESC-key interrupt support for the streaming agent loop (Phase C.3).

A context manager that runs a small background thread reading raw key
presses from stdin. When ESC is detected, an internal threading.Event
gets set; the agent loop polls it between stream chunks and aborts the
generation cleanly.

Cross-platform:
    Windows  — uses msvcrt.kbhit() / msvcrt.getch().
    POSIX    — uses termios cbreak mode + select() polling.
    Non-TTY  — no-op (tests, piped input, CI). The flag never fires.

The listener does NOT swallow Ctrl+C. SIGINT still raises
KeyboardInterrupt the way users expect; the agent catches that at the
same level as the ESC interrupt for a unified UX.

Public API:
    InterruptListener()     — context manager (use with `with`).
        .is_set() -> bool   — has ESC been pressed since the last reset?
        .reset()            — clear the flag (between agent iterations).
"""

from __future__ import annotations

import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)


# Length of one polling tick. Short enough that the user feels ESC fire
# instantly, long enough that the thread doesn't burn CPU.
_POLL_INTERVAL = 0.05


class InterruptListener:
    """Background-thread ESC watcher; flag-based interface for the agent loop."""

    def __init__(self) -> None:
        self._interrupt = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # POSIX-only: holds the original termios settings + the fd we
        # mutated, so __exit__ can restore them.
        self._restore: tuple | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> InterruptListener:
        if not self._stdin_is_tty():
            # Non-interactive run — flag never fires. The listener is
            # still usable as a passive flag holder so callers can
            # uniformly check is_set() without branching.
            return self
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sf-agent-esc-listener",
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            # Daemon thread — small wait is enough; we don't block on it
            # if the keyboard read is mid-call.
            self._thread.join(timeout=0.5)
            self._thread = None
        self._restore_terminal()

    # ------------------------------------------------------------------
    # Public flag API (also usable from tests by direct mutation)
    # ------------------------------------------------------------------

    def is_set(self) -> bool:
        """True iff ESC has been pressed since construction or last reset."""
        return self._interrupt.is_set()

    def reset(self) -> None:
        """Clear the flag — call between agent iterations to allow re-trigger."""
        self._interrupt.clear()

    def fire_for_test(self) -> None:
        """Test-only hook: pretend ESC was pressed. Same effect as a real keystroke."""
        self._interrupt.set()

    # ------------------------------------------------------------------
    # Platform polling
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            if sys.platform == "win32":
                self._run_windows()
            else:
                self._run_posix()
        except Exception:
            # Listener crashes must not break the agent run. Log + exit
            # the thread; the user just loses ESC support for this run.
            logger.exception("Interrupt listener thread crashed")

    def _run_windows(self) -> None:
        import msvcrt
        while not self._stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b"\x1b":  # ESC
                    self._interrupt.set()
                    return
            time.sleep(_POLL_INTERVAL)

    def _run_posix(self) -> None:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        self._restore = (fd, original)
        try:
            tty.setcbreak(fd)
            while not self._stop.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], _POLL_INTERVAL)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":  # ESC
                        self._interrupt.set()
                        return
        finally:
            self._restore_terminal()

    def _restore_terminal(self) -> None:
        if self._restore is None:
            return
        try:
            import termios
            fd, settings = self._restore
            termios.tcsetattr(fd, termios.TCSADRAIN, settings)
        except Exception:
            logger.exception("Failed to restore terminal mode")
        finally:
            self._restore = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stdin_is_tty() -> bool:
        """True iff stdin is attached to a real interactive terminal."""
        try:
            return sys.stdin.isatty()
        except (AttributeError, ValueError):
            # Some test runners replace stdin with non-stream objects.
            return False


__all__ = ["InterruptListener"]
