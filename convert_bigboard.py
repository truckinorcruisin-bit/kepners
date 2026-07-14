#!/usr/bin/env python3
"""
convert_bigboard.py
Converts the FY Big Board Excel into the JSON the Mission Control draft app consumes.

Usage:
    python convert_bigboard.py FY26_BigBoard.xlsx data/bigboard.json

Output schema:
{
  "meta": { "season": 2026, "generated": "..." },
  "players": [
     { "id", "name", "team", "pos", "seanPosRank", "tier", "tierGroup",
       "targetRound", "like", "avgRank", "avgRound", "myDiff",
       "platform": {"yahoo":..,"espn":..,"underdog":..,...}, "notes" }
  ],
  "leagues": {
     "kepners": { "platform":"yahoo", "myTeam":"Pickups", "teams":[...], "rosterSlots":[...],
                  "keepers":[...], "rules": {see league_rules.json} },
     "miami":   { "platform":"yahoo", "myTeam":"HannahLees", ... },
     "zimmer":  { "platform":"espn",  "myTeam":"Elements of Intrigue", ... }
  }
}

League format/keeper rules live in league_rules.json (hand-maintained, not
derived from the spreadsheet) and are merged into each league's output here.
Edit that file directly to update rules; this script only merges it in.
"""
import sys, os, json, re
from datetime import datetime, timezone
import openpyxl

RULES_FILE = "league_rules.json"  # hand-maintained; sits alongside this script


def load_league_rules():
    """Static format/keeper-rule config, hand-maintained in league_rules.json.
    Merged into each league's output under the "rules" key. Missing file is
    non-fatal -- the app just won't have rule metadata until it's added."""
    if not os.path.exists(RULES_FILE):
        print(f"Note: {RULES_FILE} not found -- leagues will have no 'rules' section.")
        return {}
    with open(RULES_FILE) as f:
        rules = json.load(f)
    rules.pop("_comment", None)
    return rules


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip() if s is not None else None


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def tier_group(tier):
    """Collapse the verbose tier string into a coarse actionable band."""
    if not tier or tier == "#N/A":
        return "unranked"
    t = tier.lower()
    if "don't draft" in t or "don\u2019t draft" in t or "streamer" in t or "replacement" in t:
        return "avoid"
    if "handcuff" in t:
        return "handcuff"
    if "late round" in t or "very late" in t:
        return "late-flier"
    m = re.match(r"([A-Z]+)\d", tier)
    return m.group(1).lower() if m else "ranked"


def read_players(wb):
    ws = wb["2025 Big Board"]  # rename tab per season; keep logic identical
    # Column map (1-indexed) confirmed from header row 6
    COL = dict(targetRound=4, like=6, player=7, team=8, pos=9, tier=10,
               seanPosRank=11, yahoo=13, underdog=14, cbs=16, espn=17,
               ffpc=18, sleeper=19, nfl=20, avgRank=21, avgRound=22,
               myDiff=23, notes=25)
    players = []
    for r in range(7, ws.max_row + 1):
        name = norm(ws.cell(r, COL["player"]).value)
        if not name:
            continue
        pos = norm(ws.cell(r, COL["pos"]).value)
        if pos in (None, "#N/A"):
            pos = "NA"
        tier = norm(ws.cell(r, COL["tier"]).value)
        players.append({
            "id": slug(name),
            "name": name,
            "team": norm(ws.cell(r, COL["team"]).value),
            "pos": pos,
            "seanPosRank": ws.cell(r, COL["seanPosRank"]).value,
            "tier": tier,
            "tierGroup": tier_group(tier),
            "targetRound": ws.cell(r, COL["targetRound"]).value,
            "like": norm(ws.cell(r, COL["like"]).value),
            "avgRank": ws.cell(r, COL["avgRank"]).value,
            "avgRound": ws.cell(r, COL["avgRound"]).value,
            "myDiff": ws.cell(r, COL["myDiff"]).value,
            "platform": {
                "yahoo": ws.cell(r, COL["yahoo"]).value,
                "espn": ws.cell(r, COL["espn"]).value,
                "underdog": ws.cell(r, COL["underdog"]).value,
                "cbs": ws.cell(r, COL["cbs"]).value,
                "ffpc": ws.cell(r, COL["ffpc"]).value,
                "sleeper": ws.cell(r, COL["sleeper"]).value,
                "nfl": ws.cell(r, COL["nfl"]).value,
            },
            "notes": norm(ws.cell(r, COL["notes"]).value),
        })
    return players


def read_team_sheet(wb, sheet, platform, my_team):
    """Parse a *Team* sheet: managers, draft slots, roster template, keepers."""
    ws = wb[sheet]
    # Team names live on row 3 starting col E; managers on row 4
    teams = []
    col = 5
    while True:
        tname = norm(ws.cell(3, col).value)
        if not tname:
            break
        teams.append({
            "slot": col - 4,
            "team": tname,
            "manager": norm(ws.cell(4, col).value),
            "isMe": my_team.lower() in (tname or "").lower(),
        })
        col += 1

    # Roster template: col D rows 5+ ('QB','RB',...)
    roster_slots = []
    r = 5
    while True:
        slot = norm(ws.cell(r, 4).value)
        if not slot:
            break
        roster_slots.append(slot)
        r += 1

    # Keepers: cells in the team grid formatted "Player (round)"
    keepers = []
    for tr in range(5, 5 + len(roster_slots)):
        for tc in range(5, 5 + len(teams)):
            cell = norm(ws.cell(tr, tc).value)
            if not cell:
                continue
            m = re.match(r"(.+?)\s*\((\d+)\)\s*$", cell)
            if m:
                keepers.append({
                    "team": teams[tc - 5]["team"],
                    "player": norm(m.group(1)),
                    "playerId": slug(m.group(1)),
                    "round": int(m.group(2)),
                })
    return {
        "platform": platform,
        "myTeam": my_team,
        "teams": teams,
        "rosterSlots": roster_slots,
        "keepers": keepers,
    }


def main(src, dst):
    wb = openpyxl.load_workbook(src, data_only=True)
    rules = load_league_rules()
    out = {
        "meta": {"season": 2026, "generated": datetime.now(timezone.utc).isoformat()},
        "players": read_players(wb),
        "leagues": {
            "kepners": read_team_sheet(wb, "Kepners Team", "yahoo", "Pickups"),
            "miami": read_team_sheet(wb, "Miami Team", "yahoo", "HannahLees"),
            # Zimmer (ESPN) has no dedicated Team sheet in FY25 — stub for now,
            # will populate once the FY26 workbook adds it.
            "zimmer": {"platform": "espn", "myTeam": "Elements of Intrigue",
                       "teams": [], "rosterSlots": [], "keepers": []},
        },
    }
    for league_key, league_data in out["leagues"].items():
        if league_key in rules:
            league_data["rules"] = rules[league_key]

    with open(dst, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {dst}: {len(out['players'])} players; "
          f"leagues={list(out['leagues'])}")
    for k, v in out["leagues"].items():
        has_rules = "rules" in v
        print(f"  {k}: {len(v['teams'])} teams, {len(v['keepers'])} keepers, "
              f"roster={v['rosterSlots']}, rules_loaded={has_rules}")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/FY25_BigBoard.xlsx"
    dst = sys.argv[2] if len(sys.argv) > 2 else "bigboard.json"
    main(src, dst)
