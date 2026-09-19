"""
yahoo_inseason_pull.py
Current roster / free-agent / standings state for the Yahoo leagues
(Kepners Keepers and Miami Domers).

Counterpart to espn_inseason_pull.py. Same contract, same output shape, so
build_inseason.py can merge the two without caring which platform a league
lives on. Read-only: nothing here proposes or submits a transaction.

NO PROJECTIONS ARE PULLED HERE, on purpose. Yahoo exposes nothing comparable
to ESPN's weekly projections, and mixing two projection sources would make a
Kepners recommendation and a Zimmer recommendation incomparable. ESPN is the
single projection engine for every league (see espn_ros_projections.py);
Yahoo's job is only to say who owns whom.

PREREQUISITE: working Yahoo auth. Run yahoo_setup.py once interactively, then
store these three as GitHub secrets:
    YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REFRESH_TOKEN

OUTPUT: yahoo_inseason_<year>.json  (both leagues in one file)
"""
import os
import sys
import json
from datetime import datetime, timezone

from yahoo_setup import api_get

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"yahoo_inseason_{YEAR}.json"

# Which Yahoo league maps to which key in bigboard.json. Substring match on the
# league name, case-insensitive -- Yahoo league names drift year to year but
# these stems have been stable.
LEAGUE_MATCH = {
    "kepners": "kepners",
    "miami": "miami",
}
# Yahoo hard-caps the players collection at 25 per request regardless of what
# you ask for, so any larger pull has to be paged.
PAGE = 25
FA_LIMIT = 200


# ---------------------------------------------------------------------------
# Yahoo's JSON is a hybrid of dicts-keyed-by-stringified-index and lists whose
# elements are single-key dicts. These two helpers absorb that so the parsing
# below reads like normal code. Every Yahoo script in this repo re-derives some
# version of this; if a third one appears, hoist these into a shared module.
# ---------------------------------------------------------------------------
def flatten(obj):
    """Collapse Yahoo's list-of-single-key-dicts into one flat dict."""
    out = {}
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                out.update(item)
            elif isinstance(item, list):
                out.update(flatten(item))
    return out


def numbered(block):
    """Yield the values of a Yahoo collection keyed '0','1',...,'count'."""
    if not isinstance(block, dict):
        return
    for k, v in block.items():
        if k == "count":
            continue
        yield v


def find_leagues():
    """This season's NFL leagues for the logged-in user, matched to our keys."""
    data = api_get(f"users;use_login=1/games;game_codes=nfl;seasons={YEAR}/leagues")
    users = data["fantasy_content"]["users"]["0"]["user"]
    games = {}
    for part in users:
        if isinstance(part, dict) and "games" in part:
            games = part["games"]
    found = {}
    for g in numbered(games):
        game = g.get("game")
        if not game:
            continue
        leagues_block = {}
        for part in game if isinstance(game, list) else [game]:
            if isinstance(part, dict) and "leagues" in part:
                leagues_block = part["leagues"]
        for l in numbered(leagues_block):
            lg = flatten(l.get("league"))
            name = (lg.get("name") or "").lower()
            for stem, key in LEAGUE_MATCH.items():
                if stem in name and key not in found:
                    found[key] = {
                        "league_key": lg.get("league_key"),
                        "name": lg.get("name"),
                        "num_teams": lg.get("num_teams"),
                        "current_week": lg.get("current_week"),
                        "scoring_type": lg.get("scoring_type"),
                    }
    return found


def parse_player(entry):
    """One player out of any Yahoo players collection (roster or free agent)."""
    p = flatten(entry.get("player", entry))
    name = p.get("name") or {}
    pos = p.get("display_position") or p.get("primary_position")
    # Yahoo returns eligible positions as a list of dicts; the selected lineup
    # slot (when present) lives in a separate 'selected_position' block.
    sel = p.get("selected_position")
    lineup_slot = None
    if sel:
        lineup_slot = flatten(sel).get("position")
    return {
        "player_id": p.get("player_id"),
        "player_key": p.get("player_key"),
        "name": name.get("full") if isinstance(name, dict) else name,
        "position": pos,
        "pro_team": p.get("editorial_team_abbr"),
        "lineup_slot": lineup_slot,
        # Yahoo's status is a terse code ("Q", "O", "IR", "NA"); left raw here
        # and normalized in build_inseason.py alongside ESPN's wordier version.
        "injury_status": p.get("status") or None,
        "injured": bool(p.get("status")),
        "percent_owned": None,   # only present on a separate ownership subresource
    }


def get_teams_with_rosters(league_key):
    data = api_get(f"league/{league_key}/teams/roster")
    teams_block = data["fantasy_content"]["league"][1]["teams"]
    teams = []
    for t in numbered(teams_block):
        team = t.get("team")
        meta = flatten(team[0]) if isinstance(team, list) else flatten(team)
        managers = meta.get("managers") or []
        manager = None
        is_me = False
        for m in managers:
            mm = flatten(m.get("manager", m))
            manager = mm.get("nickname") or manager
            # Yahoo flags the logged-in user's own team directly. Far more
            # reliable than matching on a team name the owner can rename.
            if str(mm.get("is_current_login", "0")) == "1":
                is_me = True
        roster_players = []
        for part in (team if isinstance(team, list) else []):
            if isinstance(part, dict) and "roster" in part:
                rblock = flatten(part["roster"]).get("0", {})
                for pl in numbered(rblock.get("players", {}) if isinstance(rblock, dict) else {}):
                    roster_players.append(parse_player(pl))
        teams.append({
            "team_id": meta.get("team_id"),
            "team_key": meta.get("team_key"),
            "team_name": meta.get("name"),
            "manager": manager,
            "is_me": is_me,
            "roster": roster_players,
        })
    return teams


def attach_standings(league_key, teams):
    """Records come from a different endpoint than rosters. Failure here is
    non-fatal: a missing W-L is cosmetic, a missing roster is not."""
    try:
        data = api_get(f"league/{league_key}/standings")
        block = data["fantasy_content"]["league"][1]["standings"][0]["teams"]
    except Exception as e:
        print(f"  ::warning::standings fetch failed ({e}) -- records will be blank.")
        return
    by_key = {t["team_key"]: t for t in teams}
    for t in numbered(block):
        team = t.get("team")
        meta = flatten(team[0]) if isinstance(team, list) else flatten(team)
        rest = flatten(team[1:]) if isinstance(team, list) and len(team) > 1 else {}
        tk = meta.get("team_key")
        if tk not in by_key:
            continue
        outcome = flatten((rest.get("team_standings") or {}).get("outcome_totals", {}))
        standings = flatten(rest.get("team_standings") or {})
        by_key[tk].update({
            "wins": _int(outcome.get("wins")),
            "losses": _int(outcome.get("losses")),
            "ties": _int(outcome.get("ties")),
            "points_for": _float(standings.get("points_for")),
            "points_against": _float(standings.get("points_against")),
            "standing": _int(standings.get("rank")),
        })


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def get_free_agents(league_key, limit=FA_LIMIT):
    """Available players, ranked by Yahoo's own actual-rank sort, paged at 25.

    Stops early on a short page: Yahoo signals the end of a collection by
    returning fewer than the requested count, not by an explicit flag.
    """
    out = []
    start = 0
    while len(out) < limit:
        path = (f"league/{league_key}/players;status=A;sort=AR;"
                f"start={start};count={PAGE}")
        try:
            data = api_get(path)
            block = data["fantasy_content"]["league"][1]["players"]
        except Exception as e:
            print(f"  ::warning::free-agent page at start={start} failed ({e}); "
                  f"keeping the {len(out)} already fetched.")
            break
        if not isinstance(block, dict):
            break
        page_rows = [parse_player(p) for p in numbered(block)]
        if not page_rows:
            break
        out.extend(page_rows)
        if len(page_rows) < PAGE:
            break
        start += PAGE
    return out[:limit]


def main():
    if not os.environ.get("YAHOO_REFRESH_TOKEN") and not os.path.exists("yahoo_token.json"):
        raise SystemExit(
            "No Yahoo credentials. Run yahoo_setup.py once interactively, then store "
            "the resulting refresh_token as the YAHOO_REFRESH_TOKEN secret."
        )

    print(f"Finding {YEAR} Yahoo leagues...")
    leagues = find_leagues()
    if not leagues:
        raise SystemExit(
            f"No matching {YEAR} leagues found for this Yahoo account. Expected names "
            f"containing: {', '.join(LEAGUE_MATCH)}. If a league was renamed, update "
            f"LEAGUE_MATCH at the top of this script."
        )
    for key, meta in leagues.items():
        print(f"  {key}: {meta['name']} ({meta['league_key']})")

    missing = set(LEAGUE_MATCH.values()) - set(leagues)
    if missing:
        print(f"::warning::No {YEAR} league matched for: {', '.join(sorted(missing))}. "
              f"That league will be absent from the Manager Cockpit.")

    out_leagues = {}
    for key, meta in leagues.items():
        lk = meta["league_key"]
        print(f"Pulling {key} rosters...")
        teams = get_teams_with_rosters(lk)
        attach_standings(lk, teams)
        if not any(t.get("is_me") for t in teams):
            print(f"  ::warning::No team in {key} is flagged as the logged-in user. "
                  f"The cockpit won't know which roster is yours.")
        print(f"  {len(teams)} teams, {sum(len(t['roster']) for t in teams)} rostered players.")
        print(f"Pulling {key} free agents...")
        fas = get_free_agents(lk)
        print(f"  {len(fas)} free agents.")
        out_leagues[key] = {
            "platform": "yahoo",
            "league_key": lk,
            "league_name": meta["name"],
            "current_week": _int(meta.get("current_week")),
            "teams": teams,
            "free_agents": fas,
        }

    out = {
        "year": YEAR,
        "generated": datetime.now(timezone.utc).isoformat(),
        "leagues": out_leagues,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE} ({len(out_leagues)} league(s)).")


if __name__ == "__main__":
    main()
