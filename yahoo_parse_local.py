#!/usr/bin/env python3
"""
yahoo_parse_local.py — Parses committed Yahoo roster HTML files into JSON.

Second half of the "phone bridge" pipeline: an iPhone Shortcut fetches each team's
roster page (from the phone's own trusted session) and pushes the raw HTML into
yahoo_roster_raw/team_<id>.html. This script runs in GitHub Actions on that push
and parses whatever is there into one JSON file. No network or cookie needed.

USAGE
-----
    python yahoo_parse_local.py --html-dir yahoo_roster_raw --out kepners_live_rosters.json
    python yahoo_parse_local.py ... --strict      # exit 1 if ANY team has a problem

PAGE CLASSIFICATION (v2)
------------------------
Every file is classified before parsing, by POSITIVE evidence first:

  roster        has a statTable*-wrap roster table            -> parsed
  login_wall    has a real sign-in form (login-username etc.)  -> flagged
  wrong_page    signed-in Yahoo page that is NOT a roster      -> flagged, says which
                (e.g. the 'Managers' page, URL ending /teams)
  unknown       none of the above                              -> flagged

v1 treated any file containing the text "login.yahoo.com" as a login wall. That
string appears in the header of EVERY signed-in Yahoo page (account/logout links),
so every file was misreported as an expired cookie.

Exit code: 0 if at least one team parsed (partial results are still written, and
problems are listed under "problems" in the JSON); 1 if nothing parsed. --strict
makes any problem a failure.
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
    print("ERROR: requires 'beautifulsoup4'. Install with: pip install beautifulsoup4", file=sys.stderr)
    raise

# Only ever present on Yahoo's actual sign-in page, never on a signed-in page.
LOGIN_FORM_MARKERS = (
    'id="login-username"',
    'id="login-signin"',
    'name="username" id="login-username"',
)
LOGIN_TITLE_RE = re.compile(r"^\s*(sign in|log in|yahoo\s*\|\s*login)", re.I)

ROSTER_WRAP_IDS = ("statTable0-wrap", "statTable1-wrap", "statTable2-wrap")
TEAM_ID_RE = re.compile(r"team_(\d+)\.html$")

# Statuses
ROSTER, LOGIN_WALL, WRONG_PAGE, UNKNOWN = "roster", "login_wall", "wrong_page", "unknown"


def page_title(soup):
    t = soup.find("title")
    return t.get_text(strip=True) if t and t.get_text() else ""


def canonical_url(soup):
    link = soup.find("link", rel="canonical")
    return link.get("href", "") if link else ""


def classify_page(html, soup):
    """Returns (status, detail). Positive evidence of a roster wins over everything."""
    if any(soup.find(id=w) for w in ROSTER_WRAP_IDS):
        return ROSTER, ""

    title = page_title(soup)
    if any(m in html for m in LOGIN_FORM_MARKERS) or LOGIN_TITLE_RE.search(title):
        return LOGIN_WALL, "sign-in form present -- phone cookie expired at fetch time"

    canon = canonical_url(soup)
    if "yahoo" in html.lower() and (title or canon):
        hint = ""
        if canon.rstrip("/").endswith("/teams"):
            hint = (" URL ends in /teams (the Managers page). The Shortcut should fetch "
                    ".../f1/<league_id>/<team_id>/team  (singular).")
        return WRONG_PAGE, f"signed in, but not a roster page (title: '{title}', canonical: '{canon}').{hint}"

    return UNKNOWN, f"unrecognized page (title: '{title}')"


def parse_team_name(soup):
    """Best-effort team name from <title> ('Kepners Keepers - The Pickups | ...')."""
    text = page_title(soup)
    if not text:
        return None
    if " - " in text:
        return text.split(" - ", 1)[1].split("|")[0].strip()
    return text


def parse_roster_row(row):
    """One player's data from a roster <tr>. None for rows with no player."""
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
    """Returns (status, detail, team_name, players) for one team's HTML."""
    soup = BeautifulSoup(html, "html.parser")
    status, detail = classify_page(html, soup)
    if status != ROSTER:
        return status, detail, None, []

    players, seen_ids = [], set()
    for wrap_id in ROSTER_WRAP_IDS:
        wrap = soup.find(id=wrap_id)
        if not wrap:
            continue
        for row in wrap.select("tbody tr"):
            player = parse_roster_row(row)
            if player and player["player_id"] not in seen_ids:
                seen_ids.add(player["player_id"])
                players.append(player)

    detail = "" if players else "roster table found but 0 players parsed -- check page structure"
    return ROSTER, detail, parse_team_name(soup), players


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html-dir", required=True, help="Directory containing team_<id>.html files")
    ap.add_argument("--out", required=True, help="Output JSON path")
    ap.add_argument("--strict", action="store_true", help="Exit 1 if ANY team has a problem")
    ap.add_argument("--fetched-at", help="ISO timestamp of when the HTML was scraped (the commit time of "
                    "the newest file). Defaults to now. Pass it when several leagues share one workflow so "
                    "re-parsing league A doesn't make league B's old data look freshly scraped.")
    args = ap.parse_args()

    html_dir = Path(args.html_dir)
    if not html_dir.is_dir():
        print(f"ERROR: {html_dir} is not a directory (nothing pushed yet?)", file=sys.stderr)
        sys.exit(1)

    result = {"fetched_at": args.fetched_at or datetime.now(timezone.utc).isoformat(), "teams": {}, "problems": {}}

    html_files = sorted(html_dir.glob("team_*.html"), key=lambda p: int(TEAM_ID_RE.search(p.name).group(1))
                        if TEAM_ID_RE.search(p.name) else 10**6)
    if not html_files:
        print(f"WARNING: no team_*.html files found in {html_dir}", file=sys.stderr)

    for html_file in html_files:
        m = TEAM_ID_RE.search(html_file.name)
        if not m:
            print(f"  Skipping {html_file.name} -- doesn't match team_<id>.html", file=sys.stderr)
            continue
        team_id = m.group(1)

        html = html_file.read_text(encoding="utf-8", errors="replace")
        status, detail, team_name, players = parse_roster_html(html)

        if status != ROSTER:
            label = {LOGIN_WALL: "LOGIN WALL", WRONG_PAGE: "WRONG PAGE"}.get(status, "UNKNOWN PAGE")
            print(f"  {label} in {html_file.name}: {detail}", file=sys.stderr)
            result["problems"][team_id] = {"status": status, "detail": detail}
            continue

        if not players:
            print(f"  WARNING: {html_file.name}: {detail}", file=sys.stderr)
            result["problems"][team_id] = {"status": "empty_roster", "detail": detail}
            continue

        keepers = sum(1 for p in players if p["is_keeper"])
        result["teams"][team_id] = {
            "team_name": team_name,
            "player_count": len(players),
            "keeper_count": keepers,
            "players": players,
        }
        print(f"  Parsed team {team_id} ({team_name}): {len(players)} players, {keepers} keepers", file=sys.stderr)

    # Back-compat with v1 output
    login_walls = [t for t, p in result["problems"].items() if p["status"] == LOGIN_WALL]
    if login_walls:
        result["login_wall_teams"] = login_walls

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    n_ok = len(result["teams"])
    n_bad = len(result["problems"])
    print(
        f"\nDone. {n_ok} team(s) parsed, "
        f"{sum(t['player_count'] for t in result['teams'].values())} players, "
        f"{sum(t['keeper_count'] for t in result['teams'].values())} keepers. "
        f"{n_bad} problem(s). Wrote {args.out}",
        file=sys.stderr,
    )
    if n_bad:
        by_status = {}
        for p in result["problems"].values():
            by_status[p["status"]] = by_status.get(p["status"], 0) + 1
        print("Problem summary: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())), file=sys.stderr)

    if n_ok == 0 or (args.strict and n_bad):
        sys.exit(1)


if __name__ == "__main__":
    main()
