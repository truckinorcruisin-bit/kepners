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
from itertools import combinations
from collections import defaultdict

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
SEASON_FILE = f"yahoo_season_{YEAR}.json"
WAIVER_FILE = f"yahoo_waiver_history_{YEAR}.json"
OUT_FILE = f"yahoo_advanced_{YEAR}.json"
INSEASON_FILE = f"inseason_{YEAR}.json"
ESPN_STATS_FILE = f"espn_season_stats_{YEAR}.json"
BIGBOARD_FILE = "bigboard.json"

FLEX_LETTERS = {"Q": "QB", "R": "RB", "W": "WR", "T": "TE"}
BENCH = {"BN", "BEN", "IR", "IR+", "NA"}
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

    # ---- 8. extras ---------------------------------------------------------------
    inseason = ((load(INSEASON_FILE) or {}).get("leagues") or {}).get(key) or {}
    out["mvp_detail"] = mvp_detail(starts, lineups, teams, name, pos_avg)
    if inseason:
        out["keepers"] = keeper_report(key, teams, starts, pos_avg, inseason, espn_points())
        out["trade_ideas"] = trade_ideas(inseason)
    out["draft_value"] = draft_value(key, lg, teams, name, espn_points())
    if waiver:
        adds = [c for c in (waiver.get("claims") or []) if c.get("won") and c.get("add")]
        adds += waiver.get("free_agent_adds") or []
        out["faab"] = {"budget_assumed": waiver.get("budget_assumed"),
                       "complete_history": waiver.get("complete_history"),
                       "only_executed_claims_visible": True,
                       "by_team": faab_activity(adds, teams, name, starts, pos_avg, waiver.get("budget"))}
    return out


# ---------------------------------------------------------------------------
# Extras: MVP detail, keepers, bid activity, trade ideas, draft busts
# ---------------------------------------------------------------------------
def espn_points():
    """name -> season points (ESPN standard scoring, the project's proxy for Yahoo)."""
    d = load(ESPN_STATS_FILE) or {}
    out = {}
    for pos, rows in (d.get("players_by_position") or {}).items():
        for r in rows:
            out[norm(r["name"])] = {"pts": r.get("total_points"), "pos": "DEF" if pos == "D/ST" else pos}
    return out


def lineup_value(players, slots):
    """Best starting-lineup ROS points per week for a set of {pos, v} players."""
    starters = [x for x in slots if x not in BENCH]
    pool = sorted(players, key=lambda p: -p["v"])
    used, total = set(), 0.0
    for slot in sorted(starters, key=lambda x: len(eligible_positions(x))):
        elig = eligible_positions(slot)
        for i, p in enumerate(pool):
            if i not in used and p["pos"] in elig:
                used.add(i)
                total += p["v"]
                break
    return total


def mvp_detail(starts, lineups, teams, name, pos_avg):
    """Quantify each team's MVP: points above the positional average per start, and share of
    the team's starter points."""
    agg = defaultdict(lambda: {"starts": 0, "pts": 0.0, "above": 0.0, "pos": None})
    for s in starts:
        a = agg[(s["tid"], s["name"])]
        a["starts"] += 1
        a["pts"] += s["pts"] or 0
        a["above"] += (s["pts"] or 0) - (pos_avg.get(s["pos"]) or 0)
        a["pos"] = s["pos"]
    res = []
    for tid in teams:
        mine = [(n, a) for (t, n), a in agg.items() if t == tid]
        if not mine:
            continue
        n, a = max(mine, key=lambda x: x[1]["above"])
        team_pts = sum(x["starter_points"] for x in lineups[tid].values())
        res.append({"team_id": tid, "team": name[tid], "player": n, "pos": a["pos"], "starts": a["starts"],
                    "points": r2(a["pts"]), "ppg": r2(a["pts"] / a["starts"]),
                    "positional_avg_per_start": r2(pos_avg.get(a["pos"])),
                    "above_avg_total": r2(a["above"]), "above_avg_per_start": r2(a["above"] / a["starts"]),
                    "pct_above_avg": r2(100 * a["above"] / (a["starts"] * pos_avg[a["pos"]])) if pos_avg.get(a["pos"]) else None,
                    "share_of_team_points": r2(100 * a["pts"] / team_pts) if team_pts else None})
    return res


def keeper_report(key, teams, starts, pos_avg, inseason, espn):
    live = load(f"{key}_live_rosters.json")
    order = load(f"{key}_draft_order.json")
    if not live or not order or not isinstance(order.get("draft_order"), list):
        return None
    cost = {}
    for r in order["draft_order"]:
        for i in (1, 2):
            if r.get(f"keeper{i}_name"):
                cost[norm(r[f"keeper{i}_name"])] = r.get(f"keeper{i}_round")
    bb = {norm(p["name"]): p for p in (load(BIGBOARD_FILE) or {}).get("players", [])}
    ros = {}
    for t in inseason.get("teams", []):
        for p in t["roster"]:
            if p.get("effective_per_week") is not None:
                ros[norm(p["name"])] = (p["position"], p["effective_per_week"])
    rank = {}
    for pos in {v[0] for v in ros.values()}:
        ordered = sorted((n for n, v in ros.items() if v[0] == pos), key=lambda n: -ros[n][1])
        rank.update({n: (i + 1, len(ordered)) for i, n in enumerate(ordered)})
    agg = defaultdict(lambda: {"starts": 0, "above": 0.0})
    for s in starts:
        a = agg[(s["tid"], norm(s["name"]))]
        a["starts"] += 1
        a["above"] += (s["pts"] or 0) - (pos_avg.get(s["pos"]) or 0)
    out = []
    for tid, t in live["teams"].items():
        if tid not in teams:
            continue
        ks = []
        for p in t["players"]:
            if not p.get("is_keeper"):
                continue
            n = norm(p["name"])
            a = agg.get((tid, n))
            cr = cost.get(n)
            adp = (bb.get(n) or {}).get("avgRound")
            rk = rank.get(n)
            ks.append({"player": p["name"], "pos": p["position"], "cost_round": cr, "adp_round": adp,
                       "season_points": (espn.get(n) or {}).get("pts"),
                       "starts": a["starts"] if a else 0,
                       "above_avg_per_start": r2(a["above"] / a["starts"]) if a and a["starts"] else None,
                       "ros_per_week": (ros.get(n) or (None, None))[1],
                       "ros_pos_rank": f"{rk[0]} of {rk[1]}" if rk else None})
        out.append({"team_id": tid, "team": teams[tid]["name"], "keepers": ks})
    return out


def faab_activity(adds, teams, name, starts, pos_avg, budget):
    """Per-team claim/add activity. Yahoo only exposes EXECUTED claims, so 'bids' here are
    winning bids; losing bids are invisible to the API."""
    per = {tid: {"won": 0, "faab": 0, "free_adds": 0, "big": []} for tid in teams}
    startrows = defaultdict(list)
    for s in starts:
        startrows[(s["tid"], norm(s["name"]))].append(s)
    for a in adds:
        tid = str(a.get("team_id"))
        if tid not in per:
            continue
        bid = a.get("bid") or 0
        if a.get("type") == "WAIVER":
            per[tid]["won"] += 1
            per[tid]["faab"] += bid
        else:
            per[tid]["free_adds"] += 1
        if bid >= 10:
            nm = (a.get("add") or {}).get("name")
            got = [s for s in startrows.get((tid, norm(nm)), []) if s["week"] >= (a.get("week") or 0)]
            above = sum((s["pts"] or 0) - (pos_avg.get(s["pos"]) or 0) for s in got)
            per[tid]["big"].append({"player": nm, "pos": (a.get("add") or {}).get("position"), "bid": bid,
                                    "week": a.get("week"), "starts": len(got),
                                    "points": r2(sum(s["pts"] or 0 for s in got)),
                                    "above_pos_avg": r2(above),
                                    "points_per_dollar": r2(sum(s["pts"] or 0 for s in got) / bid) if bid else None})
    return [{"team_id": tid, "team": name[tid], "waiver_bids_won": v["won"], "faab_spent": v["faab"],
             "faab_budget": budget, "free_agent_adds": v["free_adds"],
             "big_bids": sorted(v["big"], key=lambda b: -b["bid"])}
            for tid, v in sorted(per.items(), key=lambda kv: -kv[1]["won"])]


def trade_ideas(inseason, partner_label="The Pickups"):
    """Best win-win 1-for-1 / 2-for-2 trade between my team and every other team, scored by the
    change in each side's best-lineup rest-of-season points per week."""
    slots = inseason.get("rosterSlots") or []
    teams = inseason.get("teams") or []
    me = next((t for t in teams if t.get("is_me")), None)
    if not me:
        return []

    def pool(t):
        return [{"name": p["name"], "pos": p["position"], "v": p["effective_per_week"]}
                for p in t["roster"]
                if p.get("effective_per_week") is not None and p.get("lineup_slot") != "IR"]

    mine = pool(me)
    base_me = lineup_value(mine, slots)
    tradable = lambda xs: [p for p in xs if p["pos"] not in ("K", "DEF")]
    ideas = []
    for t in teams:
        if t is me:
            continue
        theirs = pool(t)
        base_t = lineup_value(theirs, slots)
        best, tier = None, None
        # Tier 1: both sides improve (mine by at least 0.1/wk). Tier 2, only if tier 1 finds nothing:
        # my lineup is unchanged (surplus-for-surplus) and only their side improves.
        for tier_name, min_mine in (("win-win", 0.1), ("pickups-neutral", 0.0)):
            for k in (1, 2):
                for give in combinations(tradable(mine), k):
                    for get in combinations(tradable(theirs), k):
                        new_me = [p for p in mine if p not in give] + list(get)
                        new_t = [p for p in theirs if p not in get] + list(give)
                        gm, gt = lineup_value(new_me, slots) - base_me, lineup_value(new_t, slots) - base_t
                        if gt < 0.15 or gm < min_mine:
                            continue
                        # rank by their gain plus half of mine; prefer the simpler deal on a near tie
                        score = gt + 0.5 * gm - (0.35 if k == 2 else 0)
                        if best is None or score > best["score"]:
                            best = {"score": score, "give": [g["name"] for g in give], "get": [g["name"] for g in get],
                                    "give_pos": [g["pos"] for g in give], "get_pos": [g["pos"] for g in get],
                                    "my_gain_per_week": r2(gm), "their_gain_per_week": r2(gt), "kind": tier_name}
            if best:
                break
        ideas.append({"team_id": t["team_id"], "team": t["team_name"],
                      "trade": ({k: v for k, v in best.items() if k != "score"} if best else None)})
    return ideas


def draft_value(key, lg, teams, name, espn):
    """Biggest bust / steal per team from the real draft results (lg['draft']). Each non-keeper pick is
    compared with same-position non-keeper picks within +/-12 overall picks (widening to +/-24 if
    thin): shortfall = peers' average season points minus his. Kicker/defense are skipped; a
    player missing from the ESPN stats list counts as 0 points (usually injured or never played)."""
    draft = lg.get("draft") or []
    if len(draft) < 50:
        return None
    order = load(f"{key}_draft_order.json") or {}
    keepers = set()
    for r in order.get("draft_order") or []:
        for i in (1, 2):
            if r.get(f"keeper{i}_name"):
                keepers.add(norm(r[f"keeper{i}_name"]))
    picks = []
    for p in draft:
        if not p.get("player") or p.get("pos") not in ("QB", "RB", "WR", "TE"):
            continue
        n = norm(p["player"])
        picks.append({**p, "n": n, "overall": p["pick"], "keeper": n in keepers,
                      "pts": (espn.get(n) or {}).get("pts") or 0.0})
    pool = [p for p in picks if not p["keeper"]]
    for p in pool:
        for width in (12, 24):
            peers = [q["pts"] for q in pool if q is not p and q["pos"] == p["pos"]
                     and abs(q["overall"] - p["overall"]) <= width]
            if len(peers) >= 3:
                p["expected"] = sum(peers) / len(peers)
                p["delta"] = p["pts"] - p["expected"]
                break
    scored = [p for p in pool if "delta" in p]
    res = []
    for tid in teams:
        mine = [p for p in scored if p["team_id"] == tid]
        if not mine:
            continue
        def row(p):
            return {"player": p["player"], "pos": p["pos"], "round": p["round"], "pick": p["overall"],
                    "points": r2(p["pts"]), "peer_avg_points": r2(p["expected"]), "vs_peers": r2(p["delta"])}
        res.append({"team_id": tid, "team": name[tid],
                    "biggest_bust": row(min(mine, key=lambda p: p["delta"])),
                    "biggest_steal": row(max(mine, key=lambda p: p["delta"]))})
    return res


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
