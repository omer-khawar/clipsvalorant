"""Builds match_v4_sample.json - a synthetic v4 match with deliberately known answers.

Round layout (0-indexed, matching the API):
   0  one trade. Me: 1 kill             -> no highlight
   2  I kill all five. Teammates alive  -> Ace, no clutch
   4  I kill three. Teammates alive     -> 3K
   6  teammates all die, then I get 2   -> 1v5 attempt (lost), no multi-kill
   8  I get 2, then I die, then my team dies -> nothing (died before last alive)
  10  6 kills but only 5 distinct (Sage rez), round LOST -> Ace, kill_count 5
  12  4 kills but only 3 distinct (Sage rez)  -> 3K, not 4K
  14  3 opponents + a teamkill + a self-kill  -> 3K (neither counts)
  16  an enemy is revived and is still up        -> 1v2, not 1v1
  18  an enemy is revived then dies again        -> clean 1v1 clutch
  20  *I* die, am revived, and win it alone      -> 1v1 clutch

Rounds 10, 12 and 14 are regressions for real cases found on live matches: a
resurrected victim dying twice in one round, an ace in a round you lost, and
kills that must not count toward a multi-kill.
"""

import json
from pathlib import Path

ME = ("TestMe", "0001", "p-me")
ALLIES = [("Ally1", "1111", "p-a1"), ("Ally2", "2222", "p-a2"),
          ("Ally3", "3333", "p-a3"), ("Ally4", "4444", "p-a4")]
FOES = [("Foe1", "5555", "p-f1"), ("Foe2", "6666", "p-f2"), ("Foe3", "7777", "p-f3"),
        ("Foe4", "8888", "p-f4"), ("Foe5", "9999", "p-f5")]

ROUND_MS = 100_000


def rp(who, team):
    name, tag, puuid = who
    return {"puuid": puuid, "name": name, "tag": tag, "team": team}


def kill(rnd, offset_s, killer, killer_team, victim, victim_team, weapon="Vandal"):
    return {
        "round": rnd,
        "time_in_round_in_ms": offset_s * 1000,
        "time_in_match_in_ms": rnd * ROUND_MS + offset_s * 1000,
        "killer": rp(killer, killer_team),
        "victim": rp(victim, victim_team),
        "assistants": [],
        "weapon": {"id": "w-vandal", "name": weapon},
        "location": {"x": 100, "y": 200},
    }


kills = []

# Round 0 - a trade. One kill for me.
kills.append(kill(0, 12, FOES[0], "Red", ALLIES[0], "Blue"))
kills.append(kill(0, 15, ME, "Blue", FOES[0], "Red"))

# Round 2 - ace. Teammates all survive, so this must NOT register as a clutch.
for i, foe in enumerate(FOES):
    kills.append(kill(2, 10 + i * 5, ME, "Blue", foe, "Red"))

# Round 4 - 3K.
for i, foe in enumerate(FOES[:3]):
    kills.append(kill(4, 20 + i * 4, ME, "Blue", foe, "Red"))

# Round 6 - all four teammates die first, then I take two before losing the round.
for i, ally in enumerate(ALLIES):
    kills.append(kill(6, 8 + i * 3, FOES[i], "Red", ally, "Blue"))
kills.append(kill(6, 30, ME, "Blue", FOES[0], "Red"))
kills.append(kill(6, 36, ME, "Blue", FOES[1], "Red"))
kills.append(kill(6, 44, FOES[2], "Red", ME, "Blue"))

# Round 8 - I die before becoming last alive, so no clutch however many kills I had.
kills.append(kill(8, 10, ME, "Blue", FOES[0], "Red"))
kills.append(kill(8, 14, ME, "Blue", FOES[1], "Red"))
kills.append(kill(8, 20, FOES[2], "Red", ME, "Blue"))
for i, ally in enumerate(ALLIES):
    kills.append(kill(8, 25 + i * 3, FOES[2], "Red", ally, "Blue"))

# Round 10 - Sage resurrects FOES[2], so it dies twice. Six kills, five bodies.
# The round is still lost (spike detonated), which must not suppress the ace.
for i, foe in enumerate(FOES):
    kills.append(kill(10, 10 + i * 4, ME, "Blue", foe, "Red"))
kills.append(kill(10, 46, ME, "Blue", FOES[2], "Red", weapon="Judge"))

# Round 12 - FOES[0] rezzed and killed again. Four kills, three bodies -> 3K.
kills.append(kill(12, 10, ME, "Blue", FOES[0], "Red"))
kills.append(kill(12, 16, ME, "Blue", FOES[1], "Red"))
kills.append(kill(12, 28, ME, "Blue", FOES[0], "Red", weapon="Judge"))
kills.append(kill(12, 34, ME, "Blue", FOES[2], "Red"))

# Round 14 - a teamkill and a self-kill must not inflate the multi-kill.
for i, foe in enumerate(FOES[:3]):
    kills.append(kill(14, 10 + i * 5, ME, "Blue", foe, "Red"))
kills.append(kill(14, 30, ME, "Blue", ALLIES[0], "Blue", weapon="Odin"))
kills.append(kill(14, 40, ME, "Blue", ME, "Blue", weapon="Spike"))

# Round 16 - FOES[3] dies, is revived, and is still up when I am left alone, so
# two enemies are alive: a 1v2, not a 1v1. (Lotus r5 on 2026-11-16.)
kills.append(kill(16, 6, ALLIES[0], "Blue", FOES[0], "Red"))
kills.append(kill(16, 10, ALLIES[1], "Blue", FOES[1], "Red"))
kills.append(kill(16, 14, ALLIES[2], "Blue", FOES[2], "Red"))
kills.append(kill(16, 18, ALLIES[3], "Blue", FOES[3], "Red"))   # FOES[3] dies...
kills.append(kill(16, 24, FOES[3], "Red", ALLIES[0], "Blue"))   # ...and kills: revived
kills.append(kill(16, 28, FOES[4], "Red", ALLIES[1], "Blue"))
kills.append(kill(16, 32, FOES[4], "Red", ALLIES[2], "Blue"))
kills.append(kill(16, 36, FOES[4], "Red", ALLIES[3], "Blue"))   # alone at 36s, 1v2
kills.append(kill(16, 44, ME, "Blue", FOES[3], "Red"))

# Round 18 - FOES[0] is revived and then dies AGAIN before I am left alone, so by
# then they really are dead: a clean 1v1. (Haven r7 on 2026-10-30.)
kills.append(kill(18, 6, ALLIES[0], "Blue", FOES[0], "Red"))
kills.append(kill(18, 12, FOES[0], "Red", ALLIES[0], "Blue"))   # revived
kills.append(kill(18, 18, ALLIES[1], "Blue", FOES[0], "Red"))   # and dead again
kills.append(kill(18, 22, ALLIES[2], "Blue", FOES[1], "Red"))
kills.append(kill(18, 26, ALLIES[3], "Blue", FOES[2], "Red"))
kills.append(kill(18, 30, ME, "Blue", FOES[3], "Red"))
kills.append(kill(18, 34, FOES[4], "Red", ALLIES[1], "Blue"))
kills.append(kill(18, 38, FOES[4], "Red", ALLIES[2], "Blue"))
kills.append(kill(18, 42, FOES[4], "Red", ALLIES[3], "Blue"))   # alone at 42s, 1v1
kills.append(kill(18, 50, ME, "Blue", FOES[4], "Red"))

# Round 20 - *I* die early, am revived, and win it alone. Treating my first death
# as final hides this clutch completely. (Summit r11 on 2026-11-03.)
kills.append(kill(20, 8, FOES[0], "Red", ME, "Blue"))           # I die...
kills.append(kill(20, 14, ALLIES[0], "Blue", FOES[0], "Red"))
kills.append(kill(20, 20, ME, "Blue", FOES[1], "Red"))          # ...and kill: revived
kills.append(kill(20, 26, ALLIES[1], "Blue", FOES[2], "Red"))
kills.append(kill(20, 30, ALLIES[2], "Blue", FOES[3], "Red"))
kills.append(kill(20, 34, FOES[4], "Red", ALLIES[0], "Blue"))
kills.append(kill(20, 38, FOES[4], "Red", ALLIES[1], "Blue"))
kills.append(kill(20, 42, FOES[4], "Red", ALLIES[2], "Blue"))
kills.append(kill(20, 46, FOES[4], "Red", ALLIES[3], "Blue"))   # alone at 46s, 1v1
kills.append(kill(20, 54, ME, "Blue", FOES[4], "Red"))

kills.sort(key=lambda k: k["time_in_match_in_ms"])


def player(who, team, agent, k, d, a, score):
    name, tag, puuid = who
    return {
        "puuid": puuid, "name": name, "tag": tag, "team_id": team,
        "agent": {"id": f"a-{agent.lower()}", "name": agent},
        "stats": {"kills": k, "deaths": d, "assists": a, "score": score,
                  "headshots": 4, "bodyshots": 20, "legshots": 1},
        "account_level": 120,
    }


players = [
    player(ME, "Blue", "Jett", 13, 2, 3, 4200),  # stats block is cosmetic; kills[] is truth
    player(ALLIES[0], "Blue", "Omen", 8, 9, 5, 3100),
    player(ALLIES[1], "Blue", "Sova", 6, 10, 7, 2800),
    player(ALLIES[2], "Blue", "Killjoy", 5, 11, 4, 2500),
    player(ALLIES[3], "Blue", "Sage", 4, 12, 9, 2400),
    player(FOES[0], "Red", "Reyna", 9, 10, 2, 3300),
    player(FOES[1], "Red", "Raze", 7, 11, 3, 2900),
    player(FOES[2], "Red", "Brimstone", 10, 8, 4, 3400),
    player(FOES[3], "Red", "Cypher", 3, 9, 6, 2100),
    player(FOES[4], "Red", "Skye", 5, 10, 8, 2600),
]

rounds = []
BLUE_WINS = {0, 2, 4, 12, 14, 16, 18, 20}
for i in range(21):
    winner = "Blue" if i in BLUE_WINS else "Red"
    # Riot awards one ceremony per round, to the WINNING team, so an ace in a
    # lost round carries no CeremonyAce. Round 10 encodes exactly that.
    ceremony = "CeremonyDefault"
    if i == 2:
        ceremony = "CeremonyAce"
    elif i == 10:
        ceremony = "CeremonyCloser"
    rounds.append({"id": i, "result": "Detonate" if i == 10 else "Eliminated",
                   "winning_team": winner, "ceremony": ceremony})

match = {
    "metadata": {
        "match_id": "11111111-2222-3333-4444-555555555555",
        "map": {"id": "m-ascent", "name": "Ascent"},
        "game_version": "release-10.00",
        "game_length_in_ms": 1_920_000,
        "started_at": "2026-11-15T20:04:11Z",
        "is_completed": True,
        "queue": {"id": "competitive", "name": "Competitive", "mode_type": "Standard"},
        "season": {"id": "s-e10a5", "short": "E10A5"},
        "platform": "pc",
        "region": "eu",
        "cluster": "Frankfurt",
    },
    "players": players,
    "observers": [],
    "coaches": [],
    "teams": [
        {"team_id": "Blue", "won": True, "rounds": {"won": 13, "lost": 7}},
        {"team_id": "Red", "won": False, "rounds": {"won": 7, "lost": 13}},
    ],
    "rounds": rounds,
    "kills": kills,
}

out = Path(__file__).parent / "match_v4_sample.json"
out.write_text(json.dumps({"status": 200, "data": match}, indent=2), encoding="utf-8")
print(f"wrote {out} ({len(kills)} kills, {len(players)} players)")
