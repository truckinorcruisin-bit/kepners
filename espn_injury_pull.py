#!/usr/bin/env python3
"""
espn_injury_pull.py -- injured players' EXPECTED RETURN dates, plus the ESPN leagues' draft results.

Outputs
  espn_injuries_<year>.json   every injured player in ESPN's pool: id, name, position, raw status, return date
  espn_drafts_<year>.json     each ESPN league's completed draft (overall pick, round, player)

WHY THESE TWO ARE PULLED TOGETHER: the "Returning from IR" home-page view needs (a) when each IR player is expected
back and (b) which of them are "good", which this project defines by where they were drafted. The Yahoo leagues' drafts
already exist in kepners_/miami_draft_history.json; the ESPN leagues' drafts do not, so they come from here.

WHAT THE PROBE ESTABLISHED (espn_injury_probe.json, real responses):
  * The return date lives in the fantasy API's `kona_playercard` view, at player.injuryDetails.expectedReturnDate as
    [year, month, day]. It was present on 194 of 194 injured players. The `kona_player_info` view that the projection pull
    uses does NOT carry it, which is why this is a separate pull.
  * player.injuryStatus values seen: INJURY_RESERVE (89), QUESTIONABLE, OUT, DOUBTFUL, DAY_TO_DAY. Raw status is kept
    (the main builder normalises it, which folds IR into OUT and would lose the distinction).
  * The same response carries long outlook texts for every player; none of that is saved.

DRAFTS use the `mDraftDetail` view (draftDetail.picks[]: overallPickNumber / roundId / roundPickNumber / teamId /
playerId / keeper), the same shape espn_live_draft_pull.py already depends on. A draft is saved only once ESPN reports it
complete, and it is not re-fetched on later runs (it can't change) unless --refresh-drafts is given.

SAFETY: a failed or implausibly small pull never overwrites an existing file. The league cookies come from the
environment (ESPN_S2, ESPN_SWID) and are scrubbed from anything printed.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

YEAR = int(os.environ.get("YEAR", "2026") or 2026)
S2 = os.environ.get("ESPN_S2", "").strip()
SWID = os.environ.get("ESPN_SWID", "").strip()
BASE = os.environ.get("FANTASY_BASE", "https://lm-api-reads.fantasy.espn.com").rstrip("/")
POOL = int(os.environ.get("EIP_POOL", "1500"))
PAUSE = float(os.environ.get("EIP_PAUSE", "0.4"))
MIN_POOL = 200                      # fewer players than this means ESPN gave us something other than the pool
LEAGUE_ENVS = (("zimmer", "ESPN_LEAGUE_ID"), ("inspire11", "ESPN_LEAGUE_ID_INSPIRE11"))
POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}
NOT_INJURED = (None, "ACTIVE", "NORMAL")


def scrub(t):
    for secret in (S2, SWID):
        if secret:
            t = t.replace(secret, "***")
    return t


def get(url, headers=None):
    """-> (payload|None, error|None). Retries 429/5xx twice."""
    h = {"Accept": "application/json", "User-Agent": "draft-control-weekly/1",
         "Cookie": f"espn_s2={S2}; SWID={SWID}"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace")), None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * attempt)
                continue
            return None, f"HTTP {e.code}: {scrub(body)[:200]}"
        except Exception as e:
            if attempt == 3:
                return None, scrub(str(e))[:200]
            time.sleep(2 * attempt)
    return None, "gave up"


def league_url(league_id, views):
    q = urllib.parse.urlencode([("view", v) for v in views] + [("scoringPeriodId", 0)])
    return f"{BASE}/apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{league_id}?{q}"


def pool_filter(limit=POOL, ids=None):
    f = {"limit": limit, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}
    if ids is not None:
        f = {"limit": len(ids), "filterIds": {"value": list(ids)}}
    return {"x-fantasy-filter": json.dumps({"players": f})}


def iso_return(details):
    """injuryDetails.expectedReturnDate [y, m, d] -> 'YYYY-MM-DD', or None if absent/invalid."""
    v = (details or {}).get("expectedReturnDate")
    if isinstance(v, (list, tuple)) and len(v) == 3:
        try:
            return date(int(v[0]), int(v[1]), int(v[2])).isoformat()
        except (TypeError, ValueError):
            return None
    return None


def slim_player(entry):
    p = (entry or {}).get("player") or {}
    pos = POS.get(p.get("defaultPositionId"))
    if not p.get("id") or not pos:
        return None
    return {"id": p["id"], "name": p.get("fullName"), "pos": pos}


def slim_injury(entry):
    base = slim_player(entry)
    p = (entry or {}).get("player") or {}
    status = p.get("injuryStatus")
    if not base or not (p.get("injured") or status not in NOT_INJURED):
        return None
    return dict(base, status=status, ret=iso_return(p.get("injuryDetails")))


def fetch_pool(league_id):
    data, err = get(league_url(league_id, ["kona_playercard"]), pool_filter())
    time.sleep(PAUSE)
    if err:
        return None, err
    return data.get("players") or [], None


def names_for(league_id, ids):
    """Resolve player ids the main pool didn't include (a dropped player far down the ownership list)."""
    out = {}
    ids = sorted(ids)
    for i in range(0, len(ids), 150):
        data, err = get(league_url(league_id, ["kona_playercard"]), pool_filter(ids=ids[i:i + 150]))
        time.sleep(PAUSE)
        if err:
            print(f"::warning::could not look up {len(ids[i:i + 150])} drafted player names: {err}")
            continue
        for e in data.get("players") or []:
            sp = slim_player(e)
            if sp:
                out[sp["id"]] = sp
    return out


def pull_draft(league_id, names):
    data, err = get(league_url(league_id, ["mDraftDetail", "mTeam"]))
    time.sleep(PAUSE)
    if err:
        return None, err
    dd = data.get("draftDetail")
    if not dd:
        return None, f"no draftDetail in the response (keys: {list(data.keys())[:8]})"
    if not dd.get("drafted"):
        return None, "ESPN reports this draft as not complete"
    picks = []
    for p in dd.get("picks") or []:
        pid = p.get("playerId")
        if not pid or pid < 0:
            continue
        picks.append({"overall": p.get("overallPickNumber"), "round": p.get("roundId"), "pick": p.get("roundPickNumber"),
                      "team_id": p.get("teamId"), "player_id": pid, "keeper": bool(p.get("keeper"))})
    missing = {x["player_id"] for x in picks if x["player_id"] not in names}
    if missing:
        names.update(names_for(league_id, missing))
    for x in picks:
        sp = names.get(x["player_id"]) or {}
        x["name"], x["pos"] = sp.get("name"), sp.get("pos")
    picks.sort(key=lambda x: x["overall"] or 0)
    return {"drafted": True, "teams": len(data.get("teams") or []), "pick_count": len(picks),
            "unnamed": sum(1 for x in picks if not x["name"]), "picks": picks}, None


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def write(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh-drafts", action="store_true", help="re-fetch drafts even if already saved")
    args = ap.parse_args()

    leagues = [(k, os.environ.get(env, "").strip()) for k, env in LEAGUE_ENVS]
    leagues = [(k, lid) for k, lid in leagues if lid]
    if not (S2 and SWID and leagues):
        print("::error::ESPN_S2, ESPN_SWID and at least one ESPN league id (ESPN_LEAGUE_ID / ESPN_LEAGUE_ID_INSPIRE11) are required.")
        return 1

    # ---- injuries (one pool pull is enough: the injury data is global, not per league) ----
    entries, err = fetch_pool(leagues[0][1])
    if err or len(entries) < MIN_POOL:
        print(f"::error::ESPN player pool unusable ({err or str(len(entries)) + ' players'}); existing files left untouched.")
        return 1
    injuries = [r for r in (slim_injury(e) for e in entries) if r]
    names = {sp["id"]: sp for sp in (slim_player(e) for e in entries) if sp}
    ir = [r for r in injuries if r["status"] == "INJURY_RESERVE"]
    print(f"Pool: {len(entries)} players; injured: {len(injuries)} (IR: {len(ir)}); "
          f"with a return date: {sum(1 for r in injuries if r['ret'])}.")
    if not injuries:
        print("::error::No injured players found in a pool this size -- refusing to overwrite the injuries file with nothing.")
        return 1
    write(f"espn_injuries_{YEAR}.json", {
        "year": YEAR, "generated": datetime.now(timezone.utc).isoformat(),
        "source": "ESPN fantasy API, kona_playercard (player.injuryDetails.expectedReturnDate)",
        "pool_size": len(entries), "injuries": sorted(injuries, key=lambda r: (r["ret"] or "9999", r["name"] or ""))})

    # ---- drafts: each ESPN league once its draft is complete ----
    path = f"espn_drafts_{YEAR}.json"
    doc = load(path) or {"year": YEAR, "leagues": {}}
    doc.setdefault("leagues", {})
    changed = False
    for key, lid in leagues:
        have = doc["leagues"].get(key)
        if have and have.get("drafted") and not args.refresh_drafts:
            print(f"  draft {key}: already saved ({have.get('pick_count')} picks)")
            continue
        d, derr = pull_draft(lid, names)
        if derr:
            print(f"::warning::draft {key}: {derr}")
            continue
        if have and d["pick_count"] < have.get("pick_count", 0):
            print(f"::warning::draft {key}: new pull has fewer picks than the saved one; keeping the saved draft")
            continue
        doc["leagues"][key] = d
        changed = True
        print(f"  draft {key}: {d['pick_count']} picks, {d['teams']} teams"
              + (f", {d['unnamed']} player name(s) unresolved" if d["unnamed"] else ""))
    if changed:
        doc["generated"] = datetime.now(timezone.utc).isoformat()
        write(path, doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
