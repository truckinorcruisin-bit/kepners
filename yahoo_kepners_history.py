"""
yahoo_kepners_history.py
Pulls every historical Kepners Keepers draft (all seasons Yahoo has on file for your account)
and writes a raw JSON dump: kepners_draft_history.json

Run yahoo_setup.py successfully first (needs yahoo_token.json in this folder).
"""
import json
from yahoo_setup import api_get

LEAGUE_NAME_MATCH = "kepners"   # case-insensitive substring match on league name


def find_kepners_leagues():
    """Every NFL season's leagues for this user; filter to Kepners by name."""
    data = api_get("users;use_login=1/games;game_codes=nfl/leagues")
    games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
    found = []
    for gi in games:
        if gi == "count":
            continue
        game = games[gi]["game"]
        game_meta = game[0]
        leagues_block = game[1].get("leagues", {}) if len(game) > 1 else {}
        for li in leagues_block:
            if li == "count":
                continue
            league = leagues_block[li]["league"][0]
            if LEAGUE_NAME_MATCH in league["name"].lower():
                found.append({
                    "season": game_meta["season"],
                    "league_key": league["league_key"],
                    "league_id": league["league_id"],
                    "name": league["name"],
                    "num_teams": league.get("num_teams"),
                })
    return found


def get_teams(league_key):
    data = api_get(f"league/{league_key}/teams")
    teams_block = data["fantasy_content"]["league"][1]["teams"]
    out = {}
    for ti in teams_block:
        if ti == "count":
            continue
        team_arr = teams_block[ti]["team"][0]
        team = {}
        for field in team_arr:
            if isinstance(field, dict):
                team.update(field)
        managers = team.get("managers", [])
        manager_name = None
        for m in managers:
            if "manager" in m:
                manager_name = m["manager"].get("nickname")
        out[team.get("team_key")] = {
            "team_name": team.get("name"),
            "manager": manager_name,
            "draft_position": team.get("draft_position"),
        }
    return out


def get_draft_results(league_key):
    data = api_get(f"league/{league_key}/draftresults")
    dr_block = data["fantasy_content"]["league"][1]["draft_results"]
    picks = []
    for di in dr_block:
        if di == "count":
            continue
        dr = dr_block[di]["draft_result"]
        picks.append({
            "pick": dr.get("pick"),
            "round": dr.get("round"),
            "team_key": dr.get("team_key"),
            "player_key": dr.get("player_key"),
        })
    return picks


def get_player_names(league_key, player_keys):
    """Yahoo allows batching up to 25 player_keys per call."""
    names = {}
    chunk = 25
    keys = list(player_keys)
    for i in range(0, len(keys), chunk):
        batch = keys[i:i+chunk]
        pk_str = ",".join(batch)
        data = api_get(f"league/{league_key}/players;player_keys={pk_str}")
        players_block = data["fantasy_content"]["league"][1]["players"]
        for pi in players_block:
            if pi == "count":
                continue
            p = players_block[pi]["player"][0]
            pdata = {}
            for field in p:
                if isinstance(field, dict):
                    pdata.update(field)
            names[pdata.get("player_key")] = {
                "name": pdata.get("name", {}).get("full"),
                "position": pdata.get("display_position"),
                "nfl_team": pdata.get("editorial_team_abbr"),
            }
    return names


def main():
    print("Finding Kepners leagues across seasons...")
    leagues = find_kepners_leagues()
    print(f"Found {len(leagues)} season(s):")
    for lg in leagues:
        print(f"  {lg['season']}: {lg['name']} ({lg['league_key']})")

    history = {"league_name_match": LEAGUE_NAME_MATCH, "seasons": {}}

    for lg in leagues:
        season = lg["season"]
        print(f"\nPulling {season}...")
        teams = get_teams(lg["league_key"])
        picks = get_draft_results(lg["league_key"])
        player_keys = {p["player_key"] for p in picks if p["player_key"]}
        players = get_player_names(lg["league_key"], player_keys)

        enriched_picks = []
        for p in picks:
            team_info = teams.get(p["team_key"], {})
            player_info = players.get(p["player_key"], {})
            enriched_picks.append({
                "round": p["round"],
                "pick": p["pick"],
                "manager": team_info.get("manager"),
                "team_name": team_info.get("team_name"),
                "player": player_info.get("name"),
                "position": player_info.get("position"),
                "nfl_team": player_info.get("nfl_team"),
            })

        history["seasons"][season] = {
            "league_key": lg["league_key"],
            "num_teams": lg["num_teams"],
            "teams": teams,
            "picks": enriched_picks,
        }

    with open("kepners_draft_history.json", "w") as f:
        json.dump(history, f, indent=2)
    print(f"\nWrote kepners_draft_history.json -- {len(leagues)} season(s).")


if __name__ == "__main__":
    main()
