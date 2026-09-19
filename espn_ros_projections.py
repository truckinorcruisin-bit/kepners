"""
espn_ros_projections.py
Rest-of-season projections for the ENTIRE NFL player pool -- not league-specific.

WHY THIS IS SEPARATE FROM THE ROSTER PULLERS
The in-season engines (waiver targets, trade targets, positional strength) all
need one thing above all else: a forward-looking points estimate for every
player, on a single consistent scale, so that a Kepners player and a Miami
player can be compared with the same yardstick. ESPN publishes exactly that as
weekly projections. Yahoo does not expose anything equivalent through its API.

So ESPN is the PROJECTION ENGINE for all leagues, and each platform supplies
only its own roster/ownership state. This is the same call already made for
Kepners keeper grading, which uses ESPN season stats as a Yahoo scoring proxy
on the grounds that the scoring formats are materially equivalent (only a minor
D/ST difference). The same reasoning holds here, and the same caveat applies:
these are ESPN's opinions rendered in ESPN's scoring, so treat cross-platform
comparisons as directional, not exact.

REST OF SEASON = sum of weekly projected points for weeks >= the current
scoring period. Deliberately NOT (season projection - points scored so far):
that subtraction silently bakes in a preseason projection that may be months
stale, whereas ESPN re-forecasts the weekly numbers as the season goes.

Also captured, because the waiver engine needs them and they only exist here:
  - percent_owned        -> is this actually a free agent anywhere?
  - injury_status        -> an OUT player's ROS is a lie without this
  - recent form          -> last 3 weeks actual vs projected, to catch a
                            usage change ESPN's projection hasn't absorbed yet

REQUIRED environment variables (same as every other ESPN script):
    ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID
Any league works -- the player pool is global. ESPN_LEAGUE_ID is only the
doorway the API requires.

OUTPUT: player_ros_<year>.json
{
  "year": 2026, "generated": "...", "current_week": 8, "weeks_remaining": 11,
  "players": [
    { "player_id":, "name":, "position":, "pro_team":,
      "percent_owned":, "injury_status":, "injured":,
      "ros_points":, "ros_per_week":, "weeks_projected":,
      "season_points_actual":, "last3_actual":, "last3_projected":,
      "trend":  # actual-minus-projected over last 3, + = outperforming
    }, ...
  ]
}
"""
import os
import sys
import json
from datetime import datetime, timezone

from espn_api.football import League
from espn_api.football.player import Player

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"player_ros_{YEAR}.json"
POOL_SIZE = 1000
# Last week of the NFL fantasy regular season we bother projecting. Weeks 18+
# are meaningless for every league here (all end by 17), and ESPN's projections
# that far out are noise anyway.
MAX_WEEK = 17
# Kickers never come back from the ownership-sorted pool query -- a real,
# verified ESPN quirk, documented at length in espn_player_values.py. Same
# workaround: ask for the K slot explicitly.
K_SLOT_ID = 17


def get_credentials():
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    if not league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID (env var or GitHub Secret) first.")
    return int(league_id), os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")


def fetch_pool(league, year, size=POOL_SIZE, slot_id=None):
    """Raw player entries, either the whole ownership-sorted pool or one slot."""
    params = {"view": "kona_player_info", "scoringPeriodId": 0}
    pf = {"limit": size, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}
    if slot_id is not None:
        pf["filterSlotIds"] = {"value": [slot_id]}
    headers = {"x-fantasy-filter": json.dumps({"players": pf})}
    data = league.espn_request.league_get(params=params, headers=headers)
    return data.get("players", [])


def summarize(p, current_week):
    """Turn a Player's per-week stats dict into forward and backward views.

    p.stats is keyed by scoring period. Each entry may carry 'points' (actual,
    statSourceId 0) and/or 'projected_points' (statSourceId 1). A week that has
    been played has both; a future week has only the projection. Week 0 is the
    full-season rollup, not a real week, so it is skipped -- including it would
    roughly double every ROS total.
    """
    ros = 0.0
    weeks_projected = 0
    season_actual = 0.0
    last3_actual = 0.0
    last3_proj = 0.0
    last3_from = max(1, current_week - 3)

    for wk, s in (p.stats or {}).items():
        try:
            wk = int(wk)
        except (TypeError, ValueError):
            continue
        if wk < 1 or wk > MAX_WEEK:
            continue
        actual = s.get("points")
        proj = s.get("projected_points")

        if wk < current_week and actual is not None:
            season_actual += actual
            if wk >= last3_from:
                last3_actual += actual
                if proj is not None:
                    last3_proj += proj
        elif wk >= current_week and proj is not None:
            # A bye week legitimately projects 0 and still counts as a week
            # consumed -- dropping zeros would overstate ros_per_week for any
            # player whose bye is still ahead of him.
            ros += proj
            weeks_projected += 1

    return {
        "ros_points": round(ros, 2),
        "ros_per_week": round(ros / weeks_projected, 2) if weeks_projected else None,
        "weeks_projected": weeks_projected,
        "season_points_actual": round(season_actual, 2),
        "last3_actual": round(last3_actual, 2),
        "last3_projected": round(last3_proj, 2),
        "trend": round(last3_actual - last3_proj, 2) if last3_proj else None,
    }


def main():
    league_id, espn_s2, swid = get_credentials()
    print(f"Loading league {league_id} ({YEAR}) as a doorway to the global player pool...")
    league = League(league_id=league_id, year=YEAR, espn_s2=espn_s2, swid=swid)

    current_week = getattr(league, "current_week", None) or getattr(league, "nfl_week", 1)
    print(f"Current scoring period: week {current_week}")
    if current_week > MAX_WEEK:
        print(f"WARNING: week {current_week} is past week {MAX_WEEK} -- the season is over, "
              f"so every ROS projection below will be 0. This file is not useful now.")

    entries = fetch_pool(league, YEAR)
    print(f"Fetched {len(entries)} raw entries from the main pool.")

    players, dropped = [], 0
    seen = set()
    for entry in entries:
        try:
            p = Player(entry, YEAR)
        except Exception:
            dropped += 1
            continue
        if p.playerId in seen:
            continue
        seen.add(p.playerId)
        row = {
            "player_id": p.playerId,
            "name": p.name,
            "position": p.position,
            "pro_team": p.proTeam,
            "percent_owned": getattr(p, "percent_owned", None),
            "injury_status": getattr(p, "injuryStatus", None),
            "injured": bool(getattr(p, "injured", False)),
        }
        row.update(summarize(p, current_week))
        players.append(row)
    if dropped:
        print(f"  ({dropped} raw entries failed to parse and were dropped)")

    if not any(p["position"] == "K" for p in players):
        print("  0 kickers in the main pool (known ESPN quirk) -- explicit K-slot fetch...")
        for entry in fetch_pool(league, YEAR, size=100, slot_id=K_SLOT_ID):
            try:
                p = Player(entry, YEAR)
            except Exception:
                continue
            if p.playerId in seen:
                continue
            seen.add(p.playerId)
            row = {
                "player_id": p.playerId, "name": p.name, "position": p.position,
                "pro_team": p.proTeam, "percent_owned": getattr(p, "percent_owned", None),
                "injury_status": getattr(p, "injuryStatus", None),
                "injured": bool(getattr(p, "injured", False)),
            }
            row.update(summarize(p, current_week))
            players.append(row)
        print(f"  Pool now has {sum(1 for p in players if p['position']=='K')} kicker(s).")

    have_ros = sum(1 for p in players if p["ros_points"])
    print(f"{len(players)} players, {have_ros} with a nonzero ROS projection.")
    if have_ros < len(players) * 0.1:
        print("WARNING: almost nothing has a ROS projection. Either the season hasn't "
              "started, it's over, or ESPN hasn't published weekly projections yet. "
              "Do NOT trust downstream waiver/trade output from this file.")

    out = {
        "year": YEAR,
        "generated": datetime.now(timezone.utc).isoformat(),
        "current_week": current_week,
        "weeks_remaining": max(0, MAX_WEEK - current_week + 1),
        "players": players,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE} ({len(players)} players).")


if __name__ == "__main__":
    main()
