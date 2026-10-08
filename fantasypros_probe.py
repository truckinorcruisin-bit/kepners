#!/usr/bin/env python3
"""
fantasypros_probe.py -- one-time reconnaissance of the FantasyPros API.

WHY: the public docs name the endpoints but not every parameter (for example how to ask for
rest-of-season rankings, or which external IDs the Players endpoint carries), and this project can't
reach api.fantasypros.com from its development sandbox. So this runs inside GitHub Actions with your key
and writes fantasypros_probe.json: for each request tried, the status, the response's shape, and the
server's own error text when a parameter is wrong. That is enough to write the real pull script.

WHAT IT RECORDS (and nothing more):
  * the request path + parameters (never the key)
  * HTTP status, and rate-limit headers if the server sends any
  * the response's top-level keys, the number of rows, each row field's NAME and TYPE
  * at most SAMPLE_ROWS example rows per request (a few lines; needed to see name formats and ID fields)
  * for a rejected request, the first 300 characters of the server's reply
It does NOT save the full rankings, projections or player lists. Delete fantasypros_probe.json after use.

THE KEY: read from the FANTASYPROS_API_KEY environment variable (a GitHub Actions secret), sent only in the
x-api-key header, and scrubbed from anything printed or written.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("FANTASYPROS_BASE", "https://api.fantasypros.com/public/v2/json").rstrip("/")
SEASON = int(os.environ.get("FP_SEASON", "2026"))
WEEK = int(os.environ.get("FP_WEEK", "5"))
SAMPLE_ROWS = 3
PAUSE = float(os.environ.get("FP_PAUSE", "0.6"))     # seconds between calls, to stay well inside rate limits
OUT = os.environ.get("FP_OUT", "fantasypros_probe.json")
KEY = os.environ.get("FANTASYPROS_API_KEY", "").strip()
RATE_HEADERS = ("retry-after",)


def scrub(text):
    return text.replace(KEY, "***") if KEY else text


def row_list(payload):
    """The list of records in a response, whatever key it sits under."""
    if isinstance(payload, list):
        return "(top level)", payload
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return k, v
        for k, v in payload.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    if isinstance(vv, list) and vv and isinstance(vv[0], dict):
                        return f"{k}.{kk}", vv
    return None, []


def typename(v):
    return "null" if v is None else type(v).__name__


def call(path, params):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": KEY, "Accept": "application/json",
                                               "User-Agent": "draft-control-probe/1"})
    rec = {"path": path, "params": params}
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body, status, headers = r.read(), r.status, r.headers
            break
        except urllib.error.HTTPError as e:
            body, status, headers = e.read(), e.code, e.headers
            if status in (429, 500, 502, 503, 504) and attempt == 1:
                time.sleep(3)
                continue
            break
        except Exception as e:                                   # network trouble
            rec.update(status=None, error=scrub(str(e))[:200])
            return rec
    rec["status"] = status
    rl = {h: headers.get(h) for h in headers.keys() if "ratelimit" in h.lower() or h.lower() in RATE_HEADERS}
    if rl:
        rec["rate_limit_headers"] = rl
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except Exception:
        rec["error"] = scrub(text)[:300]
        return rec
    if status >= 400:
        rec["error"] = scrub(json.dumps(payload))[:300]
        return rec
    where, rows = row_list(payload)
    rec["top_level_keys"] = list(payload.keys()) if isinstance(payload, dict) else "(list)"
    if isinstance(payload, dict):
        # small scalar metadata next to the rows (week, season, scoring...) is useful, and harmless
        rec["meta"] = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 80}
    rec["rows_at"], rec["row_count"] = where, len(rows)
    if rows:
        rec["fields"] = {k: typename(v) for k, v in rows[0].items()}
        rec["sample_rows"] = [json.loads(scrub(json.dumps(r))) for r in rows[:SAMPLE_ROWS]]
    return rec


def plan():
    s = f"/nfl/{SEASON}"
    c = f"{s}/consensus-rankings"
    p = f"{s}/projections"
    ros_tries = [{"type": "ros"}, {"type": "ROS"}, {"type": "rest-of-season"}, {"week": "0"}, {"week": "ros"}]
    items = [
        # --- consensus rankings: the main prize. Standard scoring (non-PPR) is what these leagues use. ---
        (c, {"position": "ALL", "scoring": "STD"}),
        (c, {"position": "RB", "scoring": "STD"}),
        (c, {"position": "RB", "scoring": "STD", "week": str(WEEK)}),
    ]
    items += [(c, dict({"position": "RB", "scoring": "STD"}, **t)) for t in ros_tries]
    items += [
        # --- projections: weekly and rest-of-season ---
        (p, {"position": "RB", "scoring": "STD"}),
        (p, {"position": "RB", "scoring": "STD", "week": str(WEEK)}),
        (p, {"position": "RB", "scoring": "STD", "type": "ros"}),
        (p, {"position": "RB", "scoring": "STD", "week": "0"}),
        (p, {"position": "RB", "scoring": "STD", "week": "ros"}),
        # --- players (IDs + external cross-references), injuries ---
        ("/nfl/players", {}),
        ("/nfl/players", {"position": "RB"}),
        ("/nfl/injuries", {"season": str(SEASON), "week": str(WEEK)}),
    ]
    return items


def main():
    if not KEY:
        print("::error::FANTASYPROS_API_KEY is not set. Add it under Settings -> Secrets and variables -> Actions.")
        return 1
    results = []
    for path, params in plan():
        rec = call(path, params)
        results.append(rec)
        shown = rec.get("status")
        extra = f"{rec.get('row_count', '')} rows" if rec.get("row_count") is not None and "row_count" in rec else (rec.get("error", "")[:90])
        print(f"  {str(shown):>4}  {path}  {json.dumps(params)}  {extra}")
        time.sleep(PAUSE)

    statuses = [r.get("status") for r in results]
    ok = sum(1 for s in statuses if s == 200)
    denied = sum(1 for s in statuses if s in (401, 403))
    doc = {"generated": datetime.now(timezone.utc).isoformat(), "season": SEASON, "week": WEEK,
           "summary": {"requests": len(results), "ok": ok, "denied_401_403": denied,
                       "other_errors": len(results) - ok - denied},
           "results": results}
    with open(OUT, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"Wrote {OUT}: {ok}/{len(results)} requests succeeded.")
    if denied == len(results):
        print("::error::Every request was refused (401/403). The key is invalid, inactive, or not entitled to this API.")
        return 1
    if ok == 0:
        print("::warning::No request succeeded; fantasypros_probe.json still records what the server said.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
