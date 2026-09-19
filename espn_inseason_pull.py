"""
espn_inseason_pull.py
Current roster / free-agent / standings state for the ESPN-hosted league (Zimmer).

This is the STATE half of the in-season data layer. The TALENT half --
rest-of-season projections -- comes from espn_ros_projections.py and is joined
in later by build_inseason.py. Keeping them apart matters because the talent
file is global and shared across all four leagues, while this one is specific
to a single league's ownership situation.

Deliberately read-only. Nothing here proposes, submits, or executes a
transaction; it records what is true right now so the recommendation engines
have something to reason over.

REQUIRED environment variables:
    ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID
Optional:
    ESPN_MY_TEAM  -- team name or abbrev identifying Sean's team. If unset, the
                     script falls back to the team whose owner matches the SWID,
                     and failing that flags every team isMe=false and says so
                     loudly, because a manager cockpit with no "me" is useless.

OUTPUT: espn_inseason_<year>.json
"""
import os
import sys
import json
from datetime import datetime, timezone

from espn_api.football import League

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
LEAGUE_KEY = "zimmer"
OUT_FILE = f"espn_inseason_{YEAR}.json"
# Free agents are capped because the tail is unusable: past a few hundred you're
# looking at players nobody would start in any format. Sorted by ESPN's own
# ranking, so the cap trims the bottom, not the middle.
FA_LIMIT = 250


def get_credentials():
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    if not league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID (env var or GitHub Secret) first.")
    return int(league_id), os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")


def player_row(p):
    return {
        "player_id": getattr(p, "playerId", None),
        "name": p.name,
        "position": getattr(p, "position", None),
        "pro_team": getattr(p, "proTeam", None),
        # lineupSlot tells us whether they actually STARTED him, which is a
        # read on the owner's own opinion -- a stud on someone's bench is a
        # very different trade conversation from one in their lineup.
        "lineup_slot": getattr(p, "lineupSlot", None) or None,
        "injury_status": getattr(p, "injuryStatus", None),
        "injured": bool(getattr(p, "injured", False)),
        "percent_owned": getattr(p, "percent_owned", None),
        "acquisition_type": getattr(p, "acquisitionType", None),
    }


def identify_me(teams_raw, swid):
    """Which team is Sean's?

    Three strategies in descending order of reliability. The SWID match is
    tried before the name match because a team name can be changed mid-season
    on a whim, whereas the owner id cannot.
    """
    want = (os.environ.get("ESPN_MY_TEAM") or "").strip().lower()
    if want:
        for t in teams_raw:
            if want in (t.team_name or "").lower() or want == (t.team_abbrev or "").lower():
                return t.team_id, "ESPN_MY_TEAM"
    if swid:
        norm = swid.strip("{}").lower()
        for t in teams_raw:
            owners = getattr(t, "owners", []) or []
            for o in owners:
                oid = (o.get("id") if isinstance(o, dict) else str(o)) or ""
                if oid.strip("{}").lower() == norm:
                    return t.team_id, "SWID owner match"
    return None, None


def main():
    league_id, espn_s2, swid = get_credentials()
    print(f"Loading ESPN league {league_id} ({YEAR})...")
    league = League(league_id=league_id, year=YEAR, espn_s2=espn_s2, swid=swid)
    current_week = getattr(league, "current_week", None) or getattr(league, "nfl_week", 1)
    print(f"Current week: {current_week}")

    my_id, how = identify_me(league.teams, swid)
    if my_id is None:
        print("::warning::Could not identify Sean's team. Set ESPN_MY_TEAM to the team "
              "name or abbreviation -- without it the Manager Cockpit has no roster to "
              "manage and every recommendation engine will produce nothing.")
    else:
        print(f"Identified my team via {how}: team_id={my_id}")

    teams = []
    for t in league.teams:
        teams.append({
            "team_id": t.team_id,
            "team_name": t.team_name,
            "abbrev": t.team_abbrev,
            "manager": ", ".join(
                (o.get("firstName", "") + " " + o.get("lastName", "")).strip()
                if isinstance(o, dict) else str(o)
                for o in (getattr(t, "owners", []) or [])
            ) or None,
            "is_me": t.team_id == my_id,
            "wins": t.wins, "losses": t.losses, "ties": t.ties,
            "points_for": round(t.points_for, 2),
            "points_against": round(t.points_against, 2),
            "standing": t.standing,
            "roster": [player_row(p) for p in (t.roster or [])],
        })

    print("Fetching free agents...")
    try:
        fas = league.free_agents(size=FA_LIMIT)
    except Exception as e:
        # A failed FA fetch must not take the roster pull down with it: rosters
        # alone still power positional strength and trade targets, and a half
        # file beats no file during a live week.
        print(f"::warning::Free-agent fetch failed ({e}). Continuing with rosters only.")
        fas = []
    free_agents = [player_row(p) for p in fas]
    print(f"{len(free_agents)} free agents.")

    out = {
        "year": YEAR,
        "generated": datetime.now(timezone.utc).isoformat(),
        "current_week": current_week,
        "leagues": {
            LEAGUE_KEY: {
                "platform": "espn",
                "league_id": league_id,
                "current_week": current_week,
                "teams": teams,
                "free_agents": free_agents,
            }
        },
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    rostered = sum(len(t["roster"]) for t in teams)
    print(f"Wrote {OUT_FILE}: {len(teams)} teams, {rostered} rostered players, "
          f"{len(free_agents)} free agents.")


if __name__ == "__main__":
    main()
