"""
yahoo_advanced_analysis.py
Advanced league analytics derived OFFLINE from data the workflows already commit:
    yahoo_season_<year>.json          (matchups, weekly lineups, trades)
    yahoo_waiver_history_<year>.json  (waiver / free-agent adds and drops; optional)

No Yahoo calls, no credentials -- it only reads those files, so it is safe to re-run any time.

Answers, per league (Kepners, Miami):
  * positions       who scores the most / least at each position, per week and season
  * projections     who beats / misses Yahoo's start-of-week team projection, and how volatile
  * decisions       points left on the bench, the single costliest benching each week,
                    and losses a better lineup would have turned into wins
  * players         each team's MVP (points above the positional average while started),
                    weakest link, and the league's top performers
  * consistency     scoring mean / spread / boom-bust, record vs the weekly median
  * pickups         points from waiver + free-agent adds, FAAB efficiency, drop regrets
  * trades          what each side got from the players it received

OUTPUT: yahoo_advanced_<year>.json
"""
import sys
import json
import statistics as st
from collections import defaultdict

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
SEASON_FILE = f"yahoo_season_{YEAR}.json"
WAIVER_FILE = f"yahoo_waiver_history_{YEAR}.json"
OUT_FILE = f"yahoo_advanced_{YEAR}.json"

FLEX_LETTERS = {"Q": "QB", "R": "RB", "W": "WR", "T": "TE"}
BENCH = {"BN", "IR", "IR+", "NA"}
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]


def r2(x):
    return None if x is None else round(x, 2)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def eligible_positions(slot):
    if "/" in slot:
        return {FLEX_LETTERS[c] for c in slot.split("/") if c in FLEX_LETTERS}
    if slot.upper() == "FLEX":
        return {"RB", "WR", "TE"}
    return {slot.upper()}


def norm(name):
    return " ".join((name or "").lower().replace(".", "").replace("'", "").split())


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
def analyze_league(key, lg, waiver):
    teams = lg["teams"]
    lineups = lg["lineups"]
    name = {tid: t["name"] for tid, t in teams.items()}
    weeks_done = sorted({int(w) for tid in lineups for w in lineups[tid]})
    out = {"league_name": lg.get("league_name"), "weeks_analyzed": weeks_done}
    if not weeks_done:
        return out

    # ---- flatten every started / benched player-week ------------------------
    starts, benches = [], []
    for tid, wk in lineups.items():
        for w, ln in wk.items():
            for p in ln["starters"]:
                starts.append({"tid": tid, "week": int(w), **p})
            for p in ln["bench"]:
                benches.append({"tid": tid, "week": int(w), **p})
    pos_avg = {pos: mean([s["pts"] for s in starts if s["pos"] == pos]) for pos in POSITIONS}

    # ---- 1. positions -------------------------------------------------------
    by_tw_pos = defaultdict(float)                         # (tid, week, pos) -> points
    for s in starts:
        by_tw_pos[(s["tid"], s["week"], s["pos"])] += s["pts"] or 0
    positions = {}
    for pos in POSITIONS:
        per_team = {}
        for tid in teams:
            weeks = [w for w in lineups.get(tid, {})]
            vals = [by_tw_pos.get((tid, int(w), pos), 0.0) for w in weeks]
            per_team[tid] = mean(vals)
        league_avg = mean(list(per_team.values()))
        if league_avg is None:
            continue
        ranked = sorted(per_team, key=lambda t: -(per_team[t] or 0))
        weekly = []
        for w in weeks_done:
            row = {tid: by_tw_pos.get((tid, w, pos), 0.0) for tid in teams if str(w) in lineups.get(tid, {})}
            if not row:
                continue
            hi, lo = max(row, key=row.get), min(row, key=row.get)
            weekly.append({"week": w, "best": {"team": name[hi], "pts": r2(row[hi])},
                           "worst": {"team": name[lo], "pts": r2(row[lo])}})
        positions[pos] = {
            "league_avg_per_week": r2(league_avg),
            "teams": [{"team_id": tid, "team": name[tid], "rank": i + 1,
                       "avg_per_week": r2(per_team[tid]),
                       "vs_league": r2((per_team[tid] or 0) - league_avg),
                       "weeks_leading": sum(1 for x in weekly if x["best"]["team"] == name[tid])}
                      for i, tid in enumerate(ranked)],
            "weekly": weekly,
        }
    out["positions"] = positions

    # ---- 2. projections (Yahoo's team projection, as pulled) ---------------------
    proj = {tid: [] for tid in teams}
    for w, matchups in lg["weeks"].items():
        for m in matchups:
            if m["status"] != "postevent":
                continue
            for t in m["teams"]:
                if t["team_id"] in proj and t["points"] is not None and t["projected"]:
                    proj[t["team_id"]].append({"week": int(w), "actual": t["points"],
                                               "projected": t["projected"],
                                               "diff": round(t["points"] - t["projected"], 2)})
    projections = []
    for tid, rows in proj.items():
        if not rows:
            continue
        diffs = [r["diff"] for r in rows]
        projections.append({
            "team_id": tid, "team": name[tid], "games": len(rows),
            "avg_diff": r2(mean(diffs)), "stdev": r2(st.pstdev(diffs)) if len(diffs) > 1 else None,
            "weeks_over": sum(1 for d in diffs if d > 0), "weeks_under": sum(1 for d in diffs if d < 0),
            "best_week": max(rows, key=lambda r: r["diff"]), "worst_week": min(rows, key=lambda r: r["diff"]),
            "weekly": rows,
        })
    projections.sort(key=lambda r: -(r["avg_diff"] or 0))
    out["projections"] = projections

    # ---- 3. decisions -----------------------------------------------------------
    decisions, worst_calls = [], []
    for tid, wk in lineups.items():
        scores = {s["week"]: s for s in teams[tid].get("derived", {}).get("scores", [])}
        left, avoidable, big = [], [], 0
        for w, ln in sorted(wk.items(), key=lambda kv: int(kv[0])):
            pl = ln.get("points_left")
            if pl is not None:
                left.append(pl)
                big += pl >= 15
            # costliest single swap: a benched player replacing a starter his slot allows
            best = None
            for b in ln["bench"]:
                for s in ln["starters"]:
                    if b["pos"] in eligible_positions(s["slot"]):
                        gain = (b["pts"] or 0) - (s["pts"] or 0)
                        if best is None or gain > best["gain"]:
                            best = {"team": name[tid], "week": int(w), "bench": b["name"], "bench_pts": b["pts"],
                                    "starter": s["name"], "starter_pts": s["pts"], "gain": r2(gain)}
            if best and best["gain"] > 0:
                worst_calls.append(best)
            sc = scores.get(int(w))
            if sc and ln.get("optimal_points") is not None and sc["pts"] < sc["opp_pts"] < ln["optimal_points"]:
                avoidable.append({"week": int(w), "scored": sc["pts"], "opp": sc["opp_pts"],
                                  "optimal": ln["optimal_points"], "opp_team": name.get(sc["opp"])})
        if left:
            decisions.append({
                "team_id": tid, "team": name[tid], "weeks": len(left),
                "total_points_left": r2(sum(left)), "avg_per_week": r2(mean(left)),
                "weeks_15_plus_left": big,
                "efficiency": (teams[tid].get("lineup_season") or {}).get("efficiency"),
                "avoidable_losses": avoidable,
            })
    decisions.sort(key=lambda d: -d["total_points_left"])
    worst_calls.sort(key=lambda c: -c["gain"])
    out["decisions"] = {"by_team": decisions, "costliest_calls": worst_calls[:15]}

    # ---- 4. players: MVP / weakest link ------------------------------------------
    agg = defaultdict(lambda: {"starts": 0, "pts": 0.0, "above": 0.0, "pos": None})
    for s in starts:
        a = agg[(s["tid"], s["name"])]
        a["starts"] += 1
        a["pts"] += s["pts"] or 0
        a["above"] += (s["pts"] or 0) - (pos_avg.get(s["pos"]) or 0)
        a["pos"] = s["pos"]
    rows = [{"team_id": t, "team": name[t], "player": n, "pos": a["pos"], "starts": a["starts"],
             "points": r2(a["pts"]), "above_pos_avg": r2(a["above"]), "ppg": r2(a["pts"] / a["starts"])}
            for (t, n), a in agg.items()]
    mvps = []
    for tid in teams:
        mine = [r for r in rows if r["team_id"] == tid]
        if not mine:
            continue
        mvps.append({"team_id": tid, "team": name[tid],
                     "mvp": max(mine, key=lambda r: r["above_pos_avg"]),
                     "weakest_link": min((r for r in mine if r["starts"] >= 2),
                                         key=lambda r: r["above_pos_avg"], default=None)})
    out["players"] = {
        "positional_average_per_start": {k: r2(v) for k, v in pos_avg.items() if v is not None},
        "team_mvps": mvps,
        "league_top": sorted(rows, key=lambda r: -r["above_pos_avg"])[:15],
    }

    # ---- 5. consistency + median wins -------------------------------------------
    cons, week_scores = [], defaultdict(dict)
    for tid, t in teams.items():
        for s in t.get("derived", {}).get("scores", []):
            week_scores[s["week"]][tid] = s["pts"]
    medians = {w: st.median(v.values()) for w, v in week_scores.items() if len(v) > 2}
    for tid, t in teams.items():
        sc = [s["pts"] for s in t.get("derived", {}).get("scores", [])]
        if not sc:
            continue
        above = sum(1 for w, v in week_scores.items() if tid in v and w in medians and v[tid] > medians[w])
        n = sum(1 for w, v in week_scores.items() if tid in v and w in medians)
        cons.append({"team_id": tid, "team": name[tid], "avg": r2(mean(sc)),
                     "stdev": r2(st.pstdev(sc)) if len(sc) > 1 else None,
                     "high": max(sc), "low": min(sc), "above_median_weeks": f"{above}-{n - above}"})
    cons.sort(key=lambda c: -(c["avg"] or 0))
    out["consistency"] = cons

    # ---- 6. pickups + drop regrets -----------------------------------------------
    pickups = {"complete_history": None, "by_team": [], "top_pickups": [], "top_drop_regrets": []}
    if waiver:
        adds = []
        pickups["complete_history"] = waiver.get("complete_history")
        for c in (waiver.get("claims") or []):
            if c.get("won") and c.get("add"):
                adds.append(c)
        adds.extend(waiver.get("free_agent_adds") or [])
        pts_by = defaultdict(list)                          # (tid, norm name) -> [(week, pts)]
        any_team = defaultdict(list)                        # norm name -> [(tid, week, pts)]
        for s in starts:
            pts_by[(s["tid"], norm(s["name"]))].append((s["week"], s["pts"] or 0))
            any_team[norm(s["name"])].append((s["tid"], s["week"], s["pts"] or 0))
        per_team = defaultdict(lambda: {"pts": 0.0, "faab": 0.0, "n": 0})
        results, regrets = [], []
        for a in adds:
            tid = str(a.get("team_id"))
            if tid not in teams:
                continue
            wk = a.get("week") or 0
            nm = norm((a.get("add") or {}).get("name"))
            got = [p for (w, p) in pts_by.get((tid, nm), []) if w >= wk]
            row = {"team": name[tid], "player": (a["add"] or {}).get("name"), "week": wk,
                   "bid": a.get("bid") or 0, "type": a.get("type"), "starts": len(got), "points": r2(sum(got))}
            results.append(row)
            per_team[tid]["pts"] += sum(got)
            per_team[tid]["faab"] += row["bid"]
            per_team[tid]["n"] += 1
            d = a.get("drop")
            if d:
                dn = norm(d.get("name"))
                after = [p for (t, w, p) in any_team.get(dn, []) if t != tid and w >= wk]
                if after:
                    regrets.append({"team": name[tid], "dropped": d.get("name"), "week": wk,
                                    "points_for_others": r2(sum(after)), "starts_elsewhere": len(after)})
        pickups["by_team"] = sorted(
            [{"team_id": t, "team": name[t], "adds": v["n"], "points_from_adds": r2(v["pts"]),
              "faab_spent": r2(v["faab"]),
              "points_per_faab_dollar": r2(v["pts"] / v["faab"]) if v["faab"] else None}
             for t, v in per_team.items()], key=lambda r: -r["points_from_adds"])
        pickups["top_pickups"] = sorted(results, key=lambda r: -r["points"])[:12]
        pickups["top_drop_regrets"] = sorted(regrets, key=lambda r: -r["points_for_others"])[:10]
    out["pickups"] = pickups

    # ---- 7. trades ------------------------------------------------------------------
    scorecard = []
    for tr in lg.get("trades") or []:
        if tr.get("status") != "successful":
            continue
        for mv in tr["moves"]:
            nm = norm(mv["player"])
            new = [p for (w, p) in [(s["week"], s["pts"] or 0) for s in starts
                                    if s["tid"] == mv["to_team_id"] and norm(s["name"]) == nm]]
            old = [(s["pts"] or 0) for s in starts if s["tid"] == mv["from_team_id"] and norm(s["name"]) == nm]
            scorecard.append({"date": tr["date"], "player": mv["player"], "from": mv["from_team"], "to": mv["to_team"],
                              "points_started_for_new_team": r2(sum(new)), "starts_for_new_team": len(new),
                              "points_started_for_old_team": r2(sum(old)), "starts_for_old_team": len(old)})
    out["trades"] = scorecard
    return out


def main():
    season = load(SEASON_FILE)
    if not season:
        raise SystemExit(f"{SEASON_FILE} not found -- run Update Yahoo Season Detail first.")
    waiver = load(WAIVER_FILE) or {}
    result = {"year": YEAR, "generated_from": season.get("generated"), "leagues": {}}
    for key, lg in season["leagues"].items():
        print(f"[{key}] analyzing...")
        result["leagues"][key] = analyze_league(key, lg, (waiver.get("leagues") or {}).get(key))
    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=1)
    print(f"Wrote {OUT_FILE}.")


if __name__ == "__main__":
    main()
