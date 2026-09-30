#!/usr/bin/env python3
"""
yahoo_parse_local.py — Parses committed Yahoo roster HTML files into JSON.

This is the second half of the "phone bridge" pipeline: a phone Shortcut fetches
each team's roster page (using a cookie, from the phone's own network) and pushes
the raw HTML into this repo under a folder like yahoo_roster_raw/. This script
then runs in GitHub Actions -- triggered by that push -- and parses whatever HTML
files are present into a single JSON file, with no network access or cookie of
its own required.

Parsing logic is the same as yahoo_scrape.py's parse_roster(); see that file's
docstring for the page-structure notes this relies on (stable across the two
scripts on purpose -- if Yahoo changes their markup, both need updating together).

USAGE
-----
    python yahoo_parse_local.py --html-dir yahoo_roster_raw --out kepners_live_rosters.json

Expects filenames like team_12.html, team_3.html, etc. -- the numeric team ID is
pulled from the filename, not from inside the HTML (the HTML's own team_key would
also work, but the filename is simpler to control from the phone side).
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: requires 'beautifulsoup4'. Install with: pip install beautifulsoup4 --break-system-packages", file=sys.stderr)
    raise


# These strings only ever appear on Yahoo's real sign-in page, never on a rendered
# roster page -- if a committed file contains one, the phone's cookie had expired
# at fetch time and we should flag it rather than silently parse 0 players.
LOGIN_WALL_MARKERS = (
    'id="login-username"',
    'id="login-signin"',
    "login.yahoo.com",
)

TEAM_ID_RE = re.compile(r"team_(\d+)\.html$")


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

    nfl_team, position = None, None
    for span in row.select(".ysf-player-name .Fz-xxs"):
        detail_text = span.get_text(strip=True)
        if " - " in detail_text:
            nfl_team, position = [p.strip() for p in detail_text.split(" - ", 1)]
            break

    bye_week = None
    for td in row.find_all("td"):
        div = td.find("div")
        if div and re.fullmatch(r"\d{1,2}", div.get_text(strip=True) or ""):
            bye_week = div.get_text(strip=True)
            break

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


def parse_roster_html(html):
    """Returns (team_name, players, is_login_wall) for one team's roster HTML."""
    if any(marker in html for marker in LOGIN_WALL_MARKERS):
        return None, [], True

    soup = BeautifulSoup(html, "html.parser")
    team_name = parse_team_name(soup)

    players = []
    seen_ids = set()
    for wrap_id in ("statTable0-wrap", "statTable1-wrap", "statTable2-wrap"):
        wrap = soup.find(id=wrap_id)
        if not wrap:
            continue
        for row in wrap.select("tbody tr"):
            player = parse_roster_row(row)
            if player and player["player_id"] not in seen_ids:
                seen_ids.add(player["player_id"])
                players.append(player)

    return team_name, players, False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--html-dir", required=True, help="Directory containing team_<id>.html files")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    html_dir = Path(args.html_dir)
    if not html_dir.is_dir():
        print(f"ERROR: {html_dir} is not a directory (nothing pushed yet?)", file=sys.stderr)
        sys.exit(1)

    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "teams": {},
    }

    html_files = sorted(html_dir.glob("team_*.html"))
    if not html_files:
        print(f"WARNING: no team_*.html files found in {html_dir}", file=sys.stderr)

    login_wall_teams = []
    for html_file in html_files:
        match = TEAM_ID_RE.search(html_file.name)
        if not match:
            print(f"  Skipping {html_file.name} -- doesn't match team_<id>.html pattern", file=sys.stderr)
            continue
        team_id = match.group(1)

        html = html_file.read_text(encoding="utf-8", errors="replace")
        team_name, players, is_login_wall = parse_roster_html(html)

        if is_login_wall:
            print(f"  LOGIN WALL in {html_file.name} -- phone's cookie had expired at fetch time.", file=sys.stderr)
            login_wall_teams.append(team_id)
            continue

        if not players:
            print(f"  WARNING: 0 players parsed from {html_file.name} -- check page structure.", file=sys.stderr)

        result["teams"][team_id] = {
            "team_name": team_name,
            "player_count": len(players),
            "keeper_count": sum(1 for p in players if p["is_keeper"]),
            "players": players,
        }
        print(f"  Parsed team {team_id} ({team_name}): {len(players)} players, "
              f"{sum(1 for p in players if p['is_keeper'])} keepers", file=sys.stderr)

    if login_wall_teams:
        result["login_wall_teams"] = login_wall_teams

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    total_players = sum(t["player_count"] for t in result["teams"].values())
    total_keepers = sum(t["keeper_count"] for t in result["teams"].values())
    print(
        f"\nDone. {len(result['teams'])} team(s) parsed, {total_players} players, "
        f"{total_keepers} keepers. Wrote {args.out}",
        file=sys.stderr,
    )
    if login_wall_teams:
        print(f"{len(login_wall_teams)} team(s) hit a login wall -- phone cookie needs refreshing.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
