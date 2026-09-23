"""
sleeper_trending.py
Cross-league DEMAND signal for the FAAB assistant: Sleeper's public
"trending adds" list -- how many Sleeper leagues added each player over the
last 48 hours. Free, no auth, no key.

Why Sleeper when none of these leagues are on it: this is a heat signal, not
a league fact. A player being added in thousands of leagues at once means
managers everywhere -- including ours -- are reading the same news, so more
rivals bid than roster need alone would predict (stash bids). The FAAB
assistant uses it only as a bump to the expected rival count; everything
league-specific (who would actually start him, how hard each manager bids)
comes from ESPN.

Endpoints (docs.sleeper.com, #trending-players / #players):
  GET https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=48&limit=N
      -> [{"player_id": "...", "count": N}, ...]
  GET https://api.sleeper.app/v1/players/nfl
      -> {player_id: {full_name, first_name, last_name, position, team, ...}}
Sleeper asks that the full players map be fetched at most once a day; this
runs weekly.

OUTPUT: sleeper_trending.json
"""
import json
from datetime import datetime, timezone

import requests

LOOKBACK_HOURS = 48
LIMIT = 300
TREND_URL = (f"https://api.sleeper.app/v1/players/nfl/trending/add"
             f"?lookback_hours={LOOKBACK_HOURS}&limit={LIMIT}")
PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
OUT_FILE = "sleeper_trending.json"


def display_name(pid, meta):
    """Name in the same form ESPN uses, so the browser can join on it.
    Defenses are the one real mismatch: Sleeper keys them by team code
    ("MIN") with first/last "Minnesota"/"Vikings"; ESPN calls them
    "Vikings D/ST"."""
    pos = meta.get("position")
    if pos == "DEF":
        return f"{meta.get('last_name') or pid} D/ST"
    return (meta.get("full_name")
            or f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
            or pid)


def main():
    trend = requests.get(TREND_URL, timeout=30)
    trend.raise_for_status()
    rows = trend.json()
    players = requests.get(PLAYERS_URL, timeout=120)
    players.raise_for_status()
    pmap = players.json()

    out = []
    for rank, r in enumerate(rows, 1):
        pid = str(r.get("player_id"))
        meta = pmap.get(pid) or {}
        out.append({
            "rank": rank,
            "adds": r.get("count"),
            "name": display_name(pid, meta),
            "position": meta.get("position"),
            "team": meta.get("team"),
        })
    unnamed = sum(1 for p in out if not p["position"])
    with open(OUT_FILE, "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                   "lookbackHours": LOOKBACK_HOURS, "players": out}, f, indent=1)
    print(f"{len(out)} trending players ({unnamed} unresolved) -> {OUT_FILE}")
    if out:
        print("Top 5:", ", ".join(f"{p['name']} ({p['adds']})" for p in out[:5]))


if __name__ == "__main__":
    main()
