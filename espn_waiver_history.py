"""
espn_waiver_history.py
League-specific FAAB data for every ESPN league in espn_inseason_pull.ESPN_LEAGUES:
  - the league's FAAB settings (is it a FAAB league at all, and the budget)
  - each team's budget spent / remaining and waiver rank, straight from ESPN
  - EVERY waiver claim, won AND lost, week by week, with its bid amount

Lost bids are the whole point. Winning bids alone only tell you what a player
cost; the losing bids behind them tell you how many teams wanted him and how
hard each manager bids -- which is what the FAAB assistant in index.html
models to predict the winning price for this week's targets.

Source: espn_api's League.transactions(scoring_period, types={"WAIVER",
"WAIVER_ERROR"}) (view=mTransactions2), confirmed against espn_api 0.46
(football/league.py, football/transaction.py): each Transaction carries
team, status, bid_amount, scoring_period, and ADD/DROP items. A claim is
recorded as won when status == "EXECUTED"; every other status is kept raw in
the output and counted in the log, since ESPN's exact failure-status strings
aren't documented and should be checked against a real run, not assumed.

PRIOR SEASON is backfilled ONCE and cached in waiver_history_<year-1>.json,
so manager tendencies aren't empty in week 3. A past season never changes,
so it's never re-pulled once that file exists; delete it to force a refresh.

Read-only, like every other in-season script: nothing here submits a claim.

OUTPUT: waiver_history_<year>.json  (current + cached prior-season claims)
"""
import os
import sys
import json
from collections import Counter
from datetime import datetime, timezone

from espn_api.football import League

from espn_inseason_pull import ESPN_LEAGUES, get_credentials

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"waiver_history_{YEAR}.json"
PRIOR_FILE = f"waiver_history_{YEAR - 1}.json"
PRIOR_SEASON_WEEKS = range(1, 19)   # regular season + playoffs; empty weeks are skipped


def team_meta(t):
    owners = getattr(t, "owners", []) or []
    return {
        "team_id": t.team_id,
        "team_name": t.team_name,
        "manager": ", ".join(
            (o.get("firstName", "") + " " + o.get("lastName", "")).strip()
            if isinstance(o, dict) else str(o) for o in owners) or None,
        "faab_spent": getattr(t, "acquisition_budget_spent", 0) or 0,
        "waiver_rank": getattr(t, "waiver_rank", None),
        "acquisitions": getattr(t, "acquisitions", None),
    }


def positions_for(league, ids):
    """playerId -> position via one batched player_info call. Transactions
    only carry a player id + name, and the FAAB comps view groups past
    auctions by position -- so resolve them here once rather than guess
    from names in the browser. Failure just leaves positions blank."""
    ids = [i for i in ids if i is not None]
    if not ids:
        return {}
    try:
        res = league.player_info(playerId=ids)
    except Exception as e:
        print(f"    ::warning::player_info lookup failed ({e}) -- positions left blank.")
        return {}
    res = res if isinstance(res, list) else [res]
    return {getattr(p, "playerId", None): getattr(p, "position", None) for p in res if p}


def pull_claims(league, season, weeks, status_counts):
    rows, seen = [], set()
    for w in weeks:
        try:
            txs = league.transactions(scoring_period=w, types={"WAIVER", "WAIVER_ERROR"})
        except Exception as e:
            if "No transactions" not in str(e):
                print(f"    ::warning::season {season} week {w}: transactions fetch failed ({e})")
            continue
        for tx in txs:
            adds = [i for i in tx.items if i.type == "ADD"]
            if not adds:
                continue
            drops = [i for i in tx.items if i.type == "DROP"]
            team_id = getattr(tx.team, "team_id", None)
            add = adds[0]
            key = (team_id, add.playerId, tx.bid_amount, tx.date, tx.status)
            if key in seen:
                continue
            seen.add(key)
            status_counts[tx.status] += 1
            rows.append({
                "season": season,
                "week": tx.scoring_period or w,
                "date": tx.date,
                "type": tx.type,
                "status": tx.status,
                "won": tx.status == "EXECUTED",
                "team_id": team_id,
                "bid": tx.bid_amount or 0,
                "add": {"id": add.playerId, "name": add.player if isinstance(add.player, str) else str(add.player)},
                "drop": ({"id": drops[0].playerId,
                          "name": drops[0].player if isinstance(drops[0].player, str) else str(drops[0].player)}
                         if drops else None),
            })
    pos = positions_for(league, list({r["add"]["id"] for r in rows}))
    for r in rows:
        r["add"]["position"] = pos.get(r["add"]["id"])
    return rows


def load_prior(key):
    if not os.path.exists(PRIOR_FILE):
        return None
    try:
        with open(PRIOR_FILE) as f:
            return (json.load(f).get("leagues") or {}).get(key)
    except Exception as e:
        print(f"    ::warning::{PRIOR_FILE} unreadable ({e}) -- will re-pull prior season.")
        return None


def pull_one(cfg, prior_out):
    league_id, s2, swid = get_credentials(cfg)
    if not league_id:
        print(f"  {cfg['key']}: {cfg['league_id_env']} not set -- skipping.")
        return None
    print(f"  {cfg['key']}: loading league {league_id} ({YEAR})...")
    try:
        league = League(league_id=league_id, year=YEAR, espn_s2=s2, swid=swid)
    except Exception as e:
        print(f"  ::warning::{cfg['key']}: failed to load ({e}) -- skipping.")
        return None

    settings = getattr(league, "settings", None)
    faab = bool(getattr(settings, "faab", False))
    budget = getattr(settings, "acquisition_budget", 0) or 0
    current_week = getattr(league, "current_week", None) or 1
    print(f"    FAAB: {'yes, $' + str(budget) if faab else 'NO (rolling waivers)'} | week {current_week}")

    status_counts = Counter()
    claims = pull_claims(league, YEAR, range(1, current_week + 1), status_counts)
    print(f"    {YEAR}: {len(claims)} claims ({sum(c['won'] for c in claims)} won)")

    prior = load_prior(cfg["key"])
    if prior is None:
        print(f"    backfilling {YEAR - 1} (one-time, cached in {PRIOR_FILE})...")
        try:
            pl = League(league_id=league_id, year=YEAR - 1, espn_s2=s2, swid=swid)
            prior = {
                "faab": bool(getattr(pl.settings, "faab", False)),
                "budget": getattr(pl.settings, "acquisition_budget", 0) or 0,
                "teams": {str(t.team_id): team_meta(t) for t in pl.teams},
                "claims": pull_claims(pl, YEAR - 1, PRIOR_SEASON_WEEKS, status_counts),
            }
            print(f"    {YEAR - 1}: {len(prior['claims'])} claims")
        except Exception as e:
            # A league that didn't exist last season (or no access to it) is
            # normal, not an error -- cache an empty result so it isn't
            # retried every week.
            print(f"    no {YEAR - 1} history available ({e}) -- caching empty.")
            prior = {"faab": None, "budget": None, "teams": {}, "claims": []}
    prior_out[cfg["key"]] = prior

    # Raw statuses, surfaced every run: the won/lost split hinges on
    # "EXECUTED", and any surprising status name shows up here first.
    print(f"    claim statuses: {dict(status_counts)}")

    return {
        "platform": "espn",
        "faab": faab,
        "budget": budget,
        "currentWeek": current_week,
        "teams": {str(t.team_id): team_meta(t) for t in league.teams},
        "priorBudget": prior.get("budget"),
        "claims": claims + prior.get("claims", []),
    }


def main():
    out, prior_out = {}, {}
    for cfg in ESPN_LEAGUES:
        res = pull_one(cfg, prior_out)
        if res:
            out[cfg["key"]] = res
    if not out:
        raise SystemExit("No ESPN league produced waiver history this run.")

    with open(OUT_FILE, "w") as f:
        json.dump({"year": YEAR, "generated": datetime.now(timezone.utc).isoformat(),
                   "leagues": out}, f, indent=1)
    # (Re)write the prior cache only when something new was backfilled.
    existing = {}
    if os.path.exists(PRIOR_FILE):
        try:
            with open(PRIOR_FILE) as f:
                existing = json.load(f).get("leagues") or {}
        except Exception:
            existing = {}
    if any(k not in existing for k in prior_out):
        existing.update(prior_out)
        with open(PRIOR_FILE, "w") as f:
            json.dump({"year": YEAR - 1, "generated": datetime.now(timezone.utc).isoformat(),
                       "leagues": existing}, f, indent=1)
        print(f"-> {PRIOR_FILE} (prior-season cache)")
    print(f"-> {OUT_FILE}")


if __name__ == "__main__":
    main()
