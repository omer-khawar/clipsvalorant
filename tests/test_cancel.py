"""Stopping a scan that is going nowhere.

Most of a scan is waiting, and the longest wait is HenrikDev's rate-limit
window - up to two minutes of `time.sleep` that nothing could interrupt. A
scan started on the wrong region would spend several minutes proving it, and
the only way out was killing the server.

These tests are about the two properties that make stopping worth having: it
has to be fast, and it has to keep what was already found.
"""

import threading
import time

import pytest

from clipsvalorant.cancel import CancelToken, Cancelled, NullToken


def test_a_long_wait_ends_the_moment_it_is_cancelled():
    """The whole point. A two-minute quota pause has to end on the press, not
    two minutes after it."""
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    started = time.perf_counter()
    with pytest.raises(Cancelled):
        token.nap(30)
    assert time.perf_counter() - started < 1.0, "the wait was not interrupted"


def test_a_wait_that_is_never_cancelled_still_waits():
    """The flip side, and easy to break by making nap() return immediately:
    the backoff between retries has to actually back off."""
    token = CancelToken()
    started = time.perf_counter()
    token.nap(0.2)
    assert time.perf_counter() - started >= 0.15


def test_cancelling_before_the_wait_does_not_wait_at_all():
    token = CancelToken()
    token.cancel()
    started = time.perf_counter()
    with pytest.raises(Cancelled):
        token.nap(30)
    assert time.perf_counter() - started < 0.5


def test_check_raises_only_once_cancelled():
    token = CancelToken()
    token.check()                       # no exception
    token.cancel()
    with pytest.raises(Cancelled):
        token.check()


def test_cancelling_twice_is_harmless():
    token = CancelToken()
    token.cancel()
    token.cancel()
    assert token.cancelled


def test_a_null_token_never_stops_anything():
    """Clients take a token unconditionally rather than guarding every sleep
    with a None check, so the inert one has to behave like no token at all."""
    token = NullToken()
    token.cancel()
    assert not token.cancelled
    token.check()
    started = time.perf_counter()
    token.nap(0.1)
    assert time.perf_counter() - started >= 0.05


# ------------------------------------------------- through the HTTP client

class _Url:
    path = "/anything"


class _Request:
    url = _Url()


class _Resp:
    status_code = 429
    headers = {"retry-after": "60"}
    text = ""
    request = _Request()

    def json(self):
        return {}


def test_a_rate_limited_client_stops_instead_of_sleeping_out_the_window():
    """Where this actually bites: HenrikDev replies 429 with a 60 second
    retry-after, and the client used to sleep every one of them."""
    from clipsvalorant.henrik import HenrikClient

    class Always429:
        def get(self, *a, **k):
            return _Resp()

        def close(self):
            pass

    token = CancelToken()
    client = HenrikClient("k", client=Always429(), cancel=token)
    threading.Timer(0.05, token.cancel).start()
    started = time.perf_counter()
    with pytest.raises(Cancelled):
        client._get("/anything")
    assert time.perf_counter() - started < 2.0, "slept out the rate-limit window"


def test_a_cancelled_client_does_not_even_send_the_request():
    """Stopping should stop spending quota, not merely stop waiting."""
    from clipsvalorant.henrik import HenrikClient

    sent = []

    class Counting:
        def get(self, path, **k):
            sent.append(path)
            return _Resp()

        def close(self):
            pass

    token = CancelToken()
    token.cancel()
    client = HenrikClient("k", client=Counting(), cancel=token)
    with pytest.raises(Cancelled):
        client._get("/anything")
    assert sent == [], "a cancelled scan still spent an API call"


# --------------------------------------------------------- through the job

def test_a_stopped_job_is_not_reported_as_a_failure(cfg, monkeypatch):
    """A scan you stopped on purpose is not an error, and showing it as one
    makes the button feel like something went wrong."""
    from clipsvalorant import web

    job = web.Job(id="j", riot_id="Someone#NA1", key="")

    def runner(acct, **kwargs):
        raise Cancelled("stopped")

    monkeypatch.setattr("clipsvalorant.ingest.ingest", runner)
    web.run_search(cfg, job, days=14, fetch=5, watch=False)

    assert job.state == "cancelled"
    assert job.error == ""
    assert any("Stopped" in line for line in job.lines)


def test_a_job_reports_that_it_is_stopping(cfg):
    from clipsvalorant import web

    job = web.Job(id="j", riot_id="Someone#NA1", key="")
    assert job.snapshot()["stopping"] is False
    job.stop()
    assert job.snapshot()["stopping"] is True
    assert job.token.cancelled
