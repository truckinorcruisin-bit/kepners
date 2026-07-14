"""
espn_season_stats.py
Pulls full-season fantasy scoring for EVERY fantasy-relevant player (rostered
teams AND free agents/waivers -- not just your league's 12x16 drafted pool)
from ESPN. This is the raw dataset a VOR/WAR-style replacement-level baseline
needs: to know what "replacement level RB" scored, you need every RB's season
total, not just the ones someone drafted.

WHY THIS USES THE SAME ESPN CREDENTIALS AS ZIMMER:
Player season scoring is a league-wide ESPN concept, not specific to one
league's rosters -- but it IS specific to that league's SCORING SETTINGS
(standard vs PPR, TD value, etc). Since Kepners, Miami, and Zimmer are all
"standard scoring" per league_rules.json, one ESPN league's player pool
(Zimmer's) gives a scoring-accurate dataset usable as the replacement-level
reference for all three leagues. If that ever changes (one league goes PPR),
this would need to run against a league with matching settings instead.

REQUIRED environment variables (same secrets as espn_zimmer_history.py):
    ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID

HOW THE FULL POOL QUERY WORKS:
league.free_agents() in the espn_api package only returns FREEAGENT/WAIVERS
status players -- it deliberately excludes anyone already rostered, which
would silently drop every startable player from the dataset. This script
instead calls ESPN's raw kona_player_info endpoint directly (bypassing that
filter) with no status restriction and a high result limit, so rostered and
unrostered players both come back. Results are parsed into the same Player
objects the library uses elsewhere, then sorted ourselves in Python by season
total points -- no reliance on guessing ESPN's internal sort-field syntax.

CAVEAT: ESPN's API may cap the result size below what we request (seen
anywhere from ~1500-3000 in practice). If PLAYERS_FETCHED in the printed
summary looks suspiously low relative to a ~2200-ish real fantasy-relevant
NFL player universe, that's ESPN capping the response, not a bug here --
let me know and we can add pagination (offset-based paging) if needed.

Output: espn_season_stats_<year>.json
{
  "year": 2025, "scoring_note": "pulled from Zimmer league scoring settings",
  "players_by_position": {
    "RB": [ {"player_id":, "name":, "pro_team":, "total_points": }, ... ],  // sorted desc
    "WR": [...], "QB": [...], "TE": [...], "K": [...], "D/ST": [...]
  }
}
"""
import os
import sys
import json
from espn_api.football import League
from espn_api.football.player import Player

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
OUT_FILE = f"espn_season_stats_{YEAR}.json"
POOL_SIZE = 3000  # requested limit; ESPN may cap lower, see CAVEAT above


def get_credentials():
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")
    if not league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID (env var or GitHub Secret) first.")
    return int(league_id), espn_s2, swid


def fetch_full_player_pool(league, year, size=POOL_SIZE):
    """Raw kona_player_info query with NO filterStatus (so rostered players
    are included, unlike league.free_agents()), season totals (scoringPeriodId=0)."""
    params = {"view": "kona_player_info", "scoringPeriodId": 0}
    filters = {
        "players": {
            "limit": size,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    headers = {"x-fantasy-filter": json.dumps(filters)}
    data = league.espn_request.league_get(params=params, headers=headers)
    raw_players = data.get("players", [])

    players = []
    for entry in raw_players:
        try:
            p = Player(entry, year)
            players.append(p)
        except Exception:
            continue  # skip malformed entries rather than failing the whole pull
    return players


def main():
    league_id, espn_s2, swid = get_credentials()
    print(f"Loading league {league_id}, {YEAR} season...")
    league = League(league_id=league_id, year=YEAR, espn_s2=espn_s2, swid=swid)

    print("Fetching full player pool (rostered + free agent)...")
    players = fetch_full_player_pool(league, YEAR)
    print(f"Fetched {len(players)} players.")

    by_pos = {}
    for p in players:
        pos = p.position or "UNKNOWN"
        by_pos.setdefault(pos, []).append({
            "player_id": p.playerId,
            "name": p.name,
            "pro_team": p.proTeam,
            "total_points": p.total_points,
        })

    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x["total_points"] or 0, reverse=True)

    out = {
        "year": YEAR,
        "scoring_note": f"pulled from ESPN league {league_id} scoring settings (standard)",
        "players_by_position": by_pos,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\nWrote {OUT_FILE}:")
    for pos, plist in sorted(by_pos.items(), key=lambda kv: -len(kv[1])):
        top = plist[0]["total_points"] if plist else 0
        print(f"  {pos:8} {len(plist):4} players, top score {top}")


if __name__ == "__main__":
    main()
