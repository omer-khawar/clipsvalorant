"""Twitch Helix client.

Only an app access token is needed for everything here - no user login, no scopes.
That matters, because the one thing an app token cannot do is create a clip on
someone else's VOD: POST /helix/videos/clips requires the broadcaster's or an
editor's token. So the output of this project is a timestamped link, not a clip.

Rate limiting is a token bucket per client ID. The documented example shows 800
points a minute but the docs do not promise a number, so we read Ratelimit-Remaining
and Ratelimit-Reset at runtime and pace off those.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"
OAUTH = "https://id.twitch.tv/oauth2/token"
USER_AGENT = "clipsvalorant/0.1 (personal match-to-VOD tool)"

_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


class TwitchError(RuntimeError):
    pass


def parse_duration(raw: str | None) -> int:
    """Twitch returns video durations as '3m21s' / '1h2m3s', not seconds."""
    if not raw:
        return 0
    match = _DURATION.fullmatch(raw.strip())
    if not match:
        return 0
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def to_epoch(iso: str | None) -> int:
    if not iso:
        return 0
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


@dataclass(frozen=True)
class Vod:
    id: str
    channel_id: str
    stream_id: str | None
    title: str
    created_at: str
    created_epoch: int
    duration_s: int
    url: str

    @property
    def end_epoch(self) -> int:
        return self.created_epoch + self.duration_s

    def overlap_s(self, start_epoch: int, end_epoch: int) -> int:
        """How many seconds of [start, end] this broadcast actually contains.

        A stream picked up two minutes into a match still holds every round after
        it, so overlap is the useful question; `covers` is the stricter one.
        """
        return max(0, min(self.end_epoch, end_epoch) - max(self.created_epoch, start_epoch))

    def covers(self, start_epoch: int, end_epoch: int, *, slack_s: int = 0) -> bool:
        """Was this broadcast running for the whole of [start, end]?

        `created_at` is documented only as "when the video was created"; community
        reports put it within seconds of the stream start. slack_s allows for that
        and for a match that started moments before the stream picked it up.
        """
        return (
            self.created_epoch - slack_s <= start_epoch
            and self.end_epoch + slack_s >= end_epoch
        )


class TwitchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise TwitchError(
                "Twitch credentials missing. Register an app at "
                "https://dev.twitch.tv/console/apps (the account needs 2FA enabled), "
                "then set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env"
            )
        self._id = client_id
        self._secret = client_secret
        self._token: str | None = None
        self._auth_lock = threading.Lock()
        self._remaining: int | None = None
        self._reset_at: float = 0.0
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})

    def __enter__(self) -> "TwitchClient":
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owns_client:
            self._client.close()

    # ---- auth ----------------------------------------------------------

    def _authenticate(self) -> None:
        resp = self._client.post(
            OAUTH,
            data={
                "client_id": self._id,
                "client_secret": self._secret,
                "grant_type": "client_credentials",
            },
        )
        if resp.status_code != 200:
            raise TwitchError(
                f"Could not get an app access token ({resp.status_code}). "
                "Check TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET."
            )
        self._token = resp.json()["access_token"]
        log.debug("Obtained a Twitch app access token")

    def _headers(self) -> dict[str, str]:
        # Guarded because evidence for a whole lobby is gathered on several
        # threads at once: without it, ten threads starting cold would each
        # ask for a token, and a 401 mid-run would set off the same stampede.
        # httpx.Client itself is safe to share; this is the one piece of
        # mutable state on the way through it.
        if self._token is None:
            with self._auth_lock:
                if self._token is None:
                    self._authenticate()
        return {"Authorization": f"Bearer {self._token}", "Client-Id": self._id}

    # ---- plumbing ------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(4):
            if self._remaining is not None and self._remaining <= 1:
                wait = max(self._reset_at - time.time(), 1.0)
                log.info("Twitch quota nearly spent, pausing %.0fs", wait)
                time.sleep(min(wait, 70))
                self._remaining = None

            resp = self._client.get(f"{HELIX}{path}", params=params, headers=self._headers())

            raw_remaining = resp.headers.get("ratelimit-remaining")
            if raw_remaining is not None:
                try:
                    self._remaining = int(raw_remaining)
                except ValueError:
                    self._remaining = None
            raw_reset = resp.headers.get("ratelimit-reset")
            if raw_reset:
                try:
                    self._reset_at = float(raw_reset)
                except ValueError:
                    pass

            if resp.status_code == 401:
                log.debug("Token rejected, re-authenticating")
                self._token = None
                continue
            if resp.status_code == 429:
                wait = max(self._reset_at - time.time(), 5.0)
                time.sleep(min(wait, 70))
                continue
            if resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise TwitchError(f"{resp.status_code} on {path}: {resp.text[:300]}")
            return resp.json()
        raise TwitchError(f"Gave up on {path}")

    def _paginate(self, path: str, params: dict[str, Any], *, limit: int) -> Iterator[dict]:
        cursor: str | None = None
        seen = 0
        while seen < limit:
            page = dict(params, first=min(100, limit - seen))
            if cursor:
                page["after"] = cursor
            payload = self._get(path, page)
            rows = payload.get("data", [])
            if not rows:
                return
            for row in rows:
                yield row
                seen += 1
            cursor = (payload.get("pagination") or {}).get("cursor")
            if not cursor:
                return

    # ---- endpoints -----------------------------------------------------

    def game_id(self, name: str = "VALORANT") -> str | None:
        for row in self._get("/search/categories", {"query": name, "first": 20}).get("data", []):
            if row.get("name", "").casefold() == name.casefold():
                return row["id"]
        return None

    def search_channels(self, query: str, *, limit: int = 20) -> list[dict]:
        """Matches Twitch login and display names - the zero-database name lookup."""
        return list(self._paginate("/search/channels", {"query": query}, limit=limit))

    def users(self, *, logins: list[str] | None = None, ids: list[str] | None = None) -> list[dict]:
        """Up to 100 per call. `description` is the channel bio, mined for NAME#TAG."""
        params: dict[str, Any] = {}
        if logins:
            params["login"] = logins[:100]
        if ids:
            params["id"] = ids[:100]
        if not params:
            return []
        return self._get("/users", params).get("data", [])

    def videos(self, channel_id: str, *, limit: int = 100, kind: str = "archive") -> list[Vod]:
        """Past broadcasts for one channel. No date filter exists - page and compare."""
        out: list[Vod] = []
        for row in self._paginate(
            "/videos", {"user_id": channel_id, "type": kind}, limit=limit
        ):
            created = row.get("created_at")
            out.append(
                Vod(
                    id=row["id"],
                    channel_id=channel_id,
                    stream_id=row.get("stream_id"),
                    title=row.get("title") or "",
                    created_at=created or "",
                    created_epoch=to_epoch(created),
                    duration_s=parse_duration(row.get("duration")),
                    url=row.get("url") or f"https://www.twitch.tv/videos/{row['id']}",
                )
            )
        return out

    def live_now(self, channel_ids: list[str]) -> dict[str, str]:
        """Which of these channels are live this second, and on what game.

        Returns channel id -> game_id. The only endpoint that answers
        authoritatively what a channel is playing, and it answers only for
        right now: there is no historical form, which is the whole reason the
        rest of this program reconstructs the past from clips and pixels.

        Useful on a match that has just ended, where the broadcast is still in
        progress. A title can lie and often does -- one broadcast here was
        titled "VCT OQ Watchparty" while showing Deadlock -- and this cannot.
        """
        out: dict[str, str] = {}
        for start in range(0, len(channel_ids), 100):
            chunk = channel_ids[start:start + 100]
            if not chunk:
                continue
            try:
                payload = self._get("/streams", {"user_id": chunk})
            except Exception:
                continue
            for row in payload.get("data", []):
                uid = row.get("user_id")
                if uid:
                    out[uid] = row.get("game_id") or ""
        return out

    def videos_by_id(self, video_ids: list[str]) -> list[Vod]:
        """Several VODs at once, by their own ids.

        The per-channel form asks "what has this channel broadcast?" and costs a
        call for every channel considered. But a clip already names the VOD it
        was cut from, and this endpoint takes a hundred video ids per request.
        Going from clips to VODs directly is therefore about two orders of
        magnitude cheaper than going via their broadcasters, which is what makes
        it affordable to check every channel that was live rather than only the
        ones whose name we liked.
        """
        out: list[Vod] = []
        for start in range(0, len(video_ids), 100):
            chunk = video_ids[start:start + 100]
            if not chunk:
                continue
            try:
                payload = self._get("/videos", {"id": chunk})
            except Exception:
                continue
            for row in payload.get("data", []):
                created = row.get("created_at")
                out.append(
                    Vod(
                        id=row["id"],
                        channel_id=row.get("user_id") or "",
                        stream_id=row.get("stream_id"),
                        title=row.get("title") or "",
                        created_at=created or "",
                        created_epoch=to_epoch(created),
                        duration_s=parse_duration(row.get("duration")),
                        url=row.get("url") or f"https://www.twitch.tv/videos/{row['id']}",
                    )
                )
        return out

    def clips_in_window(
        self, game_id: str, started_at: str, ended_at: str, *, limit: int = 200
    ) -> list[dict]:
        """Clips from ANY channel in a time window - a cheap candidate generator.

        Each clip carries broadcaster_id, video_id and vod_offset, so a viewer having
        clipped a moment hands us a precise timestamp for free. Biased toward
        streamers with an audience, which is exactly who we care about.
        """
        return list(
            self._paginate(
                "/clips",
                {"game_id": game_id, "started_at": started_at, "ended_at": ended_at},
                limit=limit,
            )
        )

    def channel_clips(
        self, broadcaster_id: str, started_at: str, ended_at: str, *, limit: int = 300
    ) -> list[dict]:
        """One channel's clips in a creation window, with game_id and vod_offset.

        This is the only route from an app token to "what game was this VOD?".
        GET /helix/videos does not carry a game id, and GET /helix/streams only
        answers for channels that are live right now. A clip, though, names its
        source video, its offset into it, and its category - so clips tied to a
        VOD reconstruct that broadcast's game timeline after the fact.
        """
        return list(
            self._paginate(
                "/clips",
                {
                    "broadcaster_id": broadcaster_id,
                    "started_at": started_at,
                    "ended_at": ended_at,
                },
                limit=limit,
            )
        )
