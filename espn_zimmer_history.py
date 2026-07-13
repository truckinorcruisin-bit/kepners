"""
espn_zimmer_history.py
Pulls every historical Zimmer league draft from ESPN and writes zimmer_draft_history.json.

Uses the `espn_api` package (pip install espn_api) which wraps ESPN's unofficial
Fantasy API and already handles player-name resolution and team/owner mapping --
no need to hand-roll the raw requests + cookie logic.

REQUIRED environment variables (set locally for a test run, or as GitHub Secrets
for the Actions workflow):
    ESPN_LEAGUE_ID   -- numeric ID from your league's URL, e.g. .../leagueId=899513
    ESPN_S2          -- cookie value (see setup notes below)
    ESPN_SWID        -- cookie value, looks like {XXXXXXXX-XXXX-...}

HOW TO GET ESPN_S2 / ESPN_SWID (no coding needed, just your browser):
1. Log into https://fantasy.espn.com and open your Zimmer league
2. Right-click anywhere on the page -> Inspect (opens developer tools)
3. Click the "Application" tab (Chrome/Edge) or "Storage" tab (Firefox)
4. In the left sidebar: Cookies -> https://fantasy.espn.com
5. Find the row named "espn_s2" -> copy its Value -> that's ESPN_S2
6. Find the row named "SWID" -> copy its Value (keep the curly braces {}) -> that's ESPN_SWID

These don't require any app registration or approval process (unlike Yahoo) --
they're just your existing login session cookies.

SEASONS PULLED: edit ZIMMER_YEARS below to match how far back the league goes.

AUCTION LEAGUES: if Zimmer runs an auction draft (price paid per player instead
of round/pick order), this script detects that automatically from the data and
outputs "cost" (price paid) and "nomination_order" instead of "round"/"pick" --
no configuration needed either way.
"""
import os
import json
from espn_api.football import League

ZIMMER_YEARS = list(range(2020, 2026))  # 2020 through 2025; add 2026 once that draft happens


def get_credentials():
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")
    if not league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID (env var or GitHub Secret) first.")
    return int(league_id), espn_s2, swid


def pull_season(league_id, year, espn_s2, swid):
    try:
        league = League(league_id=league_id, year=year, espn_s2=espn_s2, swid=swid)
    except Exception as e:
        print(f"  {year}: could not load ({e})")
        return None

    teams = {}
    for team in league.teams:
        owner_names = [o.get("displayName", "Unknown") for o in team.owners]
        teams[team.team_id] = {
            "team_name": team.team_name,
            "owners": owner_names,
        }

    # Resolve positions for every drafted player in one batched call. The draft
    # objects only carry playerId + name; position comes from the player card.
    pos_map = {}
    drafted_ids = [p.playerId for p in league.draft if p.playerId]
    if drafted_ids:
        try:
            infos = league.player_info(playerId=drafted_ids)
            if infos is None:
                infos = []
            if not isinstance(infos, list):
                infos = [infos]
            for pl in infos:
                if pl:
                    pos_map[pl.playerId] = pl.position
        except Exception as e:
            print(f"  {year}: position lookup failed, positions will be blank ({e})")

    # Auction leagues populate bid_amount on every pick; snake drafts leave it
    # None/0. Detect from the data itself rather than trusting settings fields,
    # since espn_api doesn't expose draft type directly.
    is_auction = any((p.bid_amount or 0) > 0 for p in league.draft)

    picks = []
    for pick in league.draft:
        base = {
            "team_id": pick.team.team_id if pick.team else None,
            "team_name": pick.team.team_name if pick.team else None,
            "player": pick.playerName,
            "player_id": pick.playerId,
            "position": pos_map.get(pick.playerId, ""),
            "keeper": pick.keeper_status,
        }
        if is_auction:
            base["cost"] = pick.bid_amount
            base["nomination_order"] = pick.round_pick  # ESPN still assigns a sequence number
            base["nominated_by"] = pick.nominatingTeam.team_name if pick.nominatingTeam else None
        else:
            base["round"] = pick.round_num
            base["pick"] = pick.round_pick
        picks.append(base)

    if is_auction:
        # Most useful sorted by price paid, highest first, for tendency analysis
        picks.sort(key=lambda p: p.get("cost") or 0, reverse=True)

    return {"draft_type": "auction" if is_auction else "snake", "teams": teams, "picks": picks}


def main():
    league_id, espn_s2, swid = get_credentials()
    history = {"league_id": league_id, "seasons": {}}

    for year in ZIMMER_YEARS:
        print(f"Pulling {year}...")
        season_data = pull_season(league_id, year, espn_s2, swid)
        if season_data:
            history["seasons"][year] = season_data
            print(f"  {year}: {season_data['draft_type']} draft, "
                  f"{len(season_data['picks'])} picks, {len(season_data['teams'])} teams")

    with open("zimmer_draft_history.json", "w") as f:
        json.dump(history, f, indent=2)
    print(f"\nWrote zimmer_draft_history.json -- {len(history['seasons'])} season(s).")


if __name__ == "__main__":
    main()
