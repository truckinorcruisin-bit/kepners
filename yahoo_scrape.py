#!/usr/bin/env python3
"""
yahoo_scrape.py — Cookie-authenticated HTML scraper for Yahoo Fantasy Football rosters.

Workaround for the still-blocked Yahoo Fantasy Sports API. Yahoo's roster pages are
server-rendered HTML with the full roster -- including keeper status -- embedded
directly in the initial page load, no client-side JS rendering required. This script
fetches those pages using a cookie string exported from a logged-in browser session
and parses out roster/keeper data into JSON.

USAGE
-----
    python yahoo_scrape.py --league-id 102398 --teams 1-12 \
        --cookie-file yahoo_cookie.txt --out kepners_live_rosters.json

COOKIE
------
Export from a browser logged into football.fantasysports.yahoo.com:
DevTools -> Application (Chrome) / Storage (Firefox) -> Cookies -> copy every
name=value pair as one "Cookie:" header string, OR right-click any request for that
site -> Copy -> Copy as cURL, then pull the -H 'Cookie: ...' value out of that.
Save it as a single line in a text file and pass via --cookie-file, or set it as
the YAHOO_COOKIE_STRING environment variable / GitHub Actions secret.

Cookies expire (days to weeks). When this script starts producing 0-player teams
or LOGIN WALL errors, re-export a fresh cookie string from the browser.

OUTPUT SHAPE
------------
{
  "league_id": "102398",
  "fetched_at": "2026-09-29T12:00:00+00:00",
  "teams": {
    "12": {
      "team_name": "The Pickups",
      "player_count": 17,
      "keeper_count": 2,
      "players": [
        {
          "player_id": "40993",
          "name": "Bucky Irving",
          "position": "RB",
          "nfl_team": "TB",
          "roster_slot": "RB",
          "is_keeper": true,
          "bye_week": "10"
        },
        ...
      ]
    }
  }
}
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: requires 'requests'. Install with: pip install requests --break-system-packages", file=sys.stderr)
    raise

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: requires 'beautifulsoup4'. Install with: pip install beautifulsoup4 --break-system-packages", file=sys.stderr)
    raise


ROSTER_URL_TMPL = "https://football.fantasysports.yahoo.com/f1/{league_id}/{team_id}/team"

# These strings only ever appear on Yahoo's real sign-in page, never on a rendered
# roster page -- if we see them, the cookie has expired.
LOGIN_WALL_MARKERS = (
    'id="login-username"',
    'id="login-signin"',
    "login.yahoo.com",
)


class LoginWallError(RuntimeError):
    """Response looks like a Yahoo login page instead of roster HTML (expired cookie)."""


def load_cookie_string(args):
    if args.cookie_file:
        with open(args.cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    env_cookie = os.environ.get("YAHOO_COOKIE_STRING")
    if env_cookie:
        return env_cookie.strip()
    raise SystemExit(
        "No cookie provided. Pass --cookie-file, or set the YAHOO_COOKIE_STRING "
        "environment variable (e.g. via a GitHub Actions secret)."
    )


def parse_team_range(spec):
    """'1-12' -> [1..12], '1,3,5' -> [1,3,5], '7' -> [7]."""
    teams = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            teams.extend(range(int(lo), int(hi) + 1))
        else:
            teams.append(int(part))
    return teams


def fetch_roster_html(session, league_id, team_id, week=None):
    url = ROSTER_URL_TMPL.format(league_id=league_id, team_id=team_id)
    params = {"week": week} if week else {}
    resp = session.get(url, params=params, timeout=20)
    resp.raise_for_status()
    html = resp.text
    if any(marker in html for marker in LOGIN_WALL_MARKERS):
        raise LoginWallError(
            f"Team {team_id}: response looks like a Yahoo login page, not a roster. "
            "Cookie has likely expired -- re-export from the browser."
        )
    return html


def parse_team_name(soup):
    """Best-effort team name from <title> ('Kepners Keepers - The Pickups | ...')."""
    title = soup.find("title")
    if not title or not title.text:
        return None
    text = title.text.strip()
    if " - " in text:
        after_dash = text.split(" - ", 1)[1]
        return after_dash.split("|")[0].strip()
    return text


def parse_roster_row(row):
    """Extract one player's data from a roster <tr>. Returns None for rows with no player."""
    name_link = row.select_one("a.name[data-ys-playerid]")
    if not name_link:
        return None

    player_id = name_link.get("data-ys-playerid", "").strip()
    name = name_link.get_text(strip=True)

    pos_span = row.select_one(".pos-label")
    roster_slot = pos_span.get("data-pos", "").strip() if pos_span else None

    # Team/position detail line looks like "TB - RB" inside a small <span class="Fz-xxs">
    # that sits alongside the keeper icon, inside the .ysf-player-name block.
    nfl_team, position = None, None
    for span in row.select(".ysf-player-name .Fz-xxs"):
        detail_text = span.get_text(strip=True)
        if " - " in detail_text:
            nfl_team, position = [p.strip() for p in detail_text.split(" - ", 1)]
            break

    # Bye week -- first numeric-only <td><div> after the player cell.
    bye_week = None
    for td in row.find_all("td"):
        div = td.find("div")
        if div and re.fullmatch(r"\d{1,2}", div.get_text(strip=True) or ""):
            bye_week = div.get_text(strip=True)
            break

    # Keeper flag -- unambiguous: an icon span carries this exact title when present.
    is_keeper = row.find("span", title="This player is a keeper.") is not None

    return {
        "player_id": player_id,
        "name": name,
        "position": position,
        "nfl_team": nfl_team,
        "roster_slot": roster_slot,
        "is_keeper": is_keeper,
        "bye_week": bye_week,
    }


def parse_roster(html):
    soup = BeautifulSoup(html, "html.parser")
    team_name = parse_team_name(soup)

    players = []
    seen_ids = set()
    # Offense, kickers, and DEF/ST each get their own table but share row structure.
    for wrap_id in ("statTable0-wrap", "statTable1-wrap", "statTable2-wrap"):
        wrap = soup.find(id=wrap_id)
        if not wrap:
            continue
        for row in wrap.select("tbody tr"):
            player = parse_roster_row(row)
            if player and player["player_id"] not in seen_ids:
                seen_ids.add(player["player_id"])
                players.append(player)

    return team_name, players


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league-id", required=True, help="Yahoo numeric league ID, e.g. 102398")
    parser.add_argument("--teams", required=True, help="Team ID(s), e.g. '1-12' or '3,7,9'")
    parser.add_argument("--cookie-file", help="Path to a file containing the exported Yahoo cookie string")
    parser.add_argument("--week", help="Optional week number to pass through to the roster URL")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--sleep", type=float, default=1.5, help="Seconds between requests (be polite to Yahoo)")
    args = parser.parse_args()

    cookie_string = load_cookie_string(args)

    session = requests.Session()
    session.headers.update({
        "Cookie": cookie_string,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })

    team_ids = parse_team_range(args.teams)
    result = {
        "league_id": args.league_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "teams": {},
    }

    errors = []
    for i, team_id in enumerate(team_ids):
        print(f"Fetching team {team_id} ({i + 1}/{len(team_ids)})...", file=sys.stderr)
        try:
            html = fetch_roster_html(session, args.league_id, team_id, week=args.week)
        except LoginWallError as e:
            print(f"  LOGIN WALL: {e}", file=sys.stderr)
            errors.append(str(e))
            break  # every subsequent request will fail the same way -- stop early
        except requests.RequestException as e:
            print(f"  Request failed: {e}", file=sys.stderr)
            errors.append(f"Team {team_id}: {e}")
            continue

        team_name, players = parse_roster(html)
        if not players:
            print(
                f"  WARNING: 0 players parsed for team {team_id} -- page structure may "
                f"have changed, or this wasn't a roster page.",
                file=sys.stderr,
            )

        result["teams"][str(team_id)] = {
            "team_name": team_name,
            "player_count": len(players),
            "keeper_count": sum(1 for p in players if p["is_keeper"]),
            "players": players,
        }

        if i < len(team_ids) - 1:
            time.sleep(args.sleep)

    if errors:
        result["errors"] = errors

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    total_players = sum(t["player_count"] for t in result["teams"].values())
    total_keepers = sum(t["keeper_count"] for t in result["teams"].values())
    print(
        f"\nDone. {len(result['teams'])} team(s), {total_players} players, "
        f"{total_keepers} flagged as keepers. Wrote {args.out}",
        file=sys.stderr,
    )
    if errors:
        print(f"{len(errors)} error(s) -- see 'errors' key in output JSON.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
