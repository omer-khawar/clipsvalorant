"""Stopping a scan that is going nowhere.

A scan is mostly waiting: for HenrikDev's rate-limit window, for Twitch, for
ffmpeg, for OCR. That is fine when it is going to finish. It is not fine when
you typed the wrong region and the thing is going to spend four hundred
seconds confirming it - and until this existed the only way out was killing
the server.

The piece that makes this work is `threading.Event.wait(timeout)`. It is a
sleep that can be woken, so every `time.sleep` on a waiting path becomes
interruptible by swapping it for `token.nap(seconds)`. A two-minute quota
pause then ends the instant somebody presses stop, rather than two minutes
later.

Cancelling is cooperative and it is not a rollback. Work already written to
the database stays written: a scan that checked thirty matches before you
stopped it really did check them, and throwing that away would make stopping
expensive enough that nobody would do it.
"""

from __future__ import annotations

import threading


class Cancelled(Exception):
    """Raised inside a scan once somebody has asked it to stop.

    An exception rather than a return value because the places that need to
    notice are deep in retry loops and HTTP plumbing, and every caller
    between here and the top would otherwise have to remember to check.
    """


class CancelToken:
    """A flag a running scan watches, and sleeps against."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Ask the scan to stop. Safe from any thread, and idempotent."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise if we have been asked to stop. Call between units of work."""
        if self._event.is_set():
            raise Cancelled("the scan was stopped")

    def nap(self, seconds: float) -> None:
        """Sleep, but wake immediately if cancelled - and then raise.

        Every wait in the API clients goes through here. Waiting out a
        rate-limit window is the single longest thing a scan does, so it is
        also the one place stopping has to work instantly.
        """
        if seconds > 0 and self._event.wait(seconds):
            raise Cancelled("the scan was stopped while it was waiting")
        self.check()


class NullToken(CancelToken):
    """A token that is never set, for callers that have nothing to cancel.

    Lets the clients take a token unconditionally instead of guarding every
    sleep with `if token is not None`.
    """

    __slots__ = ()

    def cancel(self) -> None:          # pragma: no cover - deliberately inert
        """Ignored. Nothing holds this token, so nothing can be stopped."""

    @property
    def cancelled(self) -> bool:
        return False

    def check(self) -> None:
        return

    def nap(self, seconds: float) -> None:
        if seconds > 0:
            self._event.wait(seconds)
