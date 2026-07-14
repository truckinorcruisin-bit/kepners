"""
espn_weekly_stats.py
Converts season totals into week-by-week scoring for every player in
espn_season_stats_<year>.json -- the granularity needed for volatility/variance
analysis (e.g. "how consistent was this RB week to week" feeds directly into
2026 projection confidence intervals, especially for non-rookies with a 2025
track record).

WHY A SEPARATE SCRIPT FROM espn_season_stats.py:
That script's bulk pool query requests scoringPeriodId=0 (season total only) --
that's the only way to cheaply get thousands of players' season totals in one
call. Getting weekly detail requires a different ESPN endpoint (kona_playercard,
via league.player_info()) that returns full scoring-period history but only
for player IDs you explicitly request -- it batches IDs, but you can't ask it
for "every player in the league" the way the pool query can. So the flow is:
  1. espn_season_stats.py -> full player universe + season totals (cheap, one call)
  2. espn_weekly_stats.py (this script) -> reads that file's player IDs, then
     batches them through player_info() for weekly detail

REQUIRED environment variables (same as the other ESPN scripts):
    ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID

ZERO-POINT WEEKS (byes, injuries, or genuine zero-point games):
By request, all three are treated the same way: a week where a player scored
exactly 0 points -- whether they were on bye, inactive/injured, or simply
played and scored nothing -- is excluded from games_played and from the
avg/stdev calculation. The raw weekly_points value is still recorded (0.0 or
null) for reference, but volatility math only reflects weeks they put points
on the board.

Output: espn_weekly_stats_<year>.json
{
  "year": 2025, "weeks": [1..N],
  "players": [
    { "player_id":, "name":, "position":, "pro_team":,
      "weekly_points": {"1": 24.3, "2": 0.0, ...},   // 0.0/null both excluded from stats below
      "games_played": 15, "total_points": 316.3,
      "avg_points_per_game": 21.1, "stdev_points_per_game": 8.4 },
    ...
  ]
}
"""
import os
import sys
import json
import time
from statistics import mean, pstdev
from espn_api.football import League

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
SEASON_STATS_FILE = f"espn_season_stats_{YEAR}.json"
OUT_FILE = f"espn_weekly_stats_{YEAR}.json"
BATCH_SIZE = 150  # conservative; ESPN's practical limit for filterIds isn't documented


def get_credentials():
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")
    if not league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID (env var or GitHub Secret) first.")
    return int(league_id), espn_s2, swid


def load_player_ids():
    if not os.path.exists(SEASON_STATS_FILE):
        raise SystemExit(
            f"{SEASON_STATS_FILE} not found -- run espn_season_stats.py for {YEAR} first."
        )
    with open(SEASON_STATS_FILE) as f:
        data = json.load(f)
    ids = []
    for pos, plist in data["players_by_position"].items():
        for p in plist:
            ids.append(p["player_id"])
    return ids


def extract_weekly(player, final_week):
    """Pull real (non-projected) weekly points from a Player object's .stats
    dict. Per-request simplification: a 0-point week -- whether it's a bye,
    an injury/inactive week, or a genuine zero-point outing -- is excluded
    from games_played and the avg/stdev calc. Only weeks with a nonzero
    point total count as a "game played" for volatility purposes."""
    weekly = {}
    games_played = 0
    scored = []
    for week in range(1, final_week + 1):
        wk_stats = player.stats.get(week)
        pts = wk_stats.get("points") if wk_stats else None
        weekly[str(week)] = pts  # raw value kept for reference: None (no data) or 0.0 (scored zero) or actual points
        if pts is not None and pts != 0:
            games_played += 1
            scored.append(pts)
    return weekly, games_played, scored


def main():
    league_id, espn_s2, swid = get_credentials()
    player_ids = load_player_ids()
    print(f"Loading league {league_id}, {YEAR} season...")
    league = League(league_id=league_id, year=YEAR, espn_s2=espn_s2, swid=swid)
    final_week = league.finalScoringPeriod
    print(f"Final scoring period: {final_week}. Pulling weekly detail for "
          f"{len(player_ids)} players in batches of {BATCH_SIZE}...")

    out_players = []
    for i in range(0, len(player_ids), BATCH_SIZE):
        batch = player_ids[i:i + BATCH_SIZE]
        try:
            result = league.player_info(playerId=batch)
        except Exception as e:
            print(f"  batch {i}-{i+len(batch)} failed ({e}); skipping this batch")
            continue
        if result is None:
            continue
        players = result if isinstance(result, list) else [result]
        for p in players:
            weekly, games_played, scored = extract_weekly(p, final_week)
            out_players.append({
                "player_id": p.playerId,
                "name": p.name,
                "position": p.position,
                "pro_team": p.proTeam,
                "weekly_points": weekly,
                "games_played": games_played,
                "total_points": p.total_points,
                "avg_points_per_game": round(mean(scored), 2) if scored else None,
                "stdev_points_per_game": round(pstdev(scored), 2) if len(scored) > 1 else None,
            })
        print(f"  processed {min(i+BATCH_SIZE, len(player_ids))}/{len(player_ids)}")
        time.sleep(0.3)  # be polite to ESPN's API between batches

    out = {"year": YEAR, "weeks": list(range(1, final_week + 1)), "players": out_players}
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {OUT_FILE}: {len(out_players)} players with weekly detail.")


if __name__ == "__main__":
    main()
