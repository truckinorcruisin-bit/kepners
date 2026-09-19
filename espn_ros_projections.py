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

REST OF SEASON -- two paths, in preference order:

  A. WEEKLY. Sum the projected points of every week >= the current scoring
     period. Most accurate, because it respects byes and ESPN's week-by-week
     re-forecast.
  B. SEASON ROLLUP (fallback). season_projected_total - points_scored_to_date.

BUG FIXED (first live run, week 3 2026): this script originally implemented
ONLY path A, on the assumption that kona_player_info returns all 17 weekly
stat entries. It does not. At scoringPeriodId=0 ESPN returns SEASON-LEVEL
rollups keyed to scoring period 0, so the week filter discarded every entry
and produced ros_points=0 for the entire pool -- which then read downstream as
"every player is worthless", not as "no data". Path B exists because that is
the shape ESPN actually returns here.

Path B is a genuinely acceptable ROS estimate, not a hack: ESPN re-forecasts
the season projection as the year goes, so subtracting points already banked
leaves the forward-looking remainder. Its real weakness is that it can't see
bye weeks, so ros_per_week is slightly optimistic for a player with a bye
still ahead. Which path was used is recorded per player in `ros_method` and
summarized in the run log, so a silent downgrade is impossible.

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
      "ros_points":, "ros_per_week":, "weeks_projected":, "ros_method":,
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


def summarize(p, current_week, weeks_remaining):
    """Turn a Player's stats dict into forward and backward views.

    p.stats is keyed by scoring period. Period 0 is the SEASON ROLLUP, periods
    1..17 are individual weeks. Which of those ESPN actually populates depends
    on the request view -- see the module docstring. Both are handled.
    """
    weekly_ros = 0.0
    weeks_projected = 0
    weekly_actual = 0.0
    last3_actual = 0.0
    last3_proj = 0.0
    last3_from = max(1, current_week - 3)
    season_proj = None
    season_actual_rollup = None

    for wk, s in (p.stats or {}).items():
        try:
            wk = int(wk)
        except (TypeError, ValueError):
            continue
        actual = s.get("points")
        proj = s.get("projected_points")

        if wk == 0:
            # Season rollup, not a real week. Captured for path B, never added
            # to the weekly sums -- doing so would roughly double every total.
            season_proj = proj
            season_actual_rollup = actual
            continue
        if wk > MAX_WEEK:
            continue

        if wk < current_week and actual is not None:
            weekly_actual += actual
            if wk >= last3_from:
                last3_actual += actual
                if proj is not None:
                    last3_proj += proj
        elif wk >= current_week and proj is not None:
            # A bye legitimately projects 0 and still consumes a week --
            # dropping zeros would overstate ros_per_week.
            weekly_ros += proj
            weeks_projected += 1

    season_actual = season_actual_rollup if season_actual_rollup is not None else weekly_actual

    if weeks_projected:
        ros = weekly_ros
        per_week = ros / weeks_projected
        method = "weekly"
    elif season_proj is not None:
        remainder = season_proj - (season_actual or 0.0)
        weeks_played = max(0, current_week - 1)
        if remainder > 0:
            ros = remainder
            method = "season_rollup"
        elif weeks_played and (season_actual or 0) > 0:
            # He has already beaten his own full-season projection. Taking the
            # remainder literally would value a league-winner at ZERO and sort
            # him below waiver fodder -- the single most damaging error this
            # file could make. ESPN's preseason number is simply stale for
            # him, so fall back to his own scoring pace instead.
            ros = (season_actual / weeks_played) * weeks_remaining
            method = "pace"
        else:
            ros = 0.0
            method = "season_rollup"
        per_week = (ros / weeks_remaining) if weeks_remaining else None
        weeks_projected = weeks_remaining
    else:
        ros, per_week, method = 0.0, None, "none"

    return {
        "ros_points": round(ros, 2),
        "ros_per_week": round(per_week, 2) if per_week is not None else None,
        "weeks_projected": weeks_projected,
        "ros_method": method,
        "season_projected_total": round(season_proj, 2) if season_proj is not None else None,
        "season_points_actual": round(season_actual, 2),
        "last3_actual": round(last3_actual, 2),
        "last3_projected": round(last3_proj, 2),
        "trend": round(last3_actual - last3_proj, 2) if last3_proj else None,
    }


def describe_stats_shape(players_raw, year, limit=3):
    """Print the raw scoring periods ESPN returned for a few players.

    Exists because the failure this script hit once -- an assumption about
    which periods come back -- is invisible from the outputs alone. One line
    of this in the run log answers it definitively next time.
    """
    from espn_api.football.player import Player
    print("  Raw stats shape sample (scoring periods present per player):")
    shown = 0
    for entry in players_raw:
        try:
            p = Player(entry, year)
        except Exception:
            continue
        if not p.stats:
            continue
        periods = sorted(int(k) for k in p.stats.keys())
        fields = sorted({f for s in p.stats.values() for f in s.keys()
                         if f in ("points", "projected_points")})
        print(f"    {p.name[:24]:26} periods={periods[:20]} fields={fields}")
        shown += 1
        if shown >= limit:
            break
    if not shown:
        print("    (no player carried any stats at all -- that's the real problem)")


def main():
    league_id, espn_s2, swid = get_credentials()
    print(f"Loading league {league_id} ({YEAR}) as a doorway to the global player pool...")
    league = League(league_id=league_id, year=YEAR, espn_s2=espn_s2, swid=swid)

    current_week = getattr(league, "current_week", None) or getattr(league, "nfl_week", 1)
    print(f"Current scoring period: week {current_week}")
    if current_week > MAX_WEEK:
        print(f"WARNING: week {current_week} is past week {MAX_WEEK} -- the season is over, "
              f"so every ROS projection below will be 0. This file is not useful now.")

    weeks_remaining = max(0, MAX_WEEK - current_week + 1)
    print(f"Weeks remaining (incl. current): {weeks_remaining}")

    entries = fetch_pool(league, YEAR)
    print(f"Fetched {len(entries)} raw entries from the main pool.")
    describe_stats_shape(entries, YEAR)

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
        row.update(summarize(p, current_week, weeks_remaining))
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
            row.update(summarize(p, current_week, weeks_remaining))
            players.append(row)
        print(f"  Pool now has {sum(1 for p in players if p['position']=='K')} kicker(s).")

    have_ros = sum(1 for p in players if p["ros_points"])
    methods = {}
    for p in players:
        methods[p.get("ros_method")] = methods.get(p.get("ros_method"), 0) + 1
    print(f"{len(players)} players, {have_ros} with a nonzero ROS projection.")
    print(f"  ROS method breakdown: {methods}")
    if methods.get("pace"):
        print(f"  NOTE: {methods['pace']} player(s) have already outscored their own "
              f"season projection; their ROS is extrapolated from actual pace instead.")
    if methods.get("season_rollup"):
        print("  NOTE: using the season-rollup fallback (ESPN returned season totals, "
              "not per-week projections). Valid, but blind to upcoming byes -- "
              "ros_per_week runs slightly high for a player whose bye is ahead.")

    top = sorted((p for p in players if p["ros_per_week"] is not None),
                 key=lambda p: p["ros_per_week"], reverse=True)[:5]
    if top:
        print("  Sanity -- top 5 by ros_per_week (these should be recognisable stars):")
        for p in top:
            print(f"    {p['name'][:24]:26} {p['position']:4} "
                  f"{p['ros_per_week']}/wk  ({p['ros_points']} ROS, {p['ros_method']})")

    if have_ros < len(players) * 0.1:
        # Hard failure, not a warning. A file of zeros is worse than no file:
        # downstream it reads as "every player is worthless" rather than "no
        # data", and the Manager Cockpit will confidently recommend nonsense.
        raise SystemExit(
            f"FATAL: only {have_ros} of {len(players)} players have any ROS projection. "
            f"Method breakdown: {methods}. Check the raw stats shape printed above -- "
            f"if no player carries 'projected_points' at all, ESPN isn't returning "
            f"projections for this request and neither path can work. Not writing "
            f"{OUT_FILE}, so the last good copy stays in place."
        )

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
