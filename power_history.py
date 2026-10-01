#!/usr/bin/env python3
"""
power_history.py -- weekly snapshots of the league power rankings.

Writes power_rankings_history.json, which the Manager Cockpit's "Power rankings
over time" chart reads. Called at the end of build_inseason.py, so it runs
wherever inseason_<year>.json is rebuilt (Tuesday's scheduled run, and every
phone-scraper run) with no extra workflow step.

GRANULARITY: ONE SNAPSHOT PER NFL WEEK, PER LEAGUE.
Entries are keyed by week number. A re-run in the same week REPLACES that week's
entry rather than adding a new one, so however often the data refreshes there is
never more than one point per week, and the point that survives is the last
refresh of the week (rosters as they stood going into the games).

THE NUMBERS MATCH THE PAGE. The two boards on each league page are computed in
index.html (mgrLeagueRankings). This is a line-for-line port, so a snapshot and
the live board can't disagree:
  surplus   = sum over EVERY starting slot of (team's pts/wk at the slot
              minus the league median at that slot), rounded to 0.1
  benchSum  = pts/wk of the top 3 FLEX-eligible (RB/WR/TE) bench players,
              rounded to 0.1. QBs/K/DEF excluded; "bench" = rostered, projected,
              and not in the optimal starting lineup.
  rank      = position in the descending sort of each metric, 1 = best. Ties
              keep roster order, as JS's stable sort does on the page.

NO HISTORY CAN BE BACKFILLED: a past week's rosters aren't recoverable, so a
league's history starts the first week this runs and grows from there.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

HISTORY_FILE = "power_rankings_history.json"
FLEX_ELIGIBLE = ("RB", "WR", "TE")
BENCH_COUNT = 3


def js_round1(x):
    """Math.round(x*10)/10 -- JS rounds halves toward +infinity, Python's
    round() rounds to even, and the page's numbers are the reference."""
    return math.floor(x * 10 + 0.5) / 10


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def league_rows(lg):
    """[{team_id, name, is_me, surplus, bench}] for one league, in roster order."""
    teams = lg.get("teams") or []
    slots = [r["slot"] for r in ((teams[0] if teams else {}).get("positionStrength") or [])]
    medians = {}
    for slot in slots:
        vals = []
        for t in teams:
            row = next((r for r in (t.get("positionStrength") or []) if r.get("slot") == slot), None)
            if row and row.get("perWeek") is not None:
                vals.append(row["perWeek"])
        if vals:
            medians[slot] = _median(vals)

    out = []
    for t in teams:
        surplus = 0.0
        for r in (t.get("positionStrength") or []):
            med = medians.get(r.get("slot"))
            if med is None or r.get("perWeek") is None:
                continue
            surplus += r["perWeek"] - med

        starters = {s.get("player") for s in (t.get("startingLineup") or []) if s.get("player")}
        bench = [p for p in (t.get("roster") or [])
                 if p.get("effective_per_week") is not None and p.get("name") not in starters]
        bench.sort(key=lambda p: p["effective_per_week"], reverse=True)
        top = [p for p in bench if p.get("position") in FLEX_ELIGIBLE][:BENCH_COUNT]

        out.append({
            "team_id": str(t.get("team_id")),
            "name": t.get("team_name") or f"Team {t.get('team_id')}",
            "is_me": bool(t.get("is_me")),
            "surplus": js_round1(surplus),
            "bench": js_round1(sum(p["effective_per_week"] for p in top)),
        })
    return out


def _ranks(rows, key):
    order = sorted(rows, key=lambda r: r[key], reverse=True)   # stable
    return {r["team_id"]: i + 1 for i, r in enumerate(order)}


def snapshot_league(lg):
    """One week's entry for a league, or None if there's nothing worth keeping."""
    rows = league_rows(lg)
    if not rows or not any((t.get("positionStrength") or []) for t in (lg.get("teams") or [])):
        return None
    s_rank, b_rank = _ranks(rows, "surplus"), _ranks(rows, "bench")
    return {
        "of": len(rows),
        "teams": {
            r["team_id"]: {
                "name": r["name"],
                "isMe": r["is_me"],
                "surplus": r["surplus"],
                "bench": r["bench"],
                "starterRank": s_rank[r["team_id"]],
                "benchRank": b_rank[r["team_id"]],
            } for r in rows
        },
    }


def update_history(doc, path=HISTORY_FILE):
    """Fold this build's league states into the history file. Returns a list of
    (league, week) pairs written. Never raises on bad input -- a history
    problem must not cost the build -- but also never overwrites a history file
    it couldn't read, since that file is the only copy of weeks that can't be
    recomputed."""
    hist = {"version": 1, "leagues": {}}
    if os.path.exists(path):
        try:
            with open(path) as f:
                hist = json.load(f)
            if not isinstance(hist.get("leagues"), dict):
                raise ValueError("no 'leagues' object")
        except Exception as e:
            print(f"  ::warning::power history: {path} unreadable ({e}) -- NOT overwriting it. "
                  f"Fix or restore the file; snapshot skipped.")
            return []

    written = []
    now = datetime.now(timezone.utc).isoformat()
    for key, lg in (doc.get("leagues") or {}).items():
        week = lg.get("currentWeek") or doc.get("currentWeek")
        if not week:
            print(f"  power history: {key}: no current week in the data -- skipped.")
            continue
        snap = snapshot_league(lg)
        if snap is None:
            print(f"  power history: {key}: no positional-strength data -- skipped.")
            continue
        snap["capturedAt"] = now
        entry = hist["leagues"].setdefault(key, {"weeks": {}})
        entry["weeks"][str(int(week))] = snap
        written.append((key, int(week)))

    if written:
        hist["updated"] = now
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        print("  power history: " + ", ".join(f"{k} wk{w}" for k, w in written))
    return written


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "inseason_2026.json"
    with open(src) as f:
        update_history(json.load(f))
