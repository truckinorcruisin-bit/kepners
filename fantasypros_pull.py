#!/usr/bin/env python3
"""
fantasypros_pull.py -- weekly pull of FantasyPros REST-OF-SEASON expert consensus rankings (ECR).

Writes fantasypros_ros_<season>.json. Run by the Tuesday "Update In-Season Data" workflow.

WHAT THE PROBE ESTABLISHED (fantasypros_probe.json, real responses with this project's key):
  * Endpoint  GET /nfl/<season>/consensus-rankings?position=<P>&scoring=STD&type=ROS
    - type=ROS (case-insensitive) is the ONLY spelling that returns rest-of-season rankings. Guesses such as
      "rest-of-season" or week=ros are silently ignored and the server falls back to the preseason DRAFT
      rankings -- which would look plausible and be wrong. So every response is checked: its own
      ranking_type_name must be "ros", or the position is rejected.
    - position must be one position at a time (QB, RB, WR, TE, K, DST...); "ALL" is refused.
  * Each row carries the player's Yahoo ID (matches the Yahoo IDs already in the Kepners/Miami data exactly);
    the ESPN leagues have to be matched by name + position.
  * The response reports its own "tier", "limit" and "count". A FREE key returns only the first 10 rows of
    each position (e.g. 10 of 112 RBs). That is the key's sample cap, not a bug, and this script does NOT try to
    get around it: it reports "partial" and stops. A production (Premium) key is needed for full rankings.

WHAT IT SAVES (deliberately minimal): per player only the fields the cockpit would show or match on --
  FantasyPros id, name, team, position, Yahoo id, bye week, ECR rank, position rank, tier, and the spread of
  expert opinion (best / worst / average / std-dev). Not the raw feed, not ownership %, not the player pages.

SAFETY: a partial pull never overwrites a complete earlier file; a position that fails keeps its previous
rows (flagged); the key comes from the FANTASYPROS_API_KEY environment variable only and is scrubbed from
anything printed or written.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("FANTASYPROS_BASE", "https://api.fantasypros.com/public/v2/json").rstrip("/")
KEY = os.environ.get("FANTASYPROS_API_KEY", "").strip()
PAUSE = float(os.environ.get("FP_PAUSE", "0.5"))          # seconds between calls
DEFAULT_POSITIONS = "QB,RB,WR,TE,K,DST"
ATTRIBUTION = "Rankings: FantasyPros (fantasypros.com), Expert Consensus Rankings"


def scrub(text):
    return text.replace(KEY, "***") if KEY else text


def get(path, params):
    """-> (status, payload|None, error_text|None). Retries 429/5xx twice, honouring Retry-After."""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-api-key": KEY, "Accept": "application/json",
                                               "User-Agent": "draft-control-weekly/1"})
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                status, body, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, body, headers = e.code, e.read(), e.headers
        except Exception as e:
            if attempt == 3:
                return None, None, scrub(str(e))[:200]
            time.sleep(2 * attempt)
            continue
        if status in (429, 500, 502, 503, 504) and attempt < 3:
            try:
                wait = min(30.0, float(headers.get("Retry-After", 2 * attempt)))
            except (TypeError, ValueError):
                wait = 2.0 * attempt
            time.sleep(wait)
            continue
        text = body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except Exception:
            return status, None, scrub(text)[:300]
        if status >= 400:
            return status, None, scrub(json.dumps(payload))[:300]
        return status, payload, None
    return None, None, "gave up after 3 attempts"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def slim(row):
    """The only fields kept from a ranking row."""
    bye = row.get("player_bye_week")
    return {
        "fp_id": row.get("player_id"),
        "name": row.get("player_name"),
        "team": row.get("player_team_id"),
        "pos": row.get("player_position_id"),
        "yahoo_id": str(row["player_yahoo_id"]) if row.get("player_yahoo_id") else None,
        "bye": int(bye) if str(bye or "").isdigit() else None,
        "ecr": row.get("rank_ecr"),
        "pos_rank": row.get("pos_rank"),
        "tier": row.get("tier"),
        "best": num(row.get("rank_min")),
        "worst": num(row.get("rank_max")),
        "avg": num(row.get("rank_ave")),
        "std": num(row.get("rank_std")),
    }


def fetch_position(pos, season, scoring):
    """-> (info, rows, error). info always says what the server reported about itself."""
    params = {"position": pos, "scoring": scoring, "type": "ROS"}
    path = f"/nfl/{season}/consensus-rankings"
    status, payload, err = get(path, params)
    if err:
        return None, [], f"HTTP {status}: {err}"
    kind = str(payload.get("ranking_type_name") or payload.get("type") or "").lower()
    if kind != "ros":
        return None, [], (f"server returned '{kind or '?'}' rankings instead of ROS -- refusing to mix them in "
                          f"(parameters it accepted: {json.dumps(params)})")
    rows = payload.get("players") or []
    count = int(payload.get("count") or len(rows))
    info = {"count": count, "returned": len(rows), "limit": payload.get("limit"),
            "api_tier": payload.get("tier"), "last_updated": payload.get("last_updated"),
            "total_experts": payload.get("total_experts")}
    # A production key that still defaults to a page smaller than the full list: ask once for all of it.
    # NEVER done on a free key -- there the cap is the key's sample limit, not a paging default.
    if len(rows) < count and str(info["api_tier"]).lower() != "free":
        time.sleep(PAUSE)
        s2, p2, e2 = get(path, dict(params, limit=count))
        if not e2 and str(p2.get("ranking_type_name") or "").lower() == "ros" and len(p2.get("players") or []) > len(rows):
            rows = p2["players"]
            info.update(returned=len(rows), limit=p2.get("limit"))
    info["complete"] = len(rows) >= count
    return info, rows, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", type=int, default=int(os.environ.get("YEAR", "2026") or 2026))
    ap.add_argument("--scoring", default="STD", help="STD (these leagues are standard, non-PPR), HALF or PPR")
    ap.add_argument("--positions", default=DEFAULT_POSITIONS)
    ap.add_argument("--out", default=None, help="default: fantasypros_ros_<season>.json")
    args = ap.parse_args()
    out_path = args.out or f"fantasypros_ros_{args.season}.json"

    if not KEY:
        print("::error::FANTASYPROS_API_KEY is not set (Settings -> Secrets and variables -> Actions).")
        return 1

    prior = {}
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                prior = json.load(f)
        except Exception:
            prior = {}

    positions, players, failures, api_tier, last_updated = {}, [], [], None, None
    for pos in [p.strip().upper() for p in args.positions.split(",") if p.strip()]:
        info, rows, err = fetch_position(pos, args.season, args.scoring)
        time.sleep(PAUSE)
        if err:
            failures.append(pos)
            print(f"::warning::FantasyPros {pos}: {err}")
            carried = [p for p in (prior.get("players") or []) if p.get("pos") == pos]
            if carried:                                   # keep last week's rows rather than lose the position
                players += carried
                positions[pos] = dict((prior.get("positions") or {}).get(pos, {}), carried_over=True, error=err[:160])
            else:
                positions[pos] = {"error": err[:160]}
            continue
        api_tier = api_tier or info["api_tier"]
        last_updated = last_updated or info["last_updated"]
        positions[pos] = info
        players += [slim(r) for r in rows]
        flag = "" if info["complete"] else f"   PARTIAL ({info['returned']} of {info['count']}, tier={info['api_tier']}, limit={info['limit']})"
        print(f"  {pos:3} {info['returned']:4} of {info['count']:4} players, ROS updated {info['last_updated']}{flag}")

    fetched = [p for p, v in positions.items() if "count" in v]
    if not fetched:
        print("::error::No position returned usable ROS rankings; leaving any existing file untouched.")
        return 1

    # "complete" = every position has its FULL list, whether fetched fresh or carried over from last week.
    complete = all(v.get("complete") for v in positions.values())
    stale = sorted(p for p, v in positions.items() if v.get("carried_over"))
    truncated = [p for p in fetched if not positions[p].get("complete")]    # fresh but cut short by the key's cap
    doc = {
        "source": "FantasyPros Expert Consensus Rankings, rest of season",
        "attribution": ATTRIBUTION,
        "season": args.season, "scoring": args.scoring, "type": "ROS",
        "generated": datetime.now(timezone.utc).isoformat(),
        "last_updated": last_updated, "api_tier": api_tier,
        "complete": complete, "stale_positions": stale, "positions": positions, "players": players,
    }

    if truncated:
        sample = positions[truncated[0]]
        print(f"::warning::FantasyPros returned only part of the rankings (e.g. {truncated[0]}: {sample['returned']} of "
              f"{sample['count']}). The API reports this key as tier '{sample['api_tier']}' with limit {sample['limit']}. "
              f"Full rankings need a production (Premium) key.")
        if prior.get("complete"):
            print("::warning::Keeping the earlier COMPLETE file instead of overwriting it with partial data.")
            return 0
    if stale:
        print(f"::warning::Kept last week's rows for {', '.join(stale)} (fetch failed this run); the rest are fresh.")

    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, out_path)
    print(f"Wrote {out_path}: {len(players)} players, complete={complete}, api tier={api_tier}, ROS updated {last_updated}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
