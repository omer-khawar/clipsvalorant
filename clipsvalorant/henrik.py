"""HenrikDev unofficial Valorant API client.

Rate limits (per their README): Basic key 30 req/min, Enhanced 90 req/min, paid
tiers 130/200/300. Their rate limiter counts each call as 1 point PLUS 1 for every
background Riot request it triggers, so a cold match detail costs more than a cached
one. We read the X-RateLimit-* headers at runtime rather than hardcoding a number.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from clipsvalorant.models import Match, StoredMatch

log = logging.getLogger(__name__)

BASE_URL = "https://api.henrikdev.xyz"
USER_AGENT = "clipsvalorant/0.1 (personal match-to-VOD tool)"


class HenrikError(RuntimeError):
    """An API call failed in a way we cannot recover from."""


class RateLimited(HenrikError):
    pass


class HenrikClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        client: httpx.Client | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._owns_client = client is None
        # Quota state from the last response, so we can pace instead of absorbing 429s.
        self._remaining: int | None = None
        self._reset_in: float = 60.0
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"Authorization": api_key, "User-Agent": USER_AGENT},
        )

    def __enter__(self) -> "HenrikClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ---- plumbing ------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            self._wait_for_quota()
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:  # network-level
                last_error = exc
                self._sleep_backoff(attempt)
                continue

            self._record_quota(resp)

            if resp.status_code == 429:
                wait = self._retry_after(resp)
                log.warning("Rate limited on %s, waiting %.1fs", path, wait)
                time.sleep(wait)
                last_error = RateLimited(f"429 on {path}")
                continue

            if resp.status_code == 404:
                raise HenrikError(f"404 Not Found: {path}")

            if resp.status_code >= 500:
                last_error = HenrikError(f"{resp.status_code} on {path}")
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 403:
                raise HenrikError(
                    f"403 Forbidden on {path} - check HENRIK_API_KEY is valid and active."
                )

            if resp.status_code >= 400:
                raise HenrikError(f"{resp.status_code} on {path}: {resp.text[:300]}")

            try:
                return resp.json()
            except ValueError as exc:
                raise HenrikError(f"Non-JSON response from {path}") from exc

        raise HenrikError(f"Gave up on {path} after {self._max_retries} attempts") from last_error

    def _record_quota(self, resp: httpx.Response) -> None:
        """Remember how much quota is left so the next call can pace itself.

        Their limiter charges 1 point per call PLUS 1 per background Riot request it
        triggers, so a cold match detail can cost more than the one call you made.
        That makes reactive 429 handling alone wasteful - we pre-empt instead.
        """
        raw_remaining = resp.headers.get("x-ratelimit-remaining")
        raw_reset = resp.headers.get("x-ratelimit-reset")
        try:
            self._remaining = int(raw_remaining) if raw_remaining is not None else None
        except ValueError:
            self._remaining = None
        try:
            if raw_reset is not None:
                self._reset_in = min(max(float(raw_reset), 1.0), 120.0)
        except ValueError:
            pass
        log.debug("%s -> %s (quota left: %s)", resp.request.url.path,
                  resp.status_code, self._remaining)

    def _wait_for_quota(self) -> None:
        """Sleep out the window when we are about to run dry."""
        if self._remaining is not None and self._remaining <= 1:
            log.info("Quota nearly spent, pausing %.0fs for the window to reset",
                     self._reset_in)
            time.sleep(self._reset_in)
            self._remaining = None  # unknown again until the next response

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        for header in ("retry-after", "x-ratelimit-reset"):
            raw = resp.headers.get(header)
            if raw:
                try:
                    return min(max(float(raw), 1.0), 120.0)
                except ValueError:
                    pass
        return 15.0

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(2**attempt, 30))

    # ---- endpoints -----------------------------------------------------

    def stored_matches(
        self,
        name: str,
        tag: str,
        region: str,
        *,
        mode: str | None = "competitive",
        size: int = 20,
        page: int = 1,
    ) -> list[StoredMatch]:
        """GET /valorant/v1/stored-matches/{affinity}/{name}/{tag}

        Note: 'stored' matches are an accumulating subset populated on request, not a
        mirror of Riot's history. Matches nobody has ever fetched will be missing.
        """
        params: dict[str, Any] = {"size": size, "page": page}
        if mode:
            params["mode"] = mode
        payload = self._get(
            f"/valorant/v1/stored-matches/{region}/{name}/{tag}", params=params
        )
        return [StoredMatch.model_validate(item) for item in payload.get("data", [])]

    def match_detail(self, match_id: str, region: str) -> Match:
        """GET /valorant/v4/match/{affinity}/{match_id}

        Their OpenAPI spec gives the two-segment form, but their v4 changelog shows a
        three-segment form with a platform. We try the documented one and fall back,
        so whichever is live works.
        """
        try:
            payload = self._get(f"/valorant/v4/match/{region}/{match_id}")
        except HenrikError as exc:
            if "404" not in str(exc):
                raise
            log.debug("Two-segment v4 route 404'd, retrying with platform segment")
            payload = self._get(f"/valorant/v4/match/{region}/pc/{match_id}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise HenrikError(f"Unexpected match payload for {match_id}: no 'data' object")
        # The payload travels with the parse, so whatever stores this keeps
        # what the API actually said and not merely the fields we parse today.
        return Match.model_validate(data).with_raw(data)
