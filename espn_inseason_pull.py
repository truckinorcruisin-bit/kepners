"""
espn_inseason_pull.py
Current roster / free-agent / standings state for every ESPN-hosted league.

This is the STATE half of the in-season data layer. The TALENT half --
rest-of-season projections -- comes from espn_ros_projections.py and is joined
in later by build_inseason.py. Keeping them apart matters because the talent
file is global and shared across every league, while this one is specific to
each league's own ownership situation.

Deliberately read-only. Nothing here proposes, submits, or executes a
transaction; it records what is true right now so the recommendation engines
have something to reason over.

MULTIPLE LEAGUES, ONE SCRIPT. ESPN_LEAGUES below lists every ESPN league to
pull; adding a new one (a work league, a friend's league) is a config entry
plus two GitHub secrets, not a code change. Each league also gets its
ROSTER SHAPE derived LIVE from ESPN's own league settings
(`League.settings.position_slot_counts`) rather than hand-typed anywhere --
Zimmer's shape used to live in league_rules.json, hand-maintained; that's now
a fallback only (see build_inseason.py's roster_slots_for), because a
hand-maintained shape is one more thing that silently drifts if a league's
settings ever change mid-season, and ESPN already knows the true answer.

REQUIRED environment variables (shared across every ESPN league on the SAME
ESPN login):
    ESPN_S2, ESPN_SWID
Per league, in ESPN_LEAGUES below:
    league_id_env       -- required, e.g. ESPN_LEAGUE_ID
    my_team_id_env       -- preferred way to say which team is Sean's: the
                             numeric team_id straight out of the league's own
                             URL (?leagueId=...&teamId=N). More reliable than
                             a name, which can be edited mid-season.
    my_team_name_env     -- fallback for a league where only a name is known.
If a league lives on a DIFFERENT ESPN account, give it its own cookie env vars
by adding s2_env/swid_env to that league's config entry; unset ones fall back
to the shared ESPN_S2/ESPN_SWID.

OUTPUT: espn_inseason_<year>.json (all ESPN leagues in one file, same shape
yahoo_inseason_pull.py already uses for its two)
"""
import os
import sys
import json
from datetime import datetime, timezone

from espn_api.football import League

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"espn_inseason_{YEAR}.json"
# Free agents are capped because the tail is unusable: past a few hundred
# you're looking at players nobody would start in any format. Sorted by
# ESPN's own ranking, so the cap trims the bottom, not the middle.
FA_LIMIT = 250

ESPN_LEAGUES = [
    {"key": "zimmer",
     "league_id_env": "ESPN_LEAGUE_ID",
     "my_team_name_env": "ESPN_MY_TEAM"},
    {"key": "inspire11",
     "league_id_env": "ESPN_LEAGUE_ID_INSPIRE11",
     "my_team_id_env": "ESPN_MY_TEAM_ID_INSPIRE11"},
]

# ESPN's own slot-type labels (from espn_api's POSITION_MAP) that mean
# something different in this codebase's vocabulary, or that don't belong in
# the STARTING/BENCH shape at all. Anything not listed here is kept literally
# -- an unexpected label (an IDP league's LB/DL/etc.) should show up as
# itself rather than being silently dropped, which would misrepresent a
# league's real shape instead of just looking unfamiliar.
SLOT_LABEL_MAP = {"D/ST": "DEF", "BE": "BEN"}
# Only a genuinely blank label is dropped outright. IR is NOT in this set --
# it needs its own count captured (see below), and it very nearly got
# swallowed here instead: this generic drop check ran BEFORE the IR-specific
# branch, so adding "IR" to it meant every league's IR slots silently read as
# zero. Caught by a direct test on the parser rather than downstream, where a
# league quietly missing its IR count would have looked like reasonable data.
SLOT_LABEL_DROP = {""}


def parse_roster_slots(league):
    """Expand ESPN's {label: count} settings into an ordered starting+bench
    list, e.g. {'QB':1,'RB':2,'RB/WR/TE':2,'D/ST':1,'K':1,'BE':7,'IR':1}
    -> (['QB','RB','RB','FLEX','FLEX','DEF','K','BEN'x7], ir_count=1).

    Live from the league's own settings rather than hand-typed -- see the
    module docstring for why that matters. A slot type ESPN reports with a
    zero count is dropped; one with a genuinely unfamiliar label is kept
    under its own name so it's visible rather than silently vanishing.
    """
    counts = getattr(getattr(league, "settings", None), "position_slot_counts", None) or {}
    slots, ir_count = [], 0
    for label, n in counts.items():
        n = int(n or 0)
        if n <= 0 or label in SLOT_LABEL_DROP:
            continue
        if label == "IR":
            ir_count = n
            continue
        canon = SLOT_LABEL_MAP.get(label, label)
        # Any RB/WR/TE-style combined slot is a FLEX in this codebase's
        # vocabulary, whichever two-or-three-way combo ESPN reports.
        if "/" in canon:
            canon = "FLEX"
        slots.extend([canon] * n)
    return slots, ir_count


def get_credentials(cfg):
    league_id = os.environ.get(cfg["league_id_env"])
    if not league_id:
        return None, None, None
    s2 = os.environ.get(cfg.get("s2_env") or "ESPN_S2")
    swid = os.environ.get(cfg.get("swid_env") or "ESPN_SWID")
    return int(league_id), s2, swid


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


def identify_me(cfg, teams_raw, swid):
    """Which team is Sean's, in descending order of reliability.

    A raw team_id (from the league's own URL) is preferred over anything
    else -- it's ESPN's own permanent identifier for that team slot, unlike a
    name (editable any time) or even the SWID-owner match (breaks if a league
    is ever transferred to a co-manager). Tried first when the config
    provides one.
    """
    want_id = (os.environ.get(cfg.get("my_team_id_env") or "") or "").strip()
    if want_id:
        for t in teams_raw:
            if str(t.team_id) == want_id:
                return t.team_id, f"team_id={want_id}"
        print(f"  ::warning::{cfg['key']}: {cfg['my_team_id_env']}={want_id} did not "
              f"match any team_id in this league ({[t.team_id for t in teams_raw]}). "
              f"Falling back to name/SWID matching.")

    want_name = (os.environ.get(cfg.get("my_team_name_env") or "") or "").strip().lower()
    if want_name:
        for t in teams_raw:
            if want_name in (t.team_name or "").lower() or want_name == (t.team_abbrev or "").lower():
                return t.team_id, cfg["my_team_name_env"]

    if swid:
        norm = swid.strip("{}").lower()
        for t in teams_raw:
            owners = getattr(t, "owners", []) or []
            for o in owners:
                oid = (o.get("id") if isinstance(o, dict) else str(o)) or ""
                if oid.strip("{}").lower() == norm:
                    return t.team_id, "SWID owner match"
    return None, None


def pull_one_league(cfg):
    league_id, espn_s2, swid = get_credentials(cfg)
    if league_id is None:
        print(f"  {cfg['key']}: {cfg['league_id_env']} not set -- skipping.")
        return None

    print(f"  Loading ESPN league {league_id} ({cfg['key']}, {YEAR})...")
    try:
        league = League(league_id=league_id, year=YEAR, espn_s2=espn_s2, swid=swid)
    except Exception as e:
        # One bad league (wrong id, expired cookie, no access) must not cost
        # every other ESPN league in this run -- same reasoning as the
        # free-agent try/except below, one level up.
        print(f"  ::warning::{cfg['key']}: failed to load ({e}). Skipping this "
              f"league for this run; the rest of the pull continues.")
        return None

    current_week = getattr(league, "current_week", None) or getattr(league, "nfl_week", 1)
    roster_slots, ir_count = parse_roster_slots(league)
    print(f"    week {current_week} | roster shape (live from ESPN): {roster_slots}"
          f"{f' + {ir_count} IR' if ir_count else ''}")

    my_id, how = identify_me(cfg, league.teams, swid)
    if my_id is None:
        print(f"    ::warning::{cfg['key']}: could not identify Sean's team. Set "
              f"{cfg.get('my_team_id_env') or cfg.get('my_team_name_env')} -- without "
              f"it the Manager Cockpit has no roster to manage in this league.")
    else:
        print(f"    identified my team via {how}: team_id={my_id}")

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

    print(f"    fetching free agents...")
    try:
        fas = league.free_agents(size=FA_LIMIT)
    except Exception as e:
        # A failed FA fetch must not take the roster pull down with it:
        # rosters alone still power positional strength and trade targets,
        # and a half file beats no file during a live week.
        print(f"    ::warning::{cfg['key']}: free-agent fetch failed ({e}). "
              f"Continuing with rosters only.")
        fas = []
    free_agents = [player_row(p) for p in fas]

    # Cross-check against this SAME league's own rosters. ESPN's
    # free_agents() endpoint and its roster endpoint are two independent
    # calls, and can briefly disagree -- a player claimed off waivers can
    # still show up as "available" for a short window afterward. Since we
    # already pulled real roster data for this exact league above, this is a
    # free sanity check: never surface a recommendation for someone another
    # team in THIS league already rosters, no matter what free_agents() says.
    rostered_ids = {
        p.get("player_id") for t in teams for p in t["roster"]
        if p.get("player_id") is not None
    }
    dropped = [p["name"] for p in free_agents if p.get("player_id") in rostered_ids]
    free_agents = [p for p in free_agents if p.get("player_id") not in rostered_ids]
    if dropped:
        print(f"    ::warning::{cfg['key']}: dropped {len(dropped)} name(s) ESPN's "
              f"free_agents() call listed as available but who are actually rostered "
              f"per this same pull's team data: {', '.join(dropped)}. This is ESPN's "
              f"raw feed briefly disagreeing with itself, not a bug here -- but if this "
              f"keeps happening for the same player across runs, it's worth a closer look.")

    print(f"    {len(free_agents)} free agents.")

    return {
        "platform": "espn",
        "league_id": league_id,
        "current_week": current_week,
        "rosterSlots": roster_slots,
        "irSlots": ir_count,
        "teams": teams,
        "free_agents": free_agents,
    }


def main():
    print(f"Pulling {len(ESPN_LEAGUES)} configured ESPN league(s)...")
    leagues_out, current_week = {}, None
    for cfg in ESPN_LEAGUES:
        result = pull_one_league(cfg)
        if result is not None:
            leagues_out[cfg["key"]] = result
            current_week = current_week or result["current_week"]

    if not leagues_out:
        # Every configured league failed or was unconfigured. Historically
        # this script covered exactly one league and that WAS fatal; now that
        # it's a loop, a single missing config entry must not look like a
        # total outage, but zero leagues genuinely is one.
        raise SystemExit(
            "No ESPN league produced data this run -- check ESPN_LEAGUE_ID / "
            "ESPN_LEAGUE_ID_INSPIRE11 and the shared ESPN_S2/ESPN_SWID secrets."
        )

    out = {
        "year": YEAR,
        "generated": datetime.now(timezone.utc).isoformat(),
        "current_week": current_week,
        "leagues": leagues_out,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    for key, lg in leagues_out.items():
        rostered = sum(len(t["roster"]) for t in lg["teams"])
        print(f"Wrote {key}: {len(lg['teams'])} teams, {rostered} rostered players, "
              f"{len(lg['free_agents'])} free agents.")
    print(f"-> {OUT_FILE}")


if __name__ == "__main__":
    main()
