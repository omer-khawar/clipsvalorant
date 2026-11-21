"""Pacing against HenrikDev's window, which belongs to the key and not to us.

A Basic key is thirty points a minute, and a scan builds three clients out of
it: one to ask which region the account plays on, one to ingest, one to count
the queues the scan is not looking at. Every one of them used to start with no
idea what the others had spent, so each spent a call to find out the window was
empty and then slept a minute it had not measured. From the outside that was a
scan announcing "quota nearly spent" three times before it had done anything.

These are the contracts that stop it: the budget is shared, the wait is what is
actually left, and an empty window is announced once.
"""

import httpx
import pytest

from clipsvalorant import henrik
from clipsvalorant.cancel import CancelToken
from clipsvalorant.henrik import HenrikClient

KEY = "HDEV-test-key"


@pytest.fixture(autouse=True)
def _clean_budgets():
    """No test inherits another's window."""
    henrik.forget_budgets()
    yield
    henrik.forget_budgets()


class _Naps(CancelToken):
    """A token that records what it was asked to sleep instead of sleeping."""

    def __init__(self):
        super().__init__()
        self.naps = []

    def nap(self, seconds):
        self.naps.append(seconds)


def _responder(*, remaining, reset=60, limit=30):
    def handle(request):
        return httpx.Response(
            200,
            json={"data": {}},
            headers={
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(reset),
                "x-ratelimit-limit": str(limit),
            },
        )
    return handle


def _client(handler, token=None):
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url=henrik.BASE_URL)
    return HenrikClient(KEY, client=inner, cancel=token or CancelToken())


def test_a_second_client_does_not_start_blind(monkeypatch):
    """The failure this prevents: three clients in one scan, three discoveries
    that the window is empty, three calls spent finding out and three waits."""
    monkeypatch.setattr(henrik.time, "monotonic", lambda: 1000.0)

    first = _client(_responder(remaining=0, reset=40), _Naps())
    first._get("/valorant/v1/account/a/1")

    token = _Naps()
    second = _client(_responder(remaining=29), token)
    second._wait_for_quota()

    assert token.naps == [40.0], "the second client should know what the first spent"


def test_the_wait_is_what_is_left_of_the_window(monkeypatch):
    """A reset read fifty seconds ago has fifty fewer seconds left to wait.

    Sleeping the header's own number bought a whole extra minute every time a
    scan did anything slow - reading VOD frames, say - between two calls.
    """
    now = {"t": 1000.0}
    monkeypatch.setattr(henrik.time, "monotonic", lambda: now["t"])

    token = _Naps()
    client = _client(_responder(remaining=0, reset=60), token)
    client._get("/valorant/v1/account/a/1")

    now["t"] += 50.0  # fifty seconds of OCR
    client._wait_for_quota()

    assert token.naps == [10.0]


def test_a_window_that_has_already_turned_over_is_not_waited_for(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(henrik.time, "monotonic", lambda: now["t"])

    token = _Naps()
    client = _client(_responder(remaining=0, reset=30), token)
    client._get("/valorant/v1/account/a/1")

    now["t"] += 31.0
    client._wait_for_quota()

    assert token.naps == []


def test_an_empty_window_is_waited_out_once_not_once_per_client(monkeypatch):
    """Whoever gets there first does the waiting and says so; the rest of the
    scan carries on into the window that wait bought."""
    monkeypatch.setattr(henrik.time, "monotonic", lambda: 1000.0)

    first = _client(_responder(remaining=0, reset=45), _Naps())
    first._get("/valorant/v1/account/a/1")

    one, two = _Naps(), _Naps()
    _client(_responder(remaining=29), one)._wait_for_quota()
    _client(_responder(remaining=29), two)._wait_for_quota()

    assert one.naps == [45.0]
    assert two.naps == []


def test_quota_in_hand_is_never_waited_on(monkeypatch):
    monkeypatch.setattr(henrik.time, "monotonic", lambda: 1000.0)
    token = _Naps()
    client = _client(_responder(remaining=12), token)
    client._get("/valorant/v1/account/a/1")
    client._wait_for_quota()
    assert token.naps == []
    assert client.quota_left == 12


def test_a_key_with_its_own_window_is_not_paced_by_another_keys(monkeypatch):
    """Two keys are two windows. Sharing state by key rather than by client
    must not become sharing it by process."""
    monkeypatch.setattr(henrik.time, "monotonic", lambda: 1000.0)

    spent = HenrikClient(
        KEY,
        client=httpx.Client(
            transport=httpx.MockTransport(_responder(remaining=0, reset=60)),
            base_url=henrik.BASE_URL,
        ),
    )
    spent._get("/valorant/v1/account/a/1")

    token = _Naps()
    other = HenrikClient(
        "HDEV-a-different-key",
        client=httpx.Client(
            transport=httpx.MockTransport(_responder(remaining=30)),
            base_url=henrik.BASE_URL,
        ),
        cancel=token,
    )
    other._wait_for_quota()

    assert token.naps == []
