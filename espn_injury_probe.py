#!/usr/bin/env python3
"""
espn_injury_probe.py -- one-time reconnaissance: where does ESPN publish an injured player's EXPECTED RETURN?

Two families of sources are checked, because the answer is different for each:

  A. The FANTASY API this project already uses (lm-api-reads.fantasy.espn.com, with your league cookies).
     The python library the pulls use only exposes injuryStatus / injured, but the raw JSON may carry more.
     This pulls the raw player pool and lists every field NAME on injured players, plus any field anywhere in
     the entry whose name looks like a return/expected/timeline field (with one example value).

  B. ESPN's PUBLIC injury feeds (no login): the league-wide site feed, and the per-team core feed (which
     returns links that have to be followed). Third-party descriptions say these carry `returnDate`, filled
     in for only some players (serious injuries mostly). This measures how many, and for which statuses.

WHAT IS SAVED (espn_injury_probe.json): request path + status, response shape, field NAMES, counts and coverage,
and at most 4 example injury rows per source (player, status, injury type, return date -- all public
information). No cookies, no league data. Delete the file after use.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

YEAR = int(os.environ.get("YEAR", "2026"))
LEAGUE_ID = os.environ.get("ESPN_LEAGUE_ID", "").strip()
S2 = os.environ.get("ESPN_S2", "").strip()
SWID = os.environ.get("ESPN_SWID", "").strip()
FANTASY_BASE = os.environ.get("FANTASY_BASE", "https://lm-api-reads.fantasy.espn.com").rstrip("/")
SITE_BASE = os.environ.get("SITE_BASE", "https://site.api.espn.com").rstrip("/")
CORE_BASE = os.environ.get("CORE_BASE", "https://sports.core.api.espn.com").rstrip("/")
OUT = os.environ.get("EIP_OUT", "espn_injury_probe.json")
PAUSE = float(os.environ.get("EIP_PAUSE", "0.4"))
POOL = int(os.environ.get("EIP_POOL", "1500"))
SAMPLES = 4
HINT = re.compile(r"return|expect|eta\b|timeline|outlook|recover|weeksout|weeks_out|duration|severity", re.I)


def scrub(t):
    for secret in (S2, SWID):
        if secret:
            t = t.replace(secret, "***")
    return t


def fetch(url, headers=None, timeout=60):
    h = {"Accept": "application/json", "User-Agent": "draft-control-probe/1"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt == 1:
                time.sleep(3)
                continue
            return e.code, body, None
        except Exception as e:
            if attempt == 2:
                return None, "", scrub(str(e))[:200]
            time.sleep(2)
    return None, "", "unreachable"


def walk(obj, path="", depth=0, out=None):
    """Collect (path, value) for every key whose name looks like a return/expected/timeline field."""
    out = {} if out is None else out
    if depth > 7:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if HINT.search(str(k)):
                out.setdefault(p, v if not isinstance(v, (dict, list)) else json.dumps(v)[:120])
            walk(v, p, depth + 1, out)
    elif isinstance(obj, list):
        for v in obj[:50]:
            walk(v, path + "[]", depth + 1, out)
    return out


def field_types(d):
    return {k: ("null" if v is None else type(v).__name__) for k, v in d.items()}


# ------------------------------------------------------------------ A. the fantasy API
def probe_fantasy(results):
    if not (LEAGUE_ID and S2 and SWID):
        results.append({"source": "fantasy API", "skipped": "ESPN_LEAGUE_ID / ESPN_S2 / ESPN_SWID not all set"})
        return
    cookie = {"Cookie": f"espn_s2={S2}; SWID={SWID}"}
    for view in ("kona_player_info", "kona_playercard", "players_wl"):
        url = f"{FANTASY_BASE}/apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{LEAGUE_ID}?view={view}&scoringPeriodId=0"
        filt = {"players": {"limit": POOL, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        status, body, err = fetch(url, dict(cookie, **{"x-fantasy-filter": json.dumps(filt)}))
        rec = {"source": "fantasy API", "view": view, "status": status}
        time.sleep(PAUSE)
        if err or status != 200:
            rec["error"] = scrub(err or body)[:300]
            results.append(rec)
            continue
        try:
            data = json.loads(body)
        except Exception:
            rec["error"] = "not JSON"
            results.append(rec)
            continue
        entries = data.get("players") or []
        rec["players_returned"] = len(entries)
        player_keys, entry_keys, statuses, hits = {}, {}, {}, {}
        injured_n = 0
        for e in entries:
            if isinstance(e, dict):
                entry_keys.update(field_types(e))
                pl = e.get("player") or {}
                player_keys.update(field_types(pl))
                st = pl.get("injuryStatus")
                statuses[str(st)] = statuses.get(str(st), 0) + 1
                if pl.get("injured") or (st not in (None, "ACTIVE", "NORMAL")):
                    injured_n += 1
                    for p, v in walk(e).items():
                        h = hits.setdefault(p, {"example": v, "injured_players_with_it": 0})
                        h["injured_players_with_it"] += 1
        rec.update(injured_players=injured_n, injury_status_values=statuses,
                   entry_fields=entry_keys, player_fields=player_keys,
                   return_like_fields_on_injured_players=hits)
        results.append(rec)


# ------------------------------------------------------------------ B. the public feeds
def injury_row(r):
    """Normalise one ESPN public injury record, whichever feed it came from."""
    det = r.get("details") or {}
    ath = r.get("athlete") or {}
    typ = r.get("type") or {}
    return {
        "player": ath.get("displayName") or ath.get("fullName"),
        "pos": (ath.get("position") or {}).get("abbreviation") if isinstance(ath.get("position"), dict) else None,
        "status": r.get("status") if isinstance(r.get("status"), str) else (r.get("status") or {}).get("name"),
        "fantasyStatus": (det.get("fantasyStatus") or {}).get("abbreviation") if isinstance(det.get("fantasyStatus"), dict) else det.get("fantasyStatus"),
        "injury": det.get("type") or typ.get("description") or typ.get("name"),
        "returnDate": det.get("returnDate"),
        "date": r.get("date"),
    }


def summarize_rows(rows):
    by = {}
    for x in rows:
        k = x["fantasyStatus"] or x["status"] or "?"
        b = by.setdefault(str(k), {"injuries": 0, "with_returnDate": 0})
        b["injuries"] += 1
        b["with_returnDate"] += 1 if x["returnDate"] else 0
    withd = [x for x in rows if x["returnDate"]]
    return {"total": len(rows), "with_returnDate": len(withd), "by_status": by,
            "examples_with_returnDate": withd[:SAMPLES], "examples_without": [x for x in rows if not x["returnDate"]][:2]}


def probe_site(results):
    url = f"{SITE_BASE}/apis/site/v2/sports/football/nfl/injuries"
    status, body, err = fetch(url)
    time.sleep(PAUSE)
    rec = {"source": "public site feed", "url": url, "status": status}
    if err or status != 200:
        rec["error"] = (err or scrub(body))[:300]
        results.append(rec)
        return
    try:
        data = json.loads(body)
    except Exception:
        rec["error"] = "not JSON"
        results.append(rec)
        return
    rec["top_level_keys"] = list(data.keys())
    rows, raw_sample = [], None
    for team in data.get("injuries") or []:
        for r in team.get("injuries") or []:
            raw_sample = raw_sample or r
            rows.append(injury_row(r))
    rec.update(summarize_rows(rows))
    if raw_sample is not None:
        rec["raw_record_fields"] = field_types(raw_sample)
        rec["raw_details_fields"] = field_types(raw_sample.get("details") or {})
    results.append(rec)


def https_ref(ref):
    """ESPN's core feed hands back http:// links to its own host; use https for those (and leave others alone)."""
    return ref.replace("http://sports.core.api.espn.com", "https://sports.core.api.espn.com")


def probe_core(results):
    """The per-team core feed returns links; follow them for the first team that has any."""
    rows, tried, raw_sample, sample_url = [], [], None, None
    for team_id in (1, 2, 3, 4, 5, 6):
        url = f"{CORE_BASE}/v2/sports/football/leagues/nfl/teams/{team_id}/injuries?lang=en&region=us&limit=100"
        status, body, err = fetch(url)
        time.sleep(PAUSE)
        tried.append({"team": team_id, "status": status})
        if err or status != 200:
            continue
        try:
            items = json.loads(body).get("items") or []
        except Exception:
            continue
        sample_url = sample_url or url
        for it in items[:8]:
            ref = it.get("$ref") if isinstance(it, dict) else None
            if not ref:
                continue
            s2, b2, e2 = fetch(https_ref(ref))
            time.sleep(PAUSE / 2)
            if e2 or s2 != 200:
                continue
            try:
                r = json.loads(b2)
            except Exception:
                continue
            raw_sample = raw_sample or r
            row = injury_row(r)
            if not row["player"] and isinstance(r.get("athlete"), dict) and r["athlete"].get("$ref"):
                s3, b3, e3 = fetch(https_ref(r["athlete"]["$ref"]))
                if not e3 and s3 == 200:
                    try:
                        a = json.loads(b3); row["player"] = a.get("displayName") or a.get("fullName")
                    except Exception:
                        pass
            rows.append(row)
        if len(rows) >= 12:
            break
    rec = {"source": "public core feed (per team, links followed)", "url": sample_url, "teams_tried": tried}
    rec.update(summarize_rows(rows))
    if raw_sample is not None:
        rec["raw_record_fields"] = field_types(raw_sample)
        rec["raw_details_fields"] = field_types(raw_sample.get("details") or {})
    results.append(rec)


def main():
    results = []
    probe_fantasy(results)
    probe_site(results)
    probe_core(results)
    for r in results:
        label = r.get("view") or r.get("source")
        print(f"  {str(r.get('status')):>4}  {r.get('source')} {r.get('view', '')}  "
              f"{('injured=' + str(r['injured_players'])) if 'injured_players' in r else ''}"
              f"{('total=' + str(r['total']) + ' withReturnDate=' + str(r['with_returnDate'])) if 'total' in r else ''}"
              f"{(' ERR ' + r['error'][:80]) if r.get('error') else ''}{(' ' + r['skipped']) if r.get('skipped') else ''}")
    doc = {"generated": datetime.now(timezone.utc).isoformat(), "year": YEAR, "results": results}
    with open(OUT, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
