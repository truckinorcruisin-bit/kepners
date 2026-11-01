"""
yahoo_season_pull.py
Season-detail data for the Yahoo leagues (Kepners Keepers, Miami Domers) that the
roster/standings pull (yahoo_inseason_pull.py) does not carry:

  * weekly matchup scores + remaining schedule       (league scoreboard, every week)
  * each team's weekly lineup: starters vs bench      (team roster + player points)
  * trades                                            (league transactions)
  * playoff settings                                  (league settings)

...and the derived numbers a mid-season recap actually wants:

  * all-play record and luck (actual wins minus expected wins)
  * optimal-lineup points, points left on the bench, lineup efficiency
  * remaining schedule with opponents' average scoring

Read-only. Same auth as the other Yahoo scripts (YAHOO_CLIENT_ID /
YAHOO_CLIENT_SECRET / YAHOO_REFRESH_TOKEN).

Completed weeks are cached in the previous output file and not re-pulled, so a
weekly run costs a few dozen calls, not a few hundred. Set FORCE=1 to refetch
everything.

OUTPUT: yahoo_season_<year>.json
"""
import os
import sys
import json
import time
from datetime import datetime, timezone

from yahoo_setup import api_get
from yahoo_inseason_pull import find_leagues, flatten, numbered, _int, _float

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"yahoo_season_{YEAR}.json"
FORCE = os.environ.get("FORCE", "") not in ("", "0", "false")
PAUSE = 0.25          # politeness gap between calls
PAGE = 25             # Yahoo caps most collections at 25 per request
MAX_WEEK = 18

BENCH_SLOTS = {"BN", "IR", "IR+", "NA"}
FLEX_LETTERS = {"Q": "QB", "R": "RB", "W": "WR", "T": "TE"}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------
def get(path, tries=3):
    """api_get with retries. Returns None after the last failure; callers treat
    a miss as 'no data', never as a crash, so one bad week can't cost the run."""
    for i in range(tries):
        try:
            time.sleep(PAUSE)
            return api_get(path)
        except Exception as e:                                  # noqa: BLE001
            if i == tries - 1:
                print(f"  ::warning::giving up on {path} ({e})")
                return None
            time.sleep(1.5 * (i + 1))
    return None


def find_key(obj, key):
    """First value stored under `key` anywhere in Yahoo's nested dict/list soup."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def team_id_of(team_key):
    return str(team_key).split(".t.")[-1] if team_key else None


def _meta(team):
    """Team metadata dict out of a Yahoo 'team' value (list whose first element is the meta list)."""
    return flatten(team[0]) if isinstance(team, list) else flatten(team)


# ---------------------------------------------------------------------------
# League info, settings, teams
# ---------------------------------------------------------------------------
def pull_settings(lk):
    data = get(f"league/{lk}/settings")
    if not data:
        return {}, []
    lg = data["fantasy_content"]["league"]
    meta = flatten(lg[0])
    st = flatten(lg[1].get("settings") if isinstance(lg[1], dict) else {})
    slots = []
    rp = st.get("roster_positions") or []
    items = list(numbered(rp)) if isinstance(rp, dict) else rp
    for it in items:
        r = (it or {}).get("roster_position") or {}
        pos, cnt = r.get("position"), _int(r.get("count")) or 0
        if pos:
            slots.extend([pos] * cnt)
    settings = {
        "start_week": _int(meta.get("start_week")) or 1,
        "end_week": _int(meta.get("end_week")) or 17,
        "current_week": _int(meta.get("current_week")),
        "playoff_start_week": _int(st.get("playoff_start_week")),
        "num_playoff_teams": _int(st.get("num_playoff_teams")),
        "uses_playoff": st.get("uses_playoff"),
        "trade_end_date": st.get("trade_end_date"),
        "uses_faab": st.get("uses_faab"),
        "roster_slots": slots,
    }
    return settings, slots


def pull_teams(lk):
    data = get(f"league/{lk}/teams")
    out = {}
    if not data:
        return out
    block = data["fantasy_content"]["league"][1]["teams"]
    for t in numbered(block):
        team = t.get("team")
        meta = _meta(team)
        manager = None
        for m in meta.get("managers") or []:
            manager = flatten(m.get("manager", m)).get("nickname") or manager
        out[str(meta.get("team_id"))] = {
            "team_id": str(meta.get("team_id")),
            "team_key": meta.get("team_key"),
            "name": meta.get("name"),
            "manager": manager,
        }
    return out


# ---------------------------------------------------------------------------
# Scoreboard (matchups + schedule)
# ---------------------------------------------------------------------------
def parse_scoreboard(data, week):
    out = []
    mblock = find_key(data, "matchups")
    for entry in numbered(mblock):
        m = entry.get("matchup") if isinstance(entry, dict) else None
        if not m:
            continue
        mm = flatten(m) if isinstance(m, list) else m
        teams = []
        for t in numbered(find_key(m, "teams")):
            team = t.get("team")
            meta = _meta(team)
            rest = flatten(team[1:]) if isinstance(team, list) and len(team) > 1 else {}
            teams.append({
                "team_id": str(meta.get("team_id")),
                "name": meta.get("name"),
                "points": _float(flatten(rest.get("team_points") or {}).get("total")),
                "projected": _float(flatten(rest.get("team_projected_points") or {}).get("total")),
            })
        out.append({
            "week": _int(mm.get("week")) or week,
            "status": mm.get("status"),                         # preevent / midevent / postevent
            "is_playoffs": str(mm.get("is_playoffs", "0")) == "1",
            "is_consolation": str(mm.get("is_consolation", "0")) == "1",
            "is_tied": str(mm.get("is_tied", "0")) == "1",
            "winner_team_id": team_id_of(mm.get("winner_team_key")),
            "teams": teams,
        })
    return out


def pull_scoreboard(lk, week):
    data = get(f"league/{lk}/scoreboard;week={week}")
    return parse_scoreboard(data, week) if data else []


# ---------------------------------------------------------------------------
# Weekly lineups (starters vs bench) + optimal lineup
# ---------------------------------------------------------------------------
def pull_lineup(team_key, week):
    data = get(f"team/{team_key}/roster;week={week}/players/stats;type=week;week={week}")
    if not data:
        return None
    players = []
    for pl in numbered(find_key(data, "players")):
        p = flatten(pl.get("player", pl))
        name = p.get("name") or {}
        sel = p.get("selected_position")
        pts = _float(flatten(p.get("player_points") or {}).get("total"))
        players.append({
            "name": name.get("full") if isinstance(name, dict) else name,
            "pos": (p.get("display_position") or p.get("primary_position") or "").upper(),
            "slot": flatten(sel).get("position") if sel else None,
            "pts": pts,
        })
    return players or None


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


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------
def pull_trades(lk):
    trades, start = [], 0
    while True:
        data = get(f"league/{lk}/transactions;types=trade;start={start};count={PAGE}")
        if not data:
            break
        block = find_key(data, "transactions")
        if not isinstance(block, dict):
            break
        rows = list(numbered(block))
        for r in rows:
            tx = r.get("transaction") if isinstance(r, dict) else None
            if not tx:
                continue
            meta = flatten(tx[0]) if isinstance(tx, list) else flatten(tx)
            if meta.get("type") != "trade":          # belt and braces if the filter is ignored
                continue
            moves = []
            for pl in numbered(find_key(tx[1:] if isinstance(tx, list) else tx, "players")):
                p = flatten(pl.get("player", pl))
                td = flatten(p.get("transaction_data") or {})
                name = p.get("name") or {}
                moves.append({
                    "player": name.get("full") if isinstance(name, dict) else name,
                    "pos": p.get("display_position"),
                    "pro_team": p.get("editorial_team_abbr"),
                    "from_team_id": team_id_of(td.get("source_team_key")),
                    "from_team": td.get("source_team_name"),
                    "to_team_id": team_id_of(td.get("destination_team_key")),
                    "to_team": td.get("destination_team_name"),
                })
            ts = _int(meta.get("timestamp"))
            trades.append({
                "transaction_id": meta.get("transaction_id"),
                "status": meta.get("status"),
                "date": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
                "trader_team_id": team_id_of(meta.get("trader_team_key")),
                "tradee_team_id": team_id_of(meta.get("tradee_team_key")),
                "moves": moves,
            })
        if len(rows) < PAGE:
            break
        start += PAGE
    return trades


# ---------------------------------------------------------------------------
# Derived: all-play / luck, remaining schedule
# ---------------------------------------------------------------------------
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
# Draft results (fetched once, then cached -- a finished draft never changes)
# ---------------------------------------------------------------------------
def pull_draft(lk, prior_draft):
    if prior_draft and len(prior_draft) >= 50:
        print(f"  draft: {len(prior_draft)} picks (cached)")
        return prior_draft
    data = get(f"league/{lk}/draftresults")
    if not data:
        return prior_draft or []
    picks = []
    for entry in numbered(find_key(data, "draft_results")):
        dr = entry.get("draft_result") if isinstance(entry, dict) else None
        if dr:
            picks.append({"round": _int(dr.get("round")), "pick": _int(dr.get("pick")),
                          "team_id": team_id_of(dr.get("team_key")), "player_key": dr.get("player_key")})
    keys = [p["player_key"] for p in picks if p.get("player_key")]
    info = {}
    for i in range(0, len(keys), PAGE):
        chunk = keys[i:i + PAGE]
        pdata = get(f"league/{lk}/players;player_keys={','.join(chunk)}")
        for pl in numbered(find_key(pdata, "players")) if pdata else []:
            p = flatten(pl.get("player", pl))
            nm = p.get("name") or {}
            info[p.get("player_key")] = {
                "player": nm.get("full") if isinstance(nm, dict) else nm,
                "pos": (p.get("display_position") or "").upper(),
                "pro_team": p.get("editorial_team_abbr"),
            }
    for p in picks:
        p.update(info.get(p["player_key"], {}))
    print(f"  draft: {len(picks)} picks pulled, {len(info)} players named")
    return picks

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def load_prior():
    if FORCE or not os.path.exists(OUT_FILE):
        return {}
    try:
        with open(OUT_FILE) as f:
            return json.load(f).get("leagues", {})
    except Exception:                                           # noqa: BLE001
        return {}


def pull_league(key, meta, prior):
    lk = meta["league_key"]
    print(f"[{key}] settings...")
    settings, slots = pull_settings(lk)
    settings["current_week"] = _int(meta.get("current_week")) or settings.get("current_week")
    teams = pull_teams(lk)
    cur = settings.get("current_week") or 1
    last = min(settings.get("end_week") or 17, MAX_WEEK)
    print(f"  {len(teams)} teams, week {cur} of {last}")

    weeks = {}
    for w in range(settings.get("start_week") or 1, last + 1):
        ms = pull_scoreboard(lk, w)
        if ms:
            weeks[str(w)] = ms
    played = sorted(int(w) for w, ms in weeks.items() if any(m["status"] == "postevent" for m in ms))
    print(f"  scoreboard: {len(weeks)} weeks, {len(played)} completed")

    lineups = {tid: dict((prior.get("lineups") or {}).get(tid, {})) for tid in teams}
    pulled = 0
    for w in played:
        for tid, t in teams.items():
            if str(w) in lineups[tid]:
                continue
            players = pull_lineup(t["team_key"], w)
            if players and any(p["pts"] is not None for p in players):
                lineups[tid][str(w)] = summarize_lineup(players, slots)
                pulled += 1
    print(f"  lineups: {pulled} new team-weeks pulled")

    # Season efficiency per team, from whatever lineups exist.
    for tid, wk in lineups.items():
        if wk:
            sp = sum(x["starter_points"] for x in wk.values())
            op = sum(x["optimal_points"] or 0 for x in wk.values())
            teams[tid]["lineup_season"] = {
                "weeks": len(wk),
                "starter_points": round(sp, 2),
                "optimal_points": round(op, 2),
                "points_left": round(op - sp, 2),
                "efficiency": round(sp / op, 3) if op else None,
            }

    derived = derive_team_stats(teams, weeks)
    for tid in teams:
        teams[tid]["derived"] = derived.get(tid, {})
    schedule = derive_schedule(teams, weeks, derived)
    for tid in teams:
        opps = [s["opp_avg_pf"] for s in schedule[tid] if s["opp_avg_pf"] is not None]
        teams[tid]["remaining_schedule"] = schedule[tid]
        teams[tid]["remaining_opp_avg_pf"] = round(sum(opps) / len(opps), 2) if opps else None

    print(f"[{key}] draft...")
    try:
        draft = pull_draft(lk, prior.get("draft"))
    except Exception as e:                                      # noqa: BLE001
        print(f"  ::warning::draft pull failed ({e!r}); continuing without it.")
        draft = prior.get("draft") or []

    print(f"[{key}] trades...")
    trades = pull_trades(lk)
    print(f"  {len(trades)} trades")

    if not played:
        print(f"  ::warning::[{key}] no completed weeks parsed -- scoreboard shape may have changed.")
    return {
        "league_key": lk,
        "league_name": meta.get("name"),
        "settings": settings,
        "teams": teams,
        "weeks": weeks,
        "lineups": lineups,
        "trades": trades,
        "draft": draft,
    }


# Last-resort league keys (2026 NFL game key 470). Only used if both auto-discovery and
# yahoo_inseason_<year>.json come up empty. Update when the game key changes next season.
KNOWN_LEAGUES = {"kepners": "470.l.102398", "miami": "470.l.391024"}


def discover():
    """League lookup that survives find_leagues() coming back empty.

    1) normal discovery via the logged-in user's games/leagues;
    2) if that is empty, dump the raw response (so the cause is visible in the log) and use league
       keys already recorded in yahoo_inseason_<year>.json, then KNOWN_LEAGUES;
    each key is verified with a direct league call, which also surfaces auth/scope errors."""
    try:
        found = find_leagues()
    except Exception as e:                                      # noqa: BLE001
        print(f"::warning::league discovery raised {e!r}")
        found = {}
    if found:
        return found
    print("::warning::league discovery returned nothing; raw response follows, then falling back to known keys.")
    try:
        raw = api_get(f"users;use_login=1/games;game_codes=nfl;seasons={YEAR}/leagues")
        print(json.dumps(raw)[:2000])
    except Exception as e:                                      # noqa: BLE001
        print(f"  discovery call itself failed: {e!r}")
    keys = {}
    try:
        with open(f"yahoo_inseason_{YEAR}.json") as f:
            for k, lg in (json.load(f).get("leagues") or {}).items():
                if lg.get("league_key"):
                    keys[k] = lg["league_key"]
    except Exception:                                           # noqa: BLE001
        pass
    for k, lk in KNOWN_LEAGUES.items():
        keys.setdefault(k, lk)
    for k, lk in keys.items():
        data = get(f"league/{lk}")
        if not data:
            continue
        meta = flatten(data["fantasy_content"]["league"][0])
        found[k] = {"league_key": lk, "name": meta.get("name"), "current_week": meta.get("current_week")}
        print(f"  fallback: {k} -> {lk} ({meta.get('name')})")
    return found


def main():
    if not os.environ.get("YAHOO_REFRESH_TOKEN") and not os.path.exists("yahoo_token.json"):
        raise SystemExit("No Yahoo credentials (YAHOO_REFRESH_TOKEN secret missing).")
    leagues = discover()
    if not leagues:
        raise SystemExit(f"No Yahoo leagues found for {YEAR}, even by direct league key. "
                         f"See the ::error:: lines above for Yahoo's reason.")
    prior = load_prior()
    out_leagues = {}
    for key, meta in leagues.items():
        out_leagues[key] = pull_league(key, meta, prior.get(key, {}))
    if not any(lg["weeks"] for lg in out_leagues.values()):
        raise SystemExit("Nothing parsed from any scoreboard -- not writing an empty file.")
    out = {"year": YEAR, "generated": datetime.now(timezone.utc).isoformat(), "leagues": out_leagues}
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=1)
    print(f"Wrote {OUT_FILE}.")


if __name__ == "__main__":
    main()
