"""Turn a match's kill list into the handful of moments worth watching.

Pure functions over the parsed Match model - no I/O, no network - so this is the
part of the pipeline that is fully testable offline.

Definitions follow tracker.gg's, because tracker states them explicitly in its own
tag descriptions and is therefore a usable oracle (see tools/compare_tracker.py):

  * Multi-kills count RAW kills on opponents in a round, not distinct bodies.
    tracker has a "6k" tag described as "Killed 6 enemies in one round", which is
    impossible with five distinct enemies - a resurrection makes the sixth. We keep
    the distinct-body count alongside, since it is the more meaningful number.
  * Teamkills and self-kills never count: tracker says "enemies".
  * A clutch needs NO minimum kills. tracker's "Won a 1v1 clutch" fires when you
    were last alive against one opponent and won the round. Won and lost are
    separate kinds, matching tracker's clutch1v1 and clutchLost1v1.
  * Respawn modes (Deathmatch, Escalation) report every kill under round 0, so
    per-round counts are meaningless there. Match.has_rounds gates them. Without
    that gate a single Deathmatch produced 12 phantom aces.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from clipsvalorant.models import Kill, Match, RoundPlayer

# opponent kills in a round -> (kind, label), following tracker.gg's naming
MULTI_KILL = {
    3: ("triple", "3K"),
    4: ("quadra", "4K"),
    5: ("ace", "Ace"),
}

MIN_MULTI_KILLS = 3


def _multi(count: int) -> tuple[str, str]:
    """Name a multi-kill. Beyond an ace tracker just counts: 6k, 7k, ..."""
    return MULTI_KILL.get(count, ("multi", f"{count}K"))


@dataclass(frozen=True)
class Highlight:
    round: int
    kind: str
    label: str
    kill_count: int           # raw opponent kills, comparable to tracker.gg
    time_in_match_ms: int
    end_time_in_match_ms: int
    round_won: bool | None
    distinct_victims: int = 0  # how many different opponents; < kill_count after a rez
    ceremony: str | None = None

    @property
    def timestamp(self) -> str:
        total = self.time_in_match_ms // 1000
        return f"{total // 60:02d}:{total % 60:02d}"


def is_player(rp: RoundPlayer, puuid: str, name: str = "", tag: str = "") -> bool:
    """Identify a player in a kill event, preferring puuid and falling back to Riot ID."""
    if rp.puuid and puuid:
        return rp.puuid == puuid
    if name and rp.name and tag and rp.tag:
        return (rp.name.casefold(), rp.tag.casefold()) == (name.casefold(), tag.casefold())
    return False


def kills_by_round(match: Match) -> dict[int, list[Kill]]:
    grouped: dict[int, list[Kill]] = defaultdict(list)
    for kill in match.kills:
        grouped[kill.round].append(kill)
    for bucket in grouped.values():
        bucket.sort(key=lambda k: k.time_in_match_in_ms)
    return dict(grouped)


def revived_in_round(round_kills: list[Kill]) -> set[str]:
    """Players who landed a kill after their own first death - so were revived.

    Valorant has Sage's resurrect and Clove's self-revive, so "died once" is not the
    same as "dead now". Kept for diagnostics; the liveness model below is what the
    detection actually uses, because a revived player can die a second time.
    """
    first_death: dict[str, int] = {}
    for kill in round_kills:
        victim = kill.victim.puuid
        if not victim:
            continue
        prior = first_death.get(victim)
        if prior is None or kill.time_in_match_in_ms < prior:
            first_death[victim] = kill.time_in_match_in_ms

    revived: set[str] = set()
    for kill in round_kills:
        killer = kill.killer.puuid
        if not killer or killer == kill.victim.puuid:
            continue
        death = first_death.get(killer)
        if death is not None and kill.time_in_match_in_ms > death:
            revived.add(killer)
    return revived


def _timeline(round_kills: list[Kill]) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """-> (death times per player, kill times per player). Self-kills are deaths only."""
    deaths: dict[str, list[int]] = defaultdict(list)
    kills: dict[str, list[int]] = defaultdict(list)
    for kill in round_kills:
        if kill.victim.puuid:
            deaths[kill.victim.puuid].append(kill.time_in_match_in_ms)
        if kill.killer.puuid and kill.killer.puuid != kill.victim.puuid:
            kills[kill.killer.puuid].append(kill.time_in_match_in_ms)
    return deaths, kills


def _dead_at(
    pool: set[str],
    when: int,
    deaths: dict[str, list[int]],
    kills: dict[str, list[int]]) -> set[str]:
    """Which of `pool` are dead at `when`.

    A player is dead if their most recent death at or before `when` was not followed
    by a kill of theirs in that window - a kill after dying is the only proof of a
    revive available in the kill feed. This handles dying, being revived, and dying
    again, which a blanket "revived means never dead" rule gets wrong.

    Limitation: a player revived who never kills again looks dead to us.
    """
    out: set[str] = set()
    for player in pool:
        prior = [t for t in deaths.get(player, []) if t <= when]
        if not prior:
            continue
        last_death = max(prior)
        if any(last_death < t <= when for t in kills.get(player, [])):
            continue  # revived after that death
        out.add(player)
    return out


def _first_kill_per_victim(kills: list[Kill], victim_pool: set[str]) -> list[Kill]:
    """One kill per distinct victim drawn from victim_pool, earliest first.

    victim_pool is the opponent set, so team kills and self-inflicted deaths
    cannot inflate a multi-kill.
    """
    seen: dict[str, Kill] = {}
    for kill in sorted(kills, key=lambda k: k.time_in_match_in_ms):
        key = kill.victim.puuid
        if key and key in victim_pool and key not in seen:
            seen[key] = kill
    return sorted(seen.values(), key=lambda k: k.time_in_match_in_ms)


def find_highlights(match: Match, my_puuid: str) -> list[Highlight]:
    """Every ace, 4K, 3K and clutch the given player had in this match."""
    me = match.player_by_puuid(my_puuid)
    if me is None or not match.has_rounds:
        return []

    my_team = me.team
    teammates = {p.puuid for p in match.teammates_of(my_puuid)}
    opponents = {p.puuid for p in match.opponents_of(my_puuid)}
    if not opponents:
        return []

    highlights: list[Highlight] = []

    for round_no, round_kills in sorted(kills_by_round(match).items()):
        mine = [k for k in round_kills if is_player(k.killer, my_puuid, me.name, me.tag)]
        if not mine:
            continue

        winner = match.round_winner(round_no)
        round_won = (winner == my_team) if (winner and my_team) else None
        ceremony = match.round_ceremony(round_no)

        # Raw kills on opponents, in time order. Teamkills and self-kills excluded.
        on_enemies = sorted(
            (k for k in mine if k.victim.puuid in opponents),
            key=lambda k: k.time_in_match_in_ms,
        )
        distinct_count = len({k.victim.puuid for k in on_enemies})

        if len(on_enemies) >= MIN_MULTI_KILLS:
            kind, label = _multi(len(on_enemies))
            highlights.append(
                Highlight(
                    round=round_no,
                    kind=kind,
                    label=label,
                    kill_count=len(on_enemies),
                    time_in_match_ms=on_enemies[0].time_in_match_in_ms,
                    end_time_in_match_ms=on_enemies[-1].time_in_match_in_ms,
                    round_won=round_won,
                    distinct_victims=distinct_count,
                    ceremony=ceremony,
                )
            )

        clutch = _detect_clutch(
            round_kills, on_enemies, my_puuid, me.name, me.tag, teammates, opponents
        )
        if clutch is not None:
            enemies_alive, clutch_kills, became_alone_ms = clutch
            won = bool(round_won)
            # No kills after being left alone still leaves a watchable moment: the
            # point at which it became 1vN. Anchor there.
            start = clutch_kills[0].time_in_match_in_ms if clutch_kills else became_alone_ms
            end = clutch_kills[-1].time_in_match_in_ms if clutch_kills else became_alone_ms
            highlights.append(
                Highlight(
                    round=round_no,
                    kind="clutch" if won else "clutch_lost",
                    label=f"1v{enemies_alive} {'clutch' if won else 'lost'}",
                    kill_count=len(clutch_kills),
                    time_in_match_ms=start,
                    end_time_in_match_ms=end,
                    round_won=round_won,
                    distinct_victims=len({k.victim.puuid for k in clutch_kills}),
                    ceremony=ceremony,
                )
            )

    highlights.sort(key=lambda h: (h.time_in_match_ms, h.kind))
    return highlights


def _detect_clutch(
    round_kills: list[Kill],
    mine: list[Kill],
    my_puuid: str,
    my_name: str,
    my_tag: str,
    teammates: set[str],
    opponents: set[str],
) -> tuple[int, list[Kill], int] | None:
    """A clutch situation is simply: last alive on your team, with opponents left.

    No minimum kill count - tracker.gg's "Won a 1v1 clutch" fires on winning the
    round, however it was won. Returns (enemies alive when you were left alone, your
    kills after that moment, the timestamp you were left alone).
    """
    if not teammates or not opponents:
        return None

    deaths, kills = _timeline(round_kills)

    # The moment you were left alone: the earliest point at which every teammate is
    # dead. Candidate times are teammate death events; a resurrected teammate can
    # push that moment later, or remove it entirely.
    candidates = sorted({t for mate in teammates for t in deaths.get(mate, [])})
    t_alone: int | None = None
    for when in candidates:
        if _dead_at(teammates, when, deaths, kills) == teammates:
            t_alone = when
            break
    if t_alone is None:
        return None

    # You must be alive at that moment - having died earlier is fine if you were
    # revived before it.
    if my_puuid and my_puuid in _dead_at({my_puuid}, t_alone, deaths, kills):
        return None
    if not my_puuid and any(
        is_player(k.victim, my_puuid, my_name, my_tag)
        and k.time_in_match_in_ms <= t_alone
        for k in round_kills
    ):
        return None

    enemies_alive = len(opponents) - len(_dead_at(opponents, t_alone, deaths, kills))
    if enemies_alive < 1:
        return None

    after = [k for k in mine if k.time_in_match_in_ms > t_alone]
    return enemies_alive, after, t_alone
