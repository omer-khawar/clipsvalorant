"""Models for the HenrikDev v4 match response.

Deliberately permissive. The v4 schema is only partly documented - their docs site
does not publish the response shape, and the live OpenAPI spec confirms the fields
we depend on but not every nested object. So:

  * unknown fields are ignored rather than fatal
  * fields that might be a bare string or a {id, name} object are coerced by _name_of
  * only the fields Phase 1 actually uses are modelled

The fields below ARE confirmed against https://api.henrikdev.xyz/openapi.json:
metadata.{match_id, started_at, game_length_in_ms}, players[].{puuid, name, tag},
and the top-level kills[] with {time_in_match_in_ms, time_in_round_in_ms, round,
killer, victim, assistants} where killer/victim are {puuid, name, tag, team}.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


def _name_of(value: Any) -> str | None:
    """Accept 'Ascent', {'name': 'Ascent'}, {'id': ..., 'name': ...} or None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("name", "short", "mode", "id"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
    return None


# Queue names where players respawn, so "kills in a round" means nothing.
RESPAWN_QUEUES = (
    "deathmatch",
    "escalation",
    "snowball",
)


def _id_of(value: Any) -> str | None:
    """The uuid out of {'id': ..., 'name': ...}, or a bare id, or None.

    The mirror of `_name_of`, and needed for the same reason: this API
    returns maps, agents and queues sometimes as a string and sometimes as
    a pair, and which one it is has changed between versions.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        got = value.get("id")
        return str(got) if got not in (None, "") else None
    if isinstance(value, (str, int)):
        text = str(value)
        return text or None
    return None


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Metadata(Lenient):
    match_id: str
    started_at: datetime
    game_length_in_ms: int | None = None
    map_name: str | None = Field(default=None, validation_alias="map")
    queue: str | None = None
    mode: str | None = None
    region: str | None = None
    cluster: str | None = None

    # The map's own UUID, which is what the asset service keys its minimap
    # and its coordinate multipliers on. The name cannot stand in for it:
    # "Abyss" is a label, and labels get localised.
    map_id: str | None = Field(default=None, validation_alias="map")

    _coerce = field_validator("map_name", "queue", "mode", mode="before")(
        lambda v: _name_of(v)
    )
    _coerce_map_id = field_validator("map_id", mode="before")(lambda v: _id_of(v))


class Damage(Lenient):
    dealt: int | None = None
    received: int | None = None


class PlayerStats(Lenient):
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    score: int | None = None
    headshots: int | None = None
    bodyshots: int | None = None
    legshots: int | None = None
    damage: Damage = Field(default_factory=Damage)

    @property
    def shots(self) -> int:
        return (self.headshots or 0) + (self.bodyshots or 0) + (self.legshots or 0)

    @property
    def headshot_pct(self) -> float | None:
        """Share of landed shots that hit the head, or None when nobody shot.

        Deliberately over shots landed rather than shots fired, because that
        is the number every Valorant site shows and the only one the match
        history can support - it never says how many bullets missed.
        """
        total = self.shots
        return (self.headshots or 0) / total * 100 if total else None


class Player(Lenient):
    puuid: str
    name: str
    tag: str
    team: str | None = Field(default=None, validation_alias="team_id")
    agent: str | None = None
    agent_id: str | None = Field(default=None, validation_alias="agent")
    tier: str | None = None
    stats: PlayerStats = Field(default_factory=PlayerStats)

    _coerce_agent = field_validator("agent", mode="before")(lambda v: _name_of(v))
    _coerce_agent_id = field_validator("agent_id", mode="before")(lambda v: _id_of(v))
    _coerce_tier = field_validator("tier", mode="before")(lambda v: _name_of(v))

    @field_validator("team", mode="before")
    @classmethod
    def _coerce_team(cls, v: Any) -> str | None:
        name = _name_of(v)
        return name.capitalize() if name else None


class RoundPlayer(Lenient):
    puuid: str | None = None
    name: str | None = None
    tag: str | None = None
    team: str | None = None

    @property
    def riot_id(self) -> str:
        if self.name and self.tag:
            return f"{self.name}#{self.tag}"
        return self.name or self.puuid or "unknown"


class Location(Lenient):
    """A point in the game's own world coordinates.

    Roughly -6000..6000 on both axes, centred on the map. Turning it into a
    point on a minimap image needs that map's multipliers, which live in the
    asset service and not here - see `assets.py`.
    """

    x: int | None = None
    y: int | None = None

    @property
    def known(self) -> bool:
        return self.x is not None and self.y is not None


class Kill(Lenient):
    round: int
    time_in_match_in_ms: int
    time_in_round_in_ms: int | None = None
    killer: RoundPlayer = Field(default_factory=RoundPlayer)
    victim: RoundPlayer = Field(default_factory=RoundPlayer)
    assistants: list[RoundPlayer] = Field(default_factory=list)
    weapon: str | None = None
    # Where the victim fell. Present on every kill the v4 endpoint returns.
    location: Location = Field(default_factory=Location)

    _coerce_weapon = field_validator("weapon", mode="before")(lambda v: _name_of(v))


class TeamRounds(Lenient):
    won: int | None = None
    lost: int | None = None


class Team(Lenient):
    team_id: str | None = Field(default=None, validation_alias="team_id")
    won: bool | None = None
    rounds: TeamRounds = Field(default_factory=TeamRounds)

    @field_validator("team_id", mode="before")
    @classmethod
    def _coerce_team_id(cls, v: Any) -> str | None:
        name = _name_of(v)
        return name.capitalize() if name else None


class Plant(Lenient):
    round_time_in_ms: int | None = None
    site: str | None = None
    location: Location = Field(default_factory=Location)
    player: RoundPlayer = Field(default_factory=RoundPlayer)


class Defuse(Lenient):
    round_time_in_ms: int | None = None
    location: Location = Field(default_factory=Location)
    player: RoundPlayer = Field(default_factory=RoundPlayer)


class Round(Lenient):
    id: int | None = None
    result: str | None = None
    winning_team: str | None = None
    # Attacker or Defender. Sides swap at half time, so "Blue won" says
    # nothing about how the round was played without this.
    winning_team_role: str | None = None
    plant: Plant | None = None
    defuse: Defuse | None = None
    first_blood: RoundPlayer | None = None
    # Riot's own label for the round: CeremonyAce, CeremonyClutch, CeremonyFlawless,
    # CeremonyCloser, CeremonyThrifty, CeremonyDefault. Only one per round, and only
    # for the winning team, so it is a corroborating signal rather than a source.
    ceremony: str | None = None

    @field_validator("winning_team", mode="before")
    @classmethod
    def _coerce_winner(cls, v: Any) -> str | None:
        name = _name_of(v)
        return name.capitalize() if name else None

    @field_validator("winning_team_role", mode="before")
    @classmethod
    def _coerce_role(cls, v: Any) -> str | None:
        name = _name_of(v)
        return name.capitalize() if name else None


class Match(Lenient):
    metadata: Metadata
    players: list[Player] = Field(default_factory=list)
    teams: list[Team] = Field(default_factory=list)
    rounds: list[Round] = Field(default_factory=list)
    kills: list[Kill] = Field(default_factory=list)

    # The payload this was parsed from, when it came from the API.
    #
    # Worth the awkwardness. `raw_json` is stored as the source of truth so
    # that detection logic can change without re-fetching, but for a long
    # while what actually got stored was this model's own dump - and the
    # model is declared `extra="ignore"`, so every field it had not yet
    # learned about was dropped on the way to disk. Kill coordinates went
    # that way for sixty-seven matches. Carrying the payload alongside the
    # parse means "source of truth" is true.
    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    def with_raw(self, payload: dict[str, Any]) -> "Match":
        self._raw = payload
        return self

    # ---- helpers -------------------------------------------------------

    @property
    def id(self) -> str:
        return self.metadata.match_id

    def player_by_puuid(self, puuid: str) -> Player | None:
        return next((p for p in self.players if p.puuid == puuid), None)

    def find_me(self, name: str, tag: str) -> Player | None:
        """Locate a player by Riot ID, case-insensitively."""
        want = (name.casefold(), tag.casefold().lstrip("#"))
        return next(
            (
                p
                for p in self.players
                if (p.name.casefold(), p.tag.casefold().lstrip("#")) == want
            ),
            None,
        )

    def teammates_of(self, puuid: str) -> list[Player]:
        me = self.player_by_puuid(puuid)
        if me is None or me.team is None:
            return []
        return [p for p in self.players if p.team == me.team and p.puuid != puuid]

    def opponents_of(self, puuid: str) -> list[Player]:
        me = self.player_by_puuid(puuid)
        if me is None or me.team is None:
            return []
        return [p for p in self.players if p.team != me.team]

    def round_winner(self, round_no: int) -> str | None:
        for r in self.rounds:
            if r.id == round_no:
                return r.winning_team
        return None

    def round_ceremony(self, round_no: int) -> str | None:
        for r in self.rounds:
            if r.id == round_no:
                return r.ceremony
        return None

    @property
    def has_rounds(self) -> bool:
        """False for respawn modes, where per-round kill counts are meaningless.

        Deathmatch and friends report every kill under round 0 with players
        respawning, so a naive 'five kills in a round' test fires constantly.
        Checked by queue name, with a structural fallback for unknown queues.
        """
        queue = (self.metadata.queue or "").casefold().replace("_", " ")
        normalised = " ".join(queue.split())
        if any(bad in normalised for bad in RESPAWN_QUEUES):
            return False
        # Unknown queue: a real round-based match has more than one round.
        return len(self.rounds) > 1


class StoredMatchMeta(Lenient):
    id: str
    started_at: datetime | None = None
    mode: str | None = None
    map_name: str | None = Field(default=None, validation_alias="map")

    _coerce = field_validator("map_name", "mode", mode="before")(lambda v: _name_of(v))


class StoredMatch(Lenient):
    meta: StoredMatchMeta
