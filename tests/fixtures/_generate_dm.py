"""Builds match_v4_deathmatch.json - a respawn mode, where per-round counts lie.

Deathmatch reports every kill under round 0 and players respawn, so a naive
"five kills in a round" test fires constantly. This fixture has the same victim
dying repeatedly, which must produce no highlights at all.
"""

import json
from pathlib import Path

PLAYERS = [("TestMe", "0001", "p-me")] + [
    (f"Rando{i}", f"{i}{i}{i}{i}", f"p-r{i}") for i in range(1, 10)
]

kills = []
# I kill the same three people over and over, 12 kills in "round 0".
for n in range(12):
    victim = PLAYERS[1 + (n % 3)]
    kills.append(
        {
            "round": 0,
            "time_in_round_in_ms": n * 9000,
            "time_in_match_in_ms": n * 9000,
            "killer": {"puuid": "p-me", "name": "TestMe", "tag": "0001", "team": "Blue"},
            "victim": {"puuid": victim[2], "name": victim[0], "tag": victim[1], "team": "Red"},
            "assistants": [],
            "weapon": {"id": "w-classic", "name": "Classic"},
            "location": {"x": 1, "y": 2},
        }
    )

players = [
    {
        "puuid": p[2],
        "name": p[0],
        "tag": p[1],
        "team_id": "Blue" if i == 0 else "Red",
        "agent": {"id": "a-jett", "name": "Jett"},
        "stats": {"kills": 12 if i == 0 else 3, "deaths": 4, "assists": 0, "score": 1000},
    }
    for i, p in enumerate(PLAYERS)
]

match = {
    "metadata": {
        "match_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "map": {"id": "m-haven", "name": "Haven"},
        "game_length_in_ms": 480_000,
        "started_at": "2026-11-16T18:00:00Z",
        "queue": {"id": "deathmatch", "name": "Deathmatch", "mode_type": ""},
        "region": "eu",
        "cluster": "Frankfurt",
    },
    "players": players,
    "teams": [],
    "rounds": [{"id": 0, "result": "", "winning_team": "", "ceremony": ""}],
    "kills": kills,
}

out = Path(__file__).parent / "match_v4_deathmatch.json"
out.write_text(json.dumps({"status": 200, "data": match}, indent=2), encoding="utf-8")
print(f"wrote {out} ({len(kills)} kills)")
