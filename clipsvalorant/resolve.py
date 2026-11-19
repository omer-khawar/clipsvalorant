"""Phase 2 - work out which players in a match were streaming, and where to look.

The funnel, cheapest first (PLAN.md section 0):

  Layer 2  alias table         one local query, free
  Layer 1  name match          ~1 Twitch call per player
  Layer 3  Riot ID harvest     bio + VOD titles, regex for NAME#TAG
  Layer 4  clip window         every Valorant clip made during the match

and then, over every candidate any layer produced, the part that actually decides:

  evidence  a VOD that covers the match, AND proof that the VOD contained
            Valorant during the match's own minutes.

That second clause is load-bearing. A VOD that merely spans the match proves only
that the channel was broadcasting something - which is true of any channel that
streams daily, and is how a lobby-mate called Curry got matched to twitch.tv/curry,
a radiant Valorant streamer who was, during that window, broadcasting Deadlock.
A name can collide; a broadcast clock cannot tell you what was on screen.

So the only thing allowed to say "confirmed" is a tie to *this* match: a clip cut
from the covering VOD, filed under Valorant, at an offset inside the match. Clips
carry video_id, vod_offset and game_id, which makes a past broadcast's game
timeline reconstructable after the fact - the one thing GET /helix/videos cannot
tell you. Everything weaker is reported as a candidate and never written to the
alias table, because a wrong alias is permanent: Layer 2 would hand it back first,
forever, and the mistake would look like history repeating as corroboration.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from clipsvalorant import calibrate, db
from clipsvalorant.config import Config
from clipsvalorant.models import Match, Player
from clipsvalorant.twitch import TwitchClient, Vod
from clipsvalorant.vision import Reader, wanted_names
from clipsvalorant.vod import FrameReader, missing_tools
from clipsvalorant.watch import CERTAIN_NAMES, MAX_FRAMES, watch

log = logging.getLogger(__name__)

# A stream may be picked up moments into agent select, so allow a little slack at
# the edges when asking whether a broadcast spans a match outright.
VOD_SLACK_S = 45

# A broadcast that holds most of the match is worth looking at even if it missed
# the start - the later rounds are still in it, and the link maths already refuses
# to point at a moment before the stream began. Curry's VOD began 129s after the
# match did; what ruled it out was having no Valorant in it, not the late start.
MIN_OVERLAP = 0.6

# Candidate generation is deliberately loose, because confirmation is strict.
# Requiring an exact name match was how CocaPup#slay -> twitch.tv/cocapuppy was
# missed entirely: the VOD was titled "short valo stream!!" and covered the whole
# match, but no candidate was ever created, so no evidence was ever weighed.
# The cap is only about cost - a 4-character Riot ID like "Nuke" resembles
# hundreds of channels, and each one costs a /videos call.
MIN_PREFIX_LEN = 5

# Twitch lookups run this many at a time. They are round trips, not work:
# the process is idle waiting for each one, and a lobby needs one name
# search per player and one broadcast listing per candidate. Helix allows
# 800 points a minute, so the ceiling here is politeness rather than quota.
LOOKUP_WORKERS = 8
# Enough of a match inside a broadcast to be worth opening it. Below this there
# is no room for a sampling point to land on anything.
MIN_WATCH_WINDOW_S = 90
# How soon after a match a broadcast might still be running. Inside this window
# /helix/streams can say what a channel is playing, which is the only
# authoritative game check that exists -- and the only one a title cannot lie
# to. Outside it the question is meaningless: everyone has gone to bed.
LIVE_CHECK_H = 6
MAX_PREFIX_CANDIDATES = 6

# How long after a broadcast people are still cutting clips out of its VOD.
CLIP_LOOKAHEAD_DAYS = 14

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# A Riot ID is a 3-16 character name, then #, then a 3-5 character tag. The tag
# length rule is what keeps "FINDING #1 HUSH" and "clip #42" out of the results.
# Names may contain spaces, so the text before a # cannot be split from prose with
# any certainty - keep every plausible tail and let the comparison decide.
_RIOT_ID = re.compile(r"([A-Za-z0-9][A-Za-z0-9 _.\-]{1,30})#([A-Za-z0-9]{3,5})(?!\w)")


def normalise(name: str) -> str:
    return _NON_ALNUM.sub("", name.casefold())


# A broadcast title is free and says what a VOD contains more often than not.
# It is only ever one signal among several: titles lie by omission, and a stream
# can change game mid-broadcast.
VALORANT_WORDS = ("valorant", "valo", "vct", "radiant", "immortal", "ranked val")
OTHER_GAMES = (
    "deadlock", "fortnite", "minecraft", "apex", "league of legends", "lol ",
    "cs2", "csgo", "counter-strike", "overwatch", "rocket league", "gta",
    "rust", "dead by daylight", "dbd", "roblox", "call of duty", "warzone",
    "marvel rivals", "elden ring", "tarkov", "hearthstone", "chess", "poker",
    "just chatting", "watchparty", "watch party", "react",
)


def _words(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip() + " "


NOT_PLAYING = ("watchparty", "watch party", "react", "reacting", "just chatting")


def game_from_title(title: str) -> tuple[bool, str]:
    """(mentions Valorant, name of another activity). Watching VCT is not playing."""
    padded = _words(title)
    for phrase in NOT_PLAYING:
        if f" {phrase.strip().replace(' ', ' ')} " in padded or phrase.strip() in padded:
            return False, phrase.strip()
    if any(f" {w.strip()} " in padded for w in VALORANT_WORDS):
        return True, ""
    for other in OTHER_GAMES:
        if f" {other.strip()} " in padded:
            return False, other.strip()
    return False, ""


def bio_names(bio: str | None, player_name: str) -> bool:
    """Does the channel bio name this player in plain words?

    "WELCOME!!! I am CocaPup!!" counts. An address does not: "curry@proxy.gg"
    and "curry.gg" contain the string without anybody claiming the name, and
    that distinction is the whole difference between finding a small streamer
    and re-confirming the wrong one.
    """
    if not bio or len(normalise(player_name)) < 4:
        return False
    cleaned = re.sub(r"\S+@\S+", " ", bio)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"[\w-]+\.[A-Za-z]{2,4}", " ", cleaned)
    return re.search(
        rf"(?<![A-Za-z0-9]){re.escape(player_name)}(?![A-Za-z0-9])", cleaned, re.I
    ) is not None


def riot_ids_in(text: str | None) -> set[str]:
    """Pull NAME#TAG out of a bio or stream title. Layer 3."""
    out: set[str] = set()
    for hit in _RIOT_ID.finditer(text or ""):
        blob, tag = hit.group(1), hit.group(2)
        words = blob.split()
        tails = [" ".join(words[i:]) for i in range(len(words))]
        plausible = [t for t in tails if 3 <= len(t) <= 16]
        if not plausible and blob:
            plausible = [blob.strip()[-16:]]
        out.update(f"{t}#{tag}" for t in plausible)
    return out


def _same_id(advertised: str, player: Player) -> bool:
    """`advertised` only confirms the player if the tag AND the name agree."""
    adv_name, _, adv_tag = advertised.rpartition("#")
    return normalise(adv_tag) == normalise(player.tag) and normalise(adv_name) == normalise(
        player.name
    )


def _tags_in(advertised: tuple[str, ...]) -> set[str]:
    return {normalise(a.rpartition("#")[2]) for a in advertised}


@dataclass
class Evidence:
    """Everything we know about one (player, channel) pair, and nothing we assume."""

    name_exact: bool = False
    name_prefix: bool = False
    vod_covers: bool = False
    vod_overlap_s: int = 0
    match_len_s: int = 0
    vod_late_start_s: int = 0
    vod_early_end_s: int = 0
    vod_title: str = ""
    clips_on_vod: int = 0
    valorant_clips_on_vod: int = 0
    clips_inside_match: int = 0
    map_named_in_clip: bool = False
    advertised_ids: tuple[str, ...] = ()
    id_confirms: bool = False
    id_conflicts: bool = False
    bio_names_player: bool = False
    title_says_valorant: bool = False
    title_says_other_game: str = ""
    # Layer 5 - what the broadcast itself showed. Everything above this line is
    # circumstantial: a name, a title, a window of wall-clock. These come from
    # opening the video and reading the screen, which is the only evidence that
    # answers the actual question.
    frames_read: int = 0
    live_on_valorant: bool = False
    lobby_names_seen: tuple[str, ...] = ()
    me_seen_in_vod: bool = False
    valorant_ui_frames: int = 0
    vod_not_this_match: bool = False

    @property
    def wrong_game(self) -> bool:
        """Clips exist for this broadcast and none of them are Valorant."""
        return self.clips_on_vod > 0 and self.valorant_clips_on_vod == 0

    @property
    def vod_shows_this_match(self) -> bool:
        """Enough of the lobby was named on screen to rule out coincidence.

        Ten people were in that match and we know all ten names. Seeing five of
        them on a stranger's broadcast is not something a shared friend group or
        a popular name can explain.
        """
        return len(self.lobby_names_seen) >= CERTAIN_NAMES

    @property
    def ties_to_this_match(self) -> bool:
        return (
            self.vod_shows_this_match or self.clips_inside_match > 0 or self.id_confirms
        )

    @property
    def vod_usable(self) -> bool:
        """Does a broadcast hold enough of the match to be worth a link?"""
        if self.vod_covers:
            return True
        return bool(self.match_len_s) and self.vod_overlap_s >= MIN_OVERLAP * self.match_len_s

    def confirmed_by(self) -> str | None:
        """Which signal, if any, ties this channel to this particular match."""
        if self.vod_shows_this_match:
            return (
                f"{len(self.lobby_names_seen)} of the other players in that lobby "
                "appear by name in the VOD itself"
            )
        if self.clips_inside_match > 0:
            return "clips cut from that VOD inside the match"
        if self.id_confirms:
            return "the Riot ID the channel advertises"
        return None

    @property
    def named_like_player(self) -> bool:
        return self.name_exact or self.name_prefix

    @property
    def looks_like_valorant(self) -> bool:
        return (
            self.valorant_clips_on_vod > 0
            or self.title_says_valorant
            or self.live_on_valorant
        )

    def verdict(self) -> str:
        # Layer 5: we opened the broadcast and read the lobby off the screen.
        # This sits above even the coverage test, because a stream that joined
        # late covers less of the match than the threshold wants and still
        # plainly *is* the match. Arithmetic about overlap is a guess about
        # what a broadcast contains; this is a measurement of it.
        if self.vod_shows_this_match:
            return "confirmed"
        if not self.vod_usable:
            return "rejected"
        # Decisive, and independent of how big the channel is.
        if self.clips_inside_match > 0 or self.id_confirms:
            return "confirmed"
        # We looked at the broadcast and our match was not in it. This outranks
        # every circumstantial signal below, which is the whole point of
        # looking: a name that matches and a bio that agrees are still only a
        # guess, and this is not.
        if self.vod_not_this_match:
            return "rejected"
        if self.wrong_game or self.title_says_other_game:
            return "rejected"
        if self.id_conflicts:
            # The channel advertises a Riot ID, and it is somebody else's.
            return "rejected"
        # Three independent agreements - the channel is named for this player,
        # its bio names them in plain words, and the broadcast was Valorant.
        if self.named_like_player and self.bio_names_player and self.looks_like_valorant:
            return "confirmed"
        if self.named_like_player and self.looks_like_valorant:
            return "likely"
        return "unconfirmed"

    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.name_exact:
            out.append("Twitch name matches the Riot ID exactly")
        elif self.name_prefix:
            out.append("Twitch name is a prefix-relative of the Riot ID")
        if self.vod_title:
            out.append(f'the broadcast is titled "{self.vod_title}"')
        if self.vod_covers:
            out.append("a VOD covers the whole match")
        elif self.vod_usable:
            out.append(
                f"a VOD holds {self.vod_overlap_s}s of the {self.match_len_s}s match"
            )
        else:
            out.append("no VOD holds enough of the match")
        if self.vod_late_start_s > 0:
            out.append(f"that stream began {self.vod_late_start_s}s after the match did")
        if self.vod_early_end_s > 0:
            out.append(f"that stream ended {self.vod_early_end_s}s before the match did")
        if self.clips_inside_match:
            out.append(
                f"{self.clips_inside_match} Valorant clip(s) cut from that VOD "
                "at offsets inside the match"
            )
        elif self.valorant_clips_on_vod:
            out.append(
                f"{self.valorant_clips_on_vod} Valorant clip(s) from that VOD, "
                "none during the match"
            )
        elif self.wrong_game:
            out.append(
                f"{self.clips_on_vod} clip(s) from that VOD and not one is Valorant - "
                "they were streaming something else"
            )
        else:
            out.append("nobody clipped that VOD, so its game is unknown")
        if self.live_on_valorant:
            out.append("that channel is live on Valorant right now")
        if self.title_says_valorant:
            out.append("that broadcast's title says Valorant")
        if self.title_says_other_game:
            out.append(f"that broadcast's title says {self.title_says_other_game}, not Valorant")
        if self.bio_names_player:
            out.append("the channel bio names this player in plain words")
        if self.map_named_in_clip:
            out.append("a clip title names this match's map")
        if self.lobby_names_seen:
            shown = ", ".join(sorted(self.lobby_names_seen)[:4])
            more = len(self.lobby_names_seen) - 4
            out.append(
                f"{len(self.lobby_names_seen)} of this lobby's players are named "
                f"on screen in that VOD ({shown}{f', +{more} more' if more > 0 else ''})"
            )
            if self.me_seen_in_vod:
                out.append("your own Riot name is on screen in that VOD")
        elif self.vod_not_this_match:
            out.append(
                f"we read {self.frames_read} frames of that VOD and found no sign "
                "of this match"
            )
        if self.id_confirms:
            out.append("the channel advertises this exact Riot ID")
        elif self.id_conflicts:
            out.append(f"the channel advertises a different Riot ID ({', '.join(self.advertised_ids)})")
        return out


@dataclass
class Candidate:
    match_id: str
    player: Player
    channel_id: str
    channel_login: str
    display_name: str
    source: str
    vod: Vod | None = None
    evidence: Evidence = field(default_factory=Evidence)

    @property
    def verdict(self) -> str:
        return self.evidence.verdict()

    @property
    def confidence(self) -> float:
        return {"confirmed": 0.95, "likely": 0.5, "unconfirmed": 0.25, "rejected": 0.0}[
            self.verdict
        ]


@dataclass
class ResolveReport:
    matches_checked: int = 0
    players_checked: int = 0
    name_candidates: int = 0
    clips_seen: int = 0
    clip_channels: int = 0
    bios_read: int = 0
    frames_read: int = 0
    calibrated: int = 0
    uncalibrated: int = 0
    # Of `calibrated`, how many needed no frames of their own because
    # the confirmation pass had already read enough.
    calibrated_free: int = 0
    confirmed: list[Candidate] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def file(self, cand: Candidate) -> None:
        {"confirmed": self.confirmed, "likely": self.candidates,
         "unconfirmed": self.candidates, "rejected": self.rejected}[cand.verdict].append(cand)

    def summary(self) -> str:
        return (
            f"{self.matches_checked} match(es), {self.players_checked} player(s) checked, "
            f"{self.name_candidates} name candidate(s), "
            f"{self.clips_seen} clip(s) from {self.clip_channels} channel(s), "
            f"{self.bios_read} bio(s) read, "
            + (f"{self.frames_read} VOD frame(s) read, " if self.frames_read else "")
            + f"{len(self.confirmed)} confirmed, {len(self.candidates)} unverified, "
            f"{len(self.rejected)} ruled out"
        )


@dataclass
class Watcher:
    """Layer 5's machinery, built once per run and reused across candidates."""

    frames: FrameReader
    reader: Reader
    limit: int = MAX_FRAMES

    @property
    def ready(self) -> bool:
        return self.reader.available


def build_watcher(cache_dir, *, limit: int = MAX_FRAMES) -> tuple["Watcher | None", str]:
    """A watcher, or None and the reason there isn't one.

    Layer 5 leans on two external programs and an optional OCR package. Any of
    them can be absent on a given machine, and that is not an error: every
    other layer still works, so we say what is missing and carry on.
    """
    missing = missing_tools()
    if missing:
        return None, f"{' and '.join(missing)} not on PATH"
    watcher = Watcher(FrameReader(cache_dir), Reader(), limit=limit)
    if not watcher.ready:
        return None, "no OCR engine (pip install 'clipsvalorant[vision]')"
    return watcher, ""


def vod_link(vod_id: str, offset_s: int) -> str:
    """A VOD deep link. Twitch wants h/m/s, and negative offsets are meaningless."""
    offset_s = max(offset_s, 0)
    hours, rest = divmod(offset_s, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"https://www.twitch.tv/videos/{vod_id}?t={hours:02d}h{minutes:02d}m{seconds:02d}s"


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _known_aliases(conn: sqlite3.Connection, name: str, tag: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.channel_id, a.source, a.confidence, c.login, c.display_name
        FROM alias a LEFT JOIN twitch_channel c ON c.id = a.channel_id
        WHERE a.riot_name = ? COLLATE NOCASE AND a.riot_tag = ? COLLATE NOCASE
        """,
        (name, tag),
    ).fetchall()


def _remember_channel(conn: sqlite3.Connection, row: dict) -> None:
    now = db.utcnow()
    conn.execute(
        """
        INSERT INTO twitch_channel (id, login, display_name, description, first_seen, last_seen)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            login = excluded.login,
            display_name = excluded.display_name,
            description = COALESCE(excluded.description, twitch_channel.description),
            last_seen = excluded.last_seen
        """,
        (
            row["id"],
            row.get("broadcaster_login") or row.get("login") or "",
            row.get("display_name"),
            row.get("description"),
            now,
            now,
        ),
    )


def _remember_vod(conn: sqlite3.Connection, vod: Vod) -> None:
    conn.execute(
        """
        INSERT INTO twitch_vod (id, channel_id, stream_id, title, created_at,
                                created_epoch, duration_s, end_epoch, url, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            duration_s = excluded.duration_s, end_epoch = excluded.end_epoch,
            title = excluded.title, fetched_at = excluded.fetched_at
        """,
        (
            vod.id, vod.channel_id, vod.stream_id, vod.title, vod.created_at,
            vod.created_epoch, vod.duration_s, vod.end_epoch, vod.url, db.utcnow(),
        ),
    )


def _remember_alias(
    conn: sqlite3.Connection, player: Player, channel_id: str, source: str, confidence: float
) -> None:
    """Only ever called for a confirmed tie. An alias is permanent, so a wrong one
    is worse than none: Layer 2 returns it first and skips every later check."""
    conn.execute(
        """
        INSERT INTO alias (riot_name, riot_tag, channel_id, source, confidence, confirmed_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(riot_name, riot_tag, channel_id) DO UPDATE SET
            source = excluded.source,
            confidence = MAX(alias.confidence, excluded.confidence),
            confirmed_at = excluded.confirmed_at
        """,
        (player.name, player.tag, channel_id, source, confidence, db.utcnow()),
    )


def _remember_encounter(
    conn: sqlite3.Connection, match_id: str, cand: Candidate, same_team: bool
) -> None:
    conn.execute(
        """
        INSERT INTO encounter (match_id, puuid, riot_id, channel_id, channel_login,
                               vod_id, their_team, same_team, confidence, verified_how,
                               verdict, evidence, found_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(match_id, puuid, channel_id) DO UPDATE SET
            vod_id = excluded.vod_id, confidence = excluded.confidence,
            verified_how = excluded.verified_how, verdict = excluded.verdict,
            evidence = excluded.evidence
        """,
        (
            match_id, cand.player.puuid, f"{cand.player.name}#{cand.player.tag}",
            cand.channel_id, cand.channel_login, cand.vod.id if cand.vod else None,
            cand.player.team, int(same_team), cand.confidence, cand.source,
            cand.verdict, json.dumps(cand.evidence.__dict__), db.utcnow(),
        ),
    )


def _prefix_related(a: str, b: str, *, min_len: int = 3) -> bool:
    """Is one normalised name a prefix of the other? 'vae' <-> 'vaerie'.

    Far weaker than an exact match, so it only ever *associates* a channel that
    independently proved it was live and being clipped on Valorant in the window.
    """
    a, b = normalise(a), normalise(b)
    if len(a) < min_len or len(b) < min_len:
        return False
    return a.startswith(b) or b.startswith(a)


def _prefetch_videos(
    client: TwitchClient, channel_ids: set[str], report: ResolveReport,
) -> dict[str, list[Vod] | str]:
    """Each channel's broadcast listing, fetched all at once.

    Maps channel id to its VODs, or to the reason it could not be fetched -
    which is kept rather than dropped, because "we could not look" and "there
    was nothing there" are the two answers this program most needs to tell
    apart.
    """
    out: dict[str, list[Vod] | str] = {}
    ids = sorted(channel_ids)
    if not ids:
        return out
    with ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(ids))) as pool:
        jobs = {pool.submit(client.videos, cid, limit=100): cid for cid in ids}
        for job, cid in jobs.items():
            try:
                out[cid] = job.result()
            except Exception as exc:
                out[cid] = str(exc)
    return out


def gather_evidence(
    client: TwitchClient,
    conn: sqlite3.Connection,
    cand: Candidate,
    match: Match,
    start: int,
    end: int,
    game_id: str | None,
    report: ResolveReport,
    listings: dict[str, list[Vod] | str] | None = None,
) -> None:
    """Fill in cand.evidence: find a covering VOD, then ask what was on screen.

    `listings` is this channel's broadcasts if somebody already fetched them.
    The call is made here when they did not, so this function still works on
    its own - the audit path calls it that way.
    """
    ev = cand.evidence
    ready = (listings or {}).get(cand.channel_id)
    if isinstance(ready, str):  # the fetch failed, and we know why
        report.errors.append(f"{cand.channel_login}: videos failed: {ready}")
        return
    if ready is not None:
        vods = ready
    else:
        try:
            vods = client.videos(cand.channel_id, limit=100)
        except Exception as exc:
            report.errors.append(f"{cand.channel_login}: videos failed: {exc}")
            return
    for vod in vods:
        _remember_vod(conn, vod)

    # The best broadcast is simply the one holding the most of the match.
    ev.match_len_s = max(end - start, 0)
    covering = max(vods, key=lambda v: v.overlap_s(start, end), default=None)
    if covering is None or covering.overlap_s(start, end) == 0:
        return
    cand.vod = covering
    ev.vod_covers = covering.covers(start, end, slack_s=VOD_SLACK_S)
    ev.vod_overlap_s = covering.overlap_s(start, end)
    ev.vod_late_start_s = max(covering.created_epoch - start, 0)
    ev.vod_early_end_s = max(end - covering.end_epoch, 0)
    ev.vod_title = covering.title
    if not ev.vod_usable:
        return

    says_valorant, other_game = game_from_title(covering.title)
    ev.title_says_valorant = says_valorant
    ev.title_says_other_game = other_game

    # Layer 3 - does the channel publish a Riot ID anywhere we can read?
    texts = [v.title for v in vods[:20]]
    try:
        for user in client.users(ids=[cand.channel_id]):
            _remember_channel(conn, user)
            bio = user.get("description") or ""
            texts.append(bio)
            # A bio naming the player in plain words - "I am CocaPup!!" - is the
            # signal that reaches small streamers, who are never clipped and so
            # can never satisfy the clip test however long we wait.
            if bio_names(bio, cand.player.name):
                ev.bio_names_player = True
    except Exception as exc:
        report.errors.append(f"{cand.channel_login}: users failed: {exc}")
    found: set[str] = set()
    for text in texts:
        found |= riot_ids_in(text)
    if found:
        ev.advertised_ids = tuple(sorted(found))
        if any(_same_id(rid, cand.player) for rid in found):
            ev.id_confirms = True
        elif normalise(cand.player.tag) not in _tags_in(ev.advertised_ids):
            # Every Riot ID this channel publishes belongs to a different tagline.
            ev.id_conflicts = True

    # What game was this broadcast? GET /helix/videos will not say, but clips cut
    # from it will: each names its source video, its offset and its category.
    if game_id:
        try:
            clips = client.channel_clips(
                cand.channel_id,
                _iso(covering.created_epoch),
                _iso(covering.end_epoch + CLIP_LOOKAHEAD_DAYS * 86400),
            )
        except Exception as exc:
            report.errors.append(f"{cand.channel_login}: clips failed: {exc}")
            return
        tied = [c for c in clips if str(c.get("video_id") or "") == covering.id]
        ev.clips_on_vod = len(tied)
        valorant = [c for c in tied if str(c.get("game_id") or "") == str(game_id)]
        ev.valorant_clips_on_vod = len(valorant)
        lo, hi = start - covering.created_epoch, end - covering.created_epoch
        inside = [
            c for c in valorant
            if c.get("vod_offset") is not None and lo <= int(c["vod_offset"]) <= hi
        ]
        ev.clips_inside_match = len(inside)
        map_name = (match.metadata.map_name or "").casefold()
        if map_name:
            ev.map_named_in_clip = any(
                map_name in (c.get("title") or "").casefold() for c in inside
            )


def clip_derived_candidates(
    conn: sqlite3.Connection,
    client: TwitchClient,
    match: Match,
    unresolved: list[Player],
    start: int,
    end: int,
    game_id: str,
    report: ResolveReport,
) -> list[Candidate]:
    """Layer 4 - candidates from clips made during the match window.

    GET /helix/clips takes game_id plus started_at/ended_at, so a couple of calls
    return every Valorant clip created worldwide in those minutes, each carrying a
    broadcaster. That is a candidate pool biased toward streamers with an audience,
    and it needs no name database at all.
    """
    if not unresolved:
        return []
    try:
        clips = client.clips_in_window(game_id, _iso(start), _iso(end + 60), limit=500)
    except Exception as exc:
        report.errors.append(f"clip window lookup failed: {exc}")
        return []

    report.clips_seen += len(clips)
    casters: dict[str, str] = {}
    display: dict[str, str] = {}
    for clip in clips:
        bid = clip.get("broadcaster_id")
        if bid:
            casters[bid] = clip.get("broadcaster_login") or clip.get("broadcaster_name") or ""
            display[bid] = clip.get("broadcaster_name") or ""
    report.clip_channels += len(casters)

    # Layer 3 over the clip pool. Prefix matching alone would hand the name problem
    # straight back: it only reaches channels that already look like the Riot ID. A
    # bio does not. /helix/users takes 100 ids at a time, so reading every candidate
    # channel's bio costs about two calls for a whole lobby, and a channel that was
    # being clipped on Valorant across the match AND publishes a lobby member's Riot
    # ID is tied to that player by something stronger than a resemblance.
    bios: dict[str, set[str]] = {}
    ids = list(casters)
    for start_i in range(0, len(ids), 100):
        try:
            for user in client.users(ids=ids[start_i:start_i + 100]):
                _remember_channel(conn, user)
                found = riot_ids_in(user.get("description") or "")
                if found:
                    bios[user["id"]] = found
        except Exception as exc:
            report.errors.append(f"clip-pool bios failed: {exc}")
            break
    report.bios_read += len(ids)

    out: list[Candidate] = []
    for bid, login in casters.items():
        player = next(
            (p for p in unresolved
             if any(_same_id(rid, p) for rid in bios.get(bid, ()))),
            None,
        )
        source = "clip_window+bio"
        if player is None:
            # Weaker: the channel name resembles the Riot ID. Only ever acted on
            # because the channel independently proved it was live on Valorant here.
            player = next(
                (p for p in unresolved
                 if _prefix_related(p.name, login) or _prefix_related(p.name, display.get(bid, ""))),
                None,
            )
            source = "clip_window"
        if player is None:
            continue
        cand = Candidate(match.id, player, bid, login, display.get(bid) or login, source)
        cand.evidence.name_prefix = source == "clip_window"
        _remember_channel(conn, {"id": bid, "broadcaster_login": login,
                                 "display_name": display.get(bid) or login})
        out.append(cand)
    return out


def resolve_match(
    conn: sqlite3.Connection,
    client: TwitchClient,
    match: Match,
    *,
    my_puuid: str,
    report: ResolveReport,
    game_id: str | None = None,
    watcher: "Watcher | None" = None,
) -> None:
    started = match.metadata.started_at
    start = int(
        (started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started).timestamp()
    )
    end = start + (match.metadata.game_length_in_ms or 0) // 1000

    me = match.player_by_puuid(my_puuid)
    my_team = me.team if me else None
    unresolved: list[Player] = []

    # Layer 5 needs to know who to look for and when to look. Both are free:
    # the lobby is the match history, and the kill times tell us the moments
    # when Valorant was painting names on the screen.
    lobby = wanted_names([(p.name, p.puuid == my_puuid) for p in match.players])
    my_name = me.name if me else ""
    # Calibration dates a frame by reading the countdown on it, which only
    # means anything against the times the rounds actually began.
    round_starts = calibrate.round_starts(match.kills)
    kills = [
        (row["round"], row["time_in_match_ms"] / 1000)
        for row in conn.execute(
            "SELECT round, time_in_match_ms FROM kill WHERE match_id = ? "
            "ORDER BY time_in_match_ms",
            (match.id,),
        )
    ]

    # Frames `inspect` parsed, per channel, for `calibrate_vod` to reuse.
    watched: dict[str, list[tuple[float, object]]] = {}

    def inspect(cand: Candidate) -> None:
        """Open the broadcast and see whether this match is in it.

        Worth doing for any broadcast holding enough of the match to contain a
        sampling point, not merely those that clear the coverage threshold. A
        stream that joined late fails that threshold and can still show the
        lobby on screen, and what is on screen beats what the arithmetic
        guessed.
        """
        if watcher is None or cand.vod is None:
            return
        lo = max(0.0, cand.vod.created_epoch - start)
        hi = min(end, cand.vod.end_epoch) - start
        if hi - lo < MIN_WATCH_WINDOW_S:
            return
        seen = watch(
            watcher.reader, watcher.frames, cand.vod.id,
            start - cand.vod.created_epoch, kills, lobby, my_name,
            limit=watcher.limit, window=(lo, hi),
            exclude_name=cand.player.name,
        )
        ev = cand.evidence
        ev.frames_read = seen.frames_read
        ev.lobby_names_seen = tuple(sorted(seen.names))
        ev.me_seen_in_vod = seen.me_seen
        ev.valorant_ui_frames = seen.valorant_frames
        ev.vod_not_this_match = seen.saw_nothing
        report.frames_read += seen.frames_read
        # Hand the parsed frames on. Calibration wants a round countdown off
        # frames exactly like these, and paying twice to read one broadcast
        # was most of what made a scan slow.
        watched[cand.channel_id] = seen.reads

    def calibrate_vod(cand: Candidate) -> None:
        """Measure where this match really starts in this broadcast.

        Only for candidates that survived, and only once per broadcast. It
        costs around a dozen frames, which is the same order as the check
        that confirmed them - worth paying for a link that lands on the
        play, and pure waste on the ninety-odd channels ruled out.
        """
        if watcher is None or cand.vod is None or cand.verdict == "rejected":
            return
        if calibrate.load(conn, match.id, cand.vod.id).measured:
            return
        base = start - cand.vod.created_epoch
        lo = max(0.0, cand.vod.created_epoch - start) + base
        hi = min(end, cand.vod.end_epoch) - start + base
        # The cheap way first: frames `inspect` already read, OCR and all.
        # On a streamer whose broadcast was confirmed from the screen, those
        # frames carry a countdown often enough to settle this for nothing.
        cal = calibrate.from_reads(
            watched.get(cand.channel_id, ()),
            base=base, starts=round_starts, vod_id=cand.vod.id,
        )
        if cal.measured:
            report.calibrated_free += 1
        else:
            cal = calibrate.measure(
                watcher.reader, watcher.frames, cand.vod.id,
                base=base, starts=round_starts, window=(lo, hi),
            )
        if cal.measured:
            calibrate.store(conn, match.id, cand.vod.id, cal)
            report.calibrated += 1
            log.info("%s starts %.0fs into VOD %s beyond its own timestamp "
                     "(%d frames agreed)", match.id[:8], cal.shift_s,
                     cand.vod.id, cal.votes)
        else:
            report.uncalibrated += 1

    def settle(cand: Candidate) -> None:
        """Everything after the evidence is in: look, judge, write it down."""
        inspect(cand)
        _remember_encounter(conn, match.id, cand, cand.player.team == my_team)
        if cand.verdict == "confirmed":
            _remember_alias(conn, cand.player, cand.channel_id,
                            cand.source.split(":")[-1], cand.confidence)
        calibrate_vod(cand)
        report.file(cand)

    def open_broadcasts(cands: list[Candidate]) -> None:
        """Resolve every playlist we are about to need, together.

        `inspect` opens a VOD by asking yt-dlp for its HLS URL, which takes
        about two and a half seconds and is almost all waiting - half of it
        is yt-dlp's own interpreter starting. Left inside the loop below,
        one lobby pays that once per candidate, in a row. The VODs are all
        known by this point, so they are resolved at once instead and every
        `inspect` after this finds its playlist already in hand.
        """
        if watcher is None:
            return
        watcher.frames.prefetch_playlists(
            c.vod.id for c in cands if c.vod is not None
        )

    others = [p for p in match.players if p.puuid != my_puuid]

    # Layer 1's name search is one HTTP round trip per player and nine of them
    # in a row is nine round trips of waiting. They do not depend on each
    # other, so they go out together; everything that touches the database
    # still happens below, on this thread, in lobby order.
    aliases = {p.puuid: _known_aliases(conn, p.name, p.tag) for p in others}
    searches: dict[str, list[dict]] = {}
    unsearchable: dict[str, str] = {}
    to_search = [p for p in others if not aliases[p.puuid]]
    if to_search:
        with ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(to_search))) as pool:
            jobs = {
                pool.submit(client.search_channels, p.name, limit=40): p
                for p in to_search
            }
            for job, player in jobs.items():
                try:
                    searches[player.puuid] = job.result()
                except Exception as exc:
                    unsearchable[player.puuid] = str(exc)

    # Built for the whole lobby first, so the broadcast listings they all need
    # can be fetched together rather than one at a time inside the loop.
    per_player: list[tuple[Player, list[Candidate]]] = []

    for player in others:
        report.players_checked += 1

        candidates: list[Candidate] = []

        # Layer 2 - anything already confirmed about this Riot ID.
        for row in aliases[player.puuid]:
            cand = Candidate(match.id, player, row["channel_id"], row["login"] or "",
                             row["display_name"] or "", f"alias:{row['source']}")
            cand.evidence.name_exact = normalise(row["login"] or "") == normalise(player.name)
            candidates.append(cand)

        # Layer 1 - Twitch name search. Zero database, catches the easy cases -
        # and collides with every established streamer who shares a short name.
        if not candidates:
            if player.puuid in unsearchable:
                report.errors.append(
                    f"{player.name}#{player.tag}: search failed: "
                    f"{unsearchable[player.puuid]}"
                )
                continue
            target = normalise(player.name)
            hits = searches.get(player.puuid, [])
            near = 0
            for hit in hits:
                login = hit.get("broadcaster_login", "")
                display = hit.get("display_name", "")
                exact = normalise(login) == target or normalise(display) == target
                # Twitch returns search results by relevance, so the first few
                # near-misses are the plausible ones.
                prefix = not exact and len(target) >= MIN_PREFIX_LEN and (
                    _prefix_related(player.name, login, min_len=MIN_PREFIX_LEN)
                    or _prefix_related(player.name, display, min_len=MIN_PREFIX_LEN)
                )
                if prefix:
                    if near >= MAX_PREFIX_CANDIDATES:
                        continue
                    near += 1
                if not (exact or prefix):
                    continue
                _remember_channel(conn, hit)
                cand = Candidate(match.id, player, hit["id"], login, display,
                                 "name_match" if exact else "name_prefix")
                cand.evidence.name_exact = exact
                cand.evidence.name_prefix = prefix
                candidates.append(cand)
            report.name_candidates += len(candidates)

        # One call per player, and only while a broadcast could still be
        # running. Asked of a match from last week it would answer about
        # whatever the channel is playing today, which says nothing at all.
        if candidates and game_id and (time.time() - end) < LIVE_CHECK_H * 3600:
            try:
                live = client.live_now([c.channel_id for c in candidates])
            except Exception:
                live = {}
            for cand in candidates:
                cand.evidence.live_on_valorant = live.get(cand.channel_id) == game_id

        per_player.append((player, candidates))

    # Every candidate needs its channel's broadcast listing, and that is the
    # single most-made call in the program: one per candidate, and most
    # lobbies produce dozens. Serially that is dozens of round trips spent
    # waiting; fetched together it is one. Distinct channels only, because a
    # name search routinely returns the same big streamer for several
    # players, and the result is handed to gather_evidence rather than
    # fetched again there.
    listings = _prefetch_videos(
        client, {c.channel_id for _, cs in per_player for c in cs}, report
    )

    # Evidence first for the whole lobby, because it is what decides which
    # broadcasts are worth opening - and only then are those opened, all at
    # once. Judging still happens below, candidate by candidate in lobby
    # order, so nothing about the verdicts or what is written down changes.
    for _player, candidates in per_player:
        for cand in candidates:
            gather_evidence(client, conn, cand, match, start, end, game_id,
                            report, listings)
    open_broadcasts([c for _, cs in per_player for c in cs])

    for player, candidates in per_player:
        for cand in candidates:
            settle(cand)

        if not any(c.player.puuid == player.puuid for c in report.confirmed):
            unresolved.append(player)

    # Layer 4 - anyone name matching could not reach.
    if unresolved and game_id:
        late = list(clip_derived_candidates(
            conn, client, match, unresolved, start, end, game_id, report
        ))
        for cand in late:
            gather_evidence(client, conn, cand, match, start, end, game_id,
                            report)
        open_broadcasts(late)
        for cand in late:
            settle(cand)


def audit_verdicts(
    cfg: Config, *, limit: int = 100, frames: int = MAX_FRAMES * 2
) -> tuple[list[str], list[str]]:
    """Re-open broadcasts nobody looked at properly, and settle them.

    Rejections are not all equal, and the difference matters. Nearly all of
    them say only "no broadcast of this match survived", which no amount of
    looking can overturn - there is no video. The interesting ones are the
    handful we actually judged: a title that named another game, clips that
    were all something else, a screen we read too few frames of. Those were
    decided on circumstance, and a missed streamer could only be hiding there.

    Unconfirmed rows need the same treatment for the opposite reason. They
    are not a judgement either - they are the absence of one, left behind
    when a broadcast was never read, and they are the more dangerous kind.
    A rejection that was wrong hides a streamer; an unconfirmed row that is
    wrong *shows* one, with timestamps beside it that look exactly like the
    confirmed sort. twitch.tv/solly17x sat in this state for a fortnight on
    nothing but a name that begins the same way and a stream that happened to
    be running - it was Elden Ring throughout.

    So this walks both sets, re-reads each broadcast with twice the usual
    frame budget, confirms the ones that turn out to be the match, and rules
    out the unconfirmed ones that plainly are not. Returns (flips, notes).
    """
    flips: list[str] = []
    notes: list[str] = []
    watcher, why = build_watcher(cfg.db_path.parent / "frames", limit=frames)
    if watcher is None:
        return flips, [f"cannot audit: {why}"]

    with db.session(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT e.id, e.match_id, e.riot_id, e.channel_login, e.vod_id, "
            "       e.evidence, e.verdict, m.started_at_epoch AS start, m.game_length_ms, "
            "       v.created_epoch, v.end_epoch, v.title "
            "FROM encounter e "
            "JOIN match m ON m.id = e.match_id "
            "JOIN twitch_vod v ON v.id = e.vod_id "
            "WHERE e.verdict IN ('rejected', 'unconfirmed') "
            "  AND e.vod_id IS NOT NULL "
            "ORDER BY m.started_at_epoch DESC LIMIT ?",
            (limit,),
        ).fetchall()

        checked = 0
        for row in rows:
            start = row["start"]
            end = start + (row["game_length_ms"] or 0) // 1000
            lo = max(0.0, row["created_epoch"] - start)
            hi = min(end, row["end_epoch"]) - start
            if hi - lo < MIN_WATCH_WINDOW_S:
                continue  # no video of this match to look at: not a judgement call
            players = [
                (r["name"], bool(r["is_me"]))
                for r in conn.execute(
                    "SELECT name, is_me FROM match_player WHERE match_id = ?",
                    (row["match_id"],),
                )
            ]
            lobby = wanted_names(players)
            my_name = next((n for n, mine in players if mine), "")
            kills = [
                (k["round"], k["time_in_match_ms"] / 1000)
                for k in conn.execute(
                    "SELECT round, time_in_match_ms FROM kill WHERE match_id = ? "
                    "ORDER BY time_in_match_ms",
                    (row["match_id"],),
                )
            ]
            checked += 1
            seen = watch(
                watcher.reader, watcher.frames, row["vod_id"],
                start - row["created_epoch"], kills, lobby, my_name,
                limit=frames, window=(lo, hi),
                # The same exclusion the live path makes, and for the same
                # reason: this channel is a candidate because it is named
                # like the player, so its own overlay agreeing with itself
                # is not evidence of anything.
                exclude_name=row["riot_id"].partition("#")[0],
            )
            if len(seen.names) >= CERTAIN_NAMES:
                flips.append(
                    f"{row['riot_id']} -> twitch.tv/{row['channel_login']}: "
                    f"ruled out, but {len(seen.names)} of the lobby are on screen "
                    f"({', '.join(sorted(seen.names)[:4])})"
                )
                conn.execute(
                    "UPDATE encounter SET verdict = 'confirmed', "
                    "verified_how = 'audit:vod_names' WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
            elif row["verdict"] == "unconfirmed" and seen.saw_nothing:
                notes.append(
                    f"{row['riot_id']} -> twitch.tv/{row['channel_login']}: "
                    f"read {seen.frames_read} frames and found no Valorant at all "
                    "- ruled out"
                )
                conn.execute(
                    "UPDATE encounter SET verdict = 'rejected', "
                    "verified_how = 'audit:not_this_match' WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
            elif seen.names:
                notes.append(
                    f"{row['riot_id']} -> twitch.tv/{row['channel_login']}: "
                    f"{len(seen.names)} name(s) seen, short of the {CERTAIN_NAMES} "
                    "needed - worth a human look"
                )
        notes.insert(
            0,
            f"{len(rows)} undecided encounter(s) had a broadcast on file; {checked} "
            "held enough of the match to be worth re-reading",
        )
    return flips, notes


def resolve_recent(
    cfg: Config,
    *,
    limit: int = 5,
    match_id: str | None = None,
    match_ids: list[str] | None = None,
    watch_vods: bool = True,
    on_match: Callable[[str, "ResolveReport"], None] | None = None,
) -> ResolveReport:
    """Resolve recent matches, optionally reporting after each one.

    `on_match` exists for callers that have somebody waiting: reading a single
    broadcast can take a minute, so a UI that only hears the final total looks
    hung for as long as the work takes. It is handed the match id and the
    running report, and anything it raises is swallowed - a progress bar is
    never a reason to lose a resolve that already happened.
    """
    report = ResolveReport()
    with db.session(cfg.db_path) as conn:
        if match_ids is not None:
            if not match_ids:
                return report
            holes = ",".join("?" * len(match_ids))
            rows = conn.execute(
                f"SELECT id, raw_json FROM match WHERE id IN ({holes}) "
                "ORDER BY started_at_epoch DESC",
                match_ids,
            ).fetchall()
        elif match_id:
            rows = conn.execute(
                "SELECT id, raw_json FROM match WHERE id LIKE ?", (f"{match_id}%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, raw_json FROM match ORDER BY started_at_epoch DESC LIMIT ?",
                (limit,),
            ).fetchall()

        if not rows:
            report.errors.append("No matching match in the database.")
            return report

        watcher = None
        if watch_vods:
            watcher, why = build_watcher(cfg.db_path.parent / "frames")
            if watcher is None:
                report.errors.append(f"not reading VODs: {why}")

        with TwitchClient(cfg.twitch_client_id, cfg.twitch_client_secret) as client:
            try:
                game_id = client.game_id("VALORANT")
            except Exception as exc:
                report.errors.append(f"game id lookup failed: {exc}")
                game_id = None
            for row in rows:
                match = Match.model_validate(json.loads(row["raw_json"]))
                me = match.find_me(cfg.riot_name, cfg.riot_tag)
                if me is None:
                    continue
                report.matches_checked += 1
                resolve_match(conn, client, match, my_puuid=me.puuid, report=report,
                              game_id=game_id, watcher=watcher)
                conn.execute(
                    "UPDATE match SET last_resolved_at = ?, "
                    "resolve_attempts = resolve_attempts + 1 WHERE id = ?",
                    (db.utcnow(), row["id"]),
                )
                conn.commit()
                if on_match is not None:
                    try:
                        on_match(row["id"], report)
                    except Exception:
                        log.debug("progress callback failed", exc_info=True)
    return report
