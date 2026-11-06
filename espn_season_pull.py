"""
espn_season_pull.py
Season-detail data for the ESPN leagues (Zimmer, Inspire11) -- the ESPN counterpart of
yahoo_season_pull.py, in the SAME output shape so yahoo_advanced_analysis.py can read it:

  * weekly matchup scores + remaining schedule
  * each team's weekly lineup: starters vs bench, with ESPN's own per-player projection
  * accepted trades
  * the league draft (from espn_drafts_<year>.json, already in the repo)

...plus derived all-play / luck, optimal-lineup points, points left on the bench.

Read-only. Same credentials and league config as espn_inseason_pull.py
(ESPN_S2, ESPN_SWID, ESPN_LEAGUE_ID, ...). Never submits anything.

OUTPUT: espn_season_<year>.json
"""
import os
import sys
import json
from datetime import datetime, timezone

from espn_api.football import League

from espn_inseason_pull import ESPN_LEAGUES, get_credentials, parse_roster_slots

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"espn_season_{YEAR}.json"
DRAFTS_FILE = f"espn_drafts_{YEAR}.json"
MAX_WEEK = 18

BENCH_SLOTS = {"BN", "BEN", "BE", "IR", "IR+", "NA"}
FLEX_LETTERS = {"Q": "QB", "R": "RB", "W": "WR", "T": "TE"}
SLOT_MAP = {"D/ST": "DEF", "BE": "BEN"}


def r2(x):
    return None if x is None else round(float(x), 2)


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def canon_slot(s):
    s = SLOT_MAP.get(s, s or "")
    return "FLEX" if "/" in s else s


def canon_pos(p):
    return "DEF" if p in ("D/ST", "DST") else (p or "").upper()


def eligible_positions(slot):
    """Positions that may fill a starting slot. Dedicated slots (QB, RB, WR, TE, K, DEF) take
    only themselves; Yahoo's flex slots are named by letter (W/R/T, W/T, Q/W/R/T ...)."""
    if "/" in slot:
        return {FLEX_LETTERS[c] for c in slot.split("/") if c in FLEX_LETTERS}
    if slot.upper() == "FLEX":
        return {"RB", "WR", "TE"}
    return {slot.upper()}


def optimal_points(players, slots):
    """Best possible starting-lineup total given the week's points.

    Dedicated slots fill first with their position's top scorers, then flex slots, most
    restrictive first, take the best of whoever is left. That ordering is optimal for this
    nested-eligibility structure."""
    starters = [s for s in slots if s not in BENCH_SLOTS]
    pool = sorted((p for p in players if p.get("pts") is not None), key=lambda p: -p["pts"])
    used, total = set(), 0.0
    for slot in sorted(starters, key=lambda s: len(eligible_positions(s))):
        elig = eligible_positions(slot)
        for i, p in enumerate(pool):
            if i not in used and p["pos"] in elig:
                used.add(i)
                total += p["pts"]
                break
    return round(total, 2)


def summarize_lineup(players, slots):
    starters = [p for p in players if p["slot"] and p["slot"] not in BENCH_SLOTS]
    bench = [p for p in players if p["slot"] in BENCH_SLOTS and p["slot"] != "IR"]
    sp = round(sum(p["pts"] or 0 for p in starters), 2)
    bp = round(sum(p["pts"] or 0 for p in bench), 2)
    opt = optimal_points(players, slots) if slots else None
    top_bench = max(bench, key=lambda p: p["pts"] or 0, default=None)
    return {
        "starters": starters,
        "bench": bench,
        "starter_points": sp,
        "bench_points": bp,
        "optimal_points": opt,
        "points_left": round(opt - sp, 2) if opt is not None else None,
        "efficiency": round(sp / opt, 3) if opt else None,
        "top_bench": ({"name": top_bench["name"], "pos": top_bench["pos"], "pts": top_bench["pts"]}
                      if top_bench else None),
    }


def derive_team_stats(teams, weeks):
    """All-play record and luck from completed regular-season weeks."""
    stats = {tid: {"wins": 0, "losses": 0, "ties": 0, "ap_wins": 0, "ap_losses": 0, "ap_ties": 0,
                   "pf": 0.0, "pa": 0.0, "games": 0, "close_wins": 0, "close_losses": 0,
                   "high_score": None, "low_score": None, "scores": []} for tid in teams}
    for wk, matchups in sorted(weeks.items(), key=lambda kv: int(kv[0])):
        done = [m for m in matchups if m["status"] == "postevent" and not m["is_playoffs"]]
        scores = {t["team_id"]: t["points"] for m in done for t in m["teams"] if t["points"] is not None}
        for m in done:
            if len(m["teams"]) != 2:
                continue
            a, b = m["teams"]
            for me, opp in ((a, b), (b, a)):
                s = stats.get(me["team_id"])
                if s is None or me["points"] is None or opp["points"] is None:
                    continue
                s["games"] += 1
                s["pf"] += me["points"]
                s["pa"] += opp["points"]
                s["scores"].append({"week": int(wk), "pts": me["points"], "opp": opp["team_id"], "opp_pts": opp["points"]})
                margin = me["points"] - opp["points"]
                if m["is_tied"] or margin == 0:
                    s["ties"] += 1
                elif margin > 0:
                    s["wins"] += 1
                    if margin < 10:
                        s["close_wins"] += 1
                else:
                    s["losses"] += 1
                    if margin > -10:
                        s["close_losses"] += 1
                hs, ls = s["high_score"], s["low_score"]
                s["high_score"] = me["points"] if hs is None else max(hs, me["points"])
                s["low_score"] = me["points"] if ls is None else min(ls, me["points"])
        for tid, pts in scores.items():
            if tid not in stats:
                continue
            for oid, opts in scores.items():
                if oid == tid:
                    continue
                k = "ap_wins" if pts > opts else "ap_losses" if pts < opts else "ap_ties"
                stats[tid][k] += 1
    out = {}
    for tid, s in stats.items():
        ap_games = s["ap_wins"] + s["ap_losses"] + s["ap_ties"]
        exp = ((s["ap_wins"] + 0.5 * s["ap_ties"]) / ap_games * s["games"]) if ap_games else None
        actual = s["wins"] + 0.5 * s["ties"]
        out[tid] = {
            "record": f"{s['wins']}-{s['losses']}" + (f"-{s['ties']}" if s["ties"] else ""),
            "games": s["games"],
            "pf": round(s["pf"], 2), "pa": round(s["pa"], 2),
            "avg_pf": round(s["pf"] / s["games"], 2) if s["games"] else None,
            "all_play": f"{s['ap_wins']}-{s['ap_losses']}" + (f"-{s['ap_ties']}" if s["ap_ties"] else ""),
            "expected_wins": round(exp, 2) if exp is not None else None,
            "luck": round(actual - exp, 2) if exp is not None else None,    # + = lucky, - = unlucky
            "close_wins": s["close_wins"], "close_losses": s["close_losses"],
            "high_score": s["high_score"], "low_score": s["low_score"],
            "scores": s["scores"],
        }
    return out


def derive_schedule(teams, weeks, avg_pf):
    """Upcoming opponents per team (matchups not yet postevent), with their average scoring."""
    sched = {tid: [] for tid in teams}
    for wk, matchups in sorted(weeks.items(), key=lambda kv: int(kv[0])):
        for m in matchups:
            if m["status"] == "postevent" or len(m["teams"]) != 2:
                continue
            a, b = m["teams"]
            for me, opp in ((a, b), (b, a)):
                if me["team_id"] in sched:
                    sched[me["team_id"]].append({
                        "week": int(wk), "opp_team_id": opp["team_id"], "opp_name": opp["name"],
                        "opp_avg_pf": (avg_pf.get(opp["team_id"]) or {}).get("avg_pf"),
                        "is_playoffs": m["is_playoffs"],
                    })
    return sched


# ---------------------------------------------------------------------------
# ESPN-specific pulls
# ---------------------------------------------------------------------------
def manager_name(t):
    owners = getattr(t, "owners", []) or []
    if owners and isinstance(owners[0], dict):
        o = owners[0]
        return " ".join(x for x in (o.get("firstName"), o.get("lastName")) if x) or o.get("displayName")
    return None


def box_to_matchup(box, week):
    if box.home_team is None or box.away_team is None or isinstance(box.home_team, int) or isinstance(box.away_team, int):
        return None, {}
    sides = (("home", box.home_team, box.home_score, box.home_projected, box.home_lineup),
             ("away", box.away_team, box.away_score, box.away_projected, box.away_lineup))
    teams, lineups = [], {}
    for _, team, score, proj, lineup in sides:
        tid = str(team.team_id)
        teams.append({"team_id": tid, "name": team.team_name, "points": r2(score),
                      "projected": r2(proj) if proj and proj > 0 else None})
        lineups[tid] = [{"name": p.name, "pos": canon_pos(getattr(p, "position", "")),
                         "slot": canon_slot(getattr(p, "slot_position", "")),
                         "pts": r2(getattr(p, "points", 0)), "proj": r2(getattr(p, "projected_points", 0))}
                        for p in lineup]
    a, b = teams
    tied = a["points"] == b["points"]
    winner = None if tied else (a["team_id"] if a["points"] > b["points"] else b["team_id"])
    return {"week": week, "status": "postevent", "is_playoffs": bool(getattr(box, "is_playoff", False)),
            "is_consolation": False, "is_tied": tied, "winner_team_id": winner, "teams": teams}, lineups


def schedule_from_teams(league, weeks, completed):
    """Future matchups from each team's schedule list (box scores only exist for played weeks)."""
    seen = set()
    for t in league.teams:
        for i, opp in enumerate(getattr(t, "schedule", []) or []):
            wk = i + 1
            if wk in completed or wk > MAX_WEEK or not hasattr(opp, "team_id"):
                continue
            key = (wk, *sorted((t.team_id, opp.team_id)))
            if key in seen:
                continue
            seen.add(key)
            weeks.setdefault(str(wk), []).append({
                "week": wk, "status": "preevent", "is_playoffs": False, "is_consolation": False,
                "is_tied": False, "winner_team_id": None,
                "teams": [{"team_id": str(t.team_id), "name": t.team_name, "points": None, "projected": None},
                          {"team_id": str(opp.team_id), "name": opp.team_name, "points": None, "projected": None}]})


def pull_trades(league, teams):
    """Accepted trades via league.recent_activity (TRADE_SENT / TRADE_RECEIVED rows)."""
    out, offset, size = [], 0, 25
    for _ in range(8):
        try:
            acts = league.recent_activity(size=size, msg_type="TRADED", offset=offset)
        except Exception as e:                                      # noqa: BLE001
            print(f"  ::warning::trade pull failed ({e!r})")
            break
        for act in acts:
            moves, pending = [], None
            for tup in act.actions:
                team, action, player = tup[0], tup[1], tup[2]
                pname = getattr(player, "name", None) or str(player)
                pos = canon_pos(getattr(player, "position", ""))
                tid = str(getattr(team, "team_id", "")) if team else None
                if action == "TRADE_SENT":
                    pending = {"player": pname, "pos": pos, "from_team_id": tid,
                               "from_team": getattr(team, "team_name", None)}
                elif action == "TRADE_RECEIVED" and pending and pending["player"] == pname:
                    pending.update({"to_team_id": tid, "to_team": getattr(team, "team_name", None)})
                    moves.append(pending)
                    pending = None
            if moves:
                ts = _int(getattr(act, "date", None))
                out.append({"transaction_id": None, "status": "successful",
                            "date": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
                            "moves": moves})
        if len(acts) < size:
            break
        offset += size
    print(f"  {len(out)} trades")
    return out


def load_draft(key):
    try:
        with open(DRAFTS_FILE) as f:
            lg = (json.load(f).get("leagues") or {}).get(key) or {}
    except Exception:                                               # noqa: BLE001
        return []
    return [{"round": p.get("round"), "pick": p.get("overall"), "team_id": str(p.get("team_id")),
             "player": p.get("name"), "pos": canon_pos(p.get("pos")), "keeper": p.get("keeper")}
            for p in lg.get("picks", [])]


def pull_league(cfg):
    league_id, s2, swid = get_credentials(cfg)
    if league_id is None:
        print(f"[{cfg['key']}] {cfg['league_id_env']} not set -- skipping.")
        return None
    key = cfg["key"]
    print(f"[{key}] loading league {league_id}...")
    league = League(league_id=league_id, year=YEAR, espn_s2=s2, swid=swid)
    cur = league.current_week
    slots, _ = parse_roster_slots(league)
    teams = {str(t.team_id): {"team_id": str(t.team_id), "team_key": None, "name": t.team_name,
                              "manager": manager_name(t)} for t in league.teams}
    # Diagnostic: standings show more results than weeks played in Zimmer, so record how the league counts them.
    for t in league.teams[:2]:
        print(f"  diag {t.team_name}: wins={t.wins} losses={t.losses} scores={len(getattr(t, 'scores', []))} "
              f"outcomes={len(getattr(t, 'outcomes', []))} schedule={len(getattr(t, 'schedule', []))}")
    print(f"  matchup_periods={dict(list(league.settings.matchup_periods.items())[:3])}...")
    print(f"  {len(teams)} teams, current week {cur}")

    weeks, lineups, cache = {}, {tid: {} for tid in teams}, {}
    completed = set()
    for w in range(1, min(cur, MAX_WEEK + 1)):
        try:
            boxes = league.box_scores(w, player_team_cache=cache)
        except Exception as e:                                      # noqa: BLE001
            print(f"  ::warning::box scores week {w} failed ({e!r})")
            continue
        for box in boxes:
            m, lns = box_to_matchup(box, w)
            if not m:
                continue
            weeks.setdefault(str(w), []).append(m)
            for tid, players in lns.items():
                if tid in lineups and any(p["pts"] is not None for p in players):
                    ln = summarize_lineup(players, slots)
                    ln["projected_starters"] = r2(sum(p["proj"] or 0 for p in players if p["slot"] not in BENCH_SLOTS))
                    lineups[tid][str(w)] = ln
        if str(w) in weeks:
            completed.add(w)
    print(f"  box scores: {len(completed)} completed weeks")
    schedule_from_teams(league, weeks, completed)

    for tid, wk in lineups.items():
        if wk:
            sp = sum(x["starter_points"] for x in wk.values())
            op = sum(x["optimal_points"] or 0 for x in wk.values())
            teams[tid]["lineup_season"] = {"weeks": len(wk), "starter_points": round(sp, 2),
                                           "optimal_points": round(op, 2), "points_left": round(op - sp, 2),
                                           "efficiency": round(sp / op, 3) if op else None}
    derived = derive_team_stats(teams, weeks)
    sched = derive_schedule(teams, weeks, derived)
    for tid in teams:
        teams[tid]["derived"] = derived.get(tid, {})
        opps = [s["opp_avg_pf"] for s in sched[tid] if s["opp_avg_pf"] is not None]
        teams[tid]["remaining_schedule"] = sched[tid]
        teams[tid]["remaining_opp_avg_pf"] = round(sum(opps) / len(opps), 2) if opps else None

    return {"league_key": str(league_id), "league_name": getattr(league.settings, "name", key),
            "settings": {"current_week": cur, "roster_slots": slots},
            "teams": teams, "weeks": weeks, "lineups": lineups,
            "trades": pull_trades(league, teams), "draft": load_draft(key)}


def main():
    out = {}
    for cfg in ESPN_LEAGUES:
        try:
            lg = pull_league(cfg)
        except Exception as e:                                      # noqa: BLE001
            print(f"::error::[{cfg['key']}] failed: {e!r}")
            continue
        if lg:
            out[cfg["key"]] = lg
    if not any(lg["weeks"] for lg in out.values()):
        raise SystemExit("Nothing parsed from any ESPN league -- not writing an empty file.")
    with open(OUT_FILE, "w") as f:
        json.dump({"year": YEAR, "generated": datetime.now(timezone.utc).isoformat(), "leagues": out}, f, indent=1)
    print(f"Wrote {OUT_FILE}.")


if __name__ == "__main__":
    main()
