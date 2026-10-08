#!/usr/bin/env python3
"""
yahoo_transactions_parse.py -- Yahoo league transactions pages -> FAAB bid history.

The Safari Shortcut saves each league's Transactions page(s) under
    yahoo_transactions_raw/<league>/all_<offset>.html.gz    (the "All Transactions" tab, 25 rows a page)
    yahoo_transactions_raw/<league>/faab_<offset>.html.gz   (the "FAB Offers" tab)
    yahoo_transactions_raw/<league>/page_1.html.gz          (legacy name for the first "all" page)
This turns them into yahoo_waiver_history_<year>.json, in the same shape the ESPN leagues'
waiver_history_<year>.json uses, so the cockpit's FAAB assistant can read both.

WHAT YAHOO'S PAGE DOES AND DOESN'T SHOW (verified on real Kepners/Miami pages):
  * A completed FAAB claim: the added player is labelled "$N  Waiver" (N = the winning bid),
    with the dropped player labelled "To Waivers" in the same row.
  * A free-agent pickup: "Free Agent" (no bid). Not counted as a bid.
  * LOSING bids are NOT listed on the All Transactions tab, so every claim here is a WINNER and each
    "auction" looks uncontested. The FAAB model's competition estimate is therefore weaker for
    these leagues than for ESPN's. (The "FAB Offers" tab is captured too; see "faab_pages_seen".)
  * Times have no year and no time zone. The year is inferred from the season; the zone defaults to
    America/New_York (only used to place a row in the right waiver week).

WEEKS: waiver weeks run Tuesday -> Monday, starting the Tuesday before the first NFL Thursday game
(--week1, default 2026-09-08). A claim processed early Wednesday morning belongs to the week being
played that Thursday, which matches how the ESPN history labels its claims.

BUDGET: the page does not show the FAAB budget. It comes from league_rules.json -> <league>.faab_budget;
if absent, 100 is ASSUMED and flagged in the output ("budget_assumed": true).
"""
import argparse
import gzip
import json
import re
import sys
from datetime import datetime, timezone, date
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: requires 'beautifulsoup4'. Install with: pip install beautifulsoup4", file=sys.stderr)
    raise

try:
    from zoneinfo import ZoneInfo
except ImportError:      # pragma: no cover
    ZoneInfo = None

DEFAULT_LEAGUES = ("kepners", "miami")
DEFAULT_TZ = "America/New_York"
DEFAULT_BUDGET = 100
PAGE_RE = re.compile(r"^(?:(?P<kind>all|faab)_(?P<off>\d+)|page_(?P<legacy>\d+))\.html(?:\.gz)?$")
TEAM_HREF_RE = re.compile(r"/f1/\d+/(\d+)")
BID_RE = re.compile(r"^\$\s*(\d+)\s+Waiver", re.I)


def read_page(path):
    data = Path(path).read_bytes()
    if str(path).endswith(".gz"):
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def list_pages(folder):
    """[(kind, offset, path)] sorted by kind then offset. Legacy page_1 counts as all/0."""
    out = []
    folder = Path(folder)
    if not folder.is_dir():
        return out
    chosen = {}
    for p in folder.iterdir():
        m = PAGE_RE.match(p.name)
        if not m:
            continue
        if m.group("legacy"):
            kind, off = "all", 0
        else:
            kind, off = m.group("kind"), int(m.group("off"))
        k = (kind, off)
        # Deterministic preference when two files claim the same page: the new all_/faab_ name beats
        # the legacy page_1, and the compressed copy beats a plain .html (it is the newer scrape).
        prio = (0 if m.group("legacy") else 2) + (1 if p.name.endswith(".gz") else 0)
        if k not in chosen or prio > chosen[k][0]:
            chosen[k] = (prio, p)
    for (kind, off), (_, p) in sorted(chosen.items()):
        out.append((kind, off, p))
    return out


def parse_stamp(text, season, tz_name):
    """'Oct 7, 5:23 am' -> aware datetime. Months before June belong to the NEXT calendar year."""
    text = re.sub(r"\s+", " ", text.strip())
    for fmt in ("%b %d, %I:%M %p", "%b %d %I:%M %p"):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            dt = None
    if dt is None:
        return None
    year = season + 1 if dt.month < 6 else season
    dt = dt.replace(year=year)
    if ZoneInfo is not None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    else:                                             # pragma: no cover
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def waiver_week(dt, week1_tuesday):
    d = dt.date() if isinstance(dt, datetime) else dt
    return max(1, (d - week1_tuesday).days // 7 + 1)


def parse_player(div):
    label_el = div.find("h6")
    label = re.sub(r"\s+", " ", label_el.get_text(" ", strip=True)) if label_el else ""
    link = div.find("a", attrs={"target": "sports"}) or div.find("a")
    name = link.get_text(strip=True) if link else None
    pos_el = div.select_one(".F-position")
    nfl_team = position = None
    if pos_el:
        txt = pos_el.get_text(strip=True)
        if " - " in txt:
            nfl_team, position = [x.strip() for x in txt.split(" - ", 1)]
        else:
            position = txt
    note = div.select_one("a[data-ys-playerid]")
    pid = note.get("data-ys-playerid") if note else None
    return {"label": label, "id": pid, "name": name, "position": position, "nfl_team": nfl_team}


def parse_rows(html):
    """Yield one dict per transaction row. Non add/drop rows (trades etc.) come back with kind='other'."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="Tst-transaction-table")
    if table is None:
        return None
    rows = []
    for tr in table.find_all("tr"):
        a = tr.select_one("a.Tst-team-name")
        stamp_el = tr.select_one(".F-timestamp")
        if not a or not stamp_el:
            rows.append({"kind": "other", "text": tr.get_text(" ", strip=True)[:120]})
            continue
        m = TEAM_HREF_RE.search(a.get("href", ""))
        adds, drops, unknown = [], [], []
        for div in tr.select("div.Pbot-xs"):
            p = parse_player(div)
            lab = p["label"]
            if BID_RE.match(lab):
                p["bid"] = int(BID_RE.match(lab).group(1)); p["via"] = "waiver"; adds.append(p)
            elif lab.lower() == "free agent":
                p["via"] = "free_agent"; adds.append(p)
            elif lab.lower().startswith("to "):
                p["to"] = lab[3:]; drops.append(p)
            else:
                unknown.append(p)
        kind = "move" if (adds or drops) and not unknown else "other"
        rows.append({"kind": kind, "team_id": int(m.group(1)) if m else None, "team_name": a.get_text(strip=True),
                     "stamp": stamp_el.get_text(strip=True), "adds": adds, "drops": drops,
                     "unknown": unknown})
    return rows


def build_league(key, folder, season, week1, tz_name, budget, live_teams):
    pages = list_pages(folder)
    all_pages = [(off, p) for kind, off, p in pages if kind == "all"]
    faab_pages = [(off, p) for kind, off, p in pages if kind == "faab"]
    if not all_pages:
        return None, f"[{key}] no 'all' transaction pages in {folder}"

    seen, events, unparsed, pages_ok, page_rows = set(), [], 0, 0, {}
    for off, p in all_pages:
        try:
            rows = parse_rows(read_page(p))
        except Exception as e:
            print(f"  [{key}] could not read {p.name}: {e}", file=sys.stderr)
            continue
        if rows is None:
            print(f"  [{key}] {p.name}: no transactions table (login page or layout change?)", file=sys.stderr)
            continue
        pages_ok += 1
        page_rows[off] = len(rows)
        for r in rows:
            if r["kind"] != "move":
                unparsed += 1
                continue
            sig = (r["team_id"], r["stamp"], tuple(x["id"] for x in r["adds"]), tuple(x["id"] for x in r["drops"]))
            if sig in seen:
                continue
            seen.add(sig)
            events.append(r)
    if not pages_ok:
        return None, f"[{key}] no readable transaction pages"

    claims, fa_adds, teams = [], [], {}
    for tid, name in (live_teams or {}).items():
        teams[int(tid)] = {"team_id": int(tid), "team_name": name, "manager": None, "faab_spent": 0,
                           "waiver_rank": None, "acquisitions": 0}
    oldest = newest = None
    for r in events:
        dt = parse_stamp(r["stamp"], season, tz_name)
        if dt is None:
            unparsed += 1
            continue
        oldest = dt if oldest is None or dt < oldest else oldest
        newest = dt if newest is None or dt > newest else newest
        wk = waiver_week(dt, week1)
        tid = r["team_id"]
        t = teams.setdefault(tid, {"team_id": tid, "team_name": r["team_name"], "manager": None,
                                   "faab_spent": 0, "waiver_rank": None, "acquisitions": 0})
        t["team_name"] = t["team_name"] or r["team_name"]
        adds, drops = r["adds"], r["drops"]
        for i, a in enumerate(adds):
            drop = drops[i] if i < len(drops) else None
            drop_ref = {"id": drop["id"], "name": drop["name"]} if drop else None
            add_ref = {"id": a["id"], "name": a["name"], "position": a["position"]}
            base = {"season": season, "week": wk, "date": int(dt.timestamp() * 1000), "team_id": tid,
                    "add": add_ref, "drop": drop_ref, "source": "yahoo"}
            t["acquisitions"] += 1
            if a["via"] == "waiver":
                claims.append(dict(base, type="WAIVER", status="EXECUTED", won=True, bid=a["bid"]))
                t["faab_spent"] += a["bid"]
            else:
                fa_adds.append(dict(base, type="FREEAGENT"))

    claims.sort(key=lambda c: (c["week"], c["date"]))
    fa_adds.sort(key=lambda c: (c["week"], c["date"]))
    # History is complete once the oldest row we saw predates the season, or a page came back short.
    last_off = max(page_rows) if page_rows else 0
    complete = bool(oldest and oldest.date() < week1) or (page_rows.get(last_off, 25) < 25)
    out = {
        "platform": "yahoo", "faab": True, "budget": budget, "priorBudget": budget,
        "teams": {str(k): v for k, v in sorted(teams.items())},
        "claims": claims, "free_agent_adds": fa_adds,
        "source": "yahoo transactions page (winning bids only)",
        "pages": pages_ok, "faab_pages_seen": len(faab_pages),
        "rows_seen": len(events), "rows_unparsed": unparsed,
        "oldest": oldest.isoformat() if oldest else None, "newest": newest.isoformat() if newest else None,
        "complete_history": complete,
    }
    return out, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--leagues", default=",".join(DEFAULT_LEAGUES))
    ap.add_argument("--root", default="yahoo_transactions_raw")
    ap.add_argument("--out", default=None, help="default: yahoo_waiver_history_<year>.json")
    ap.add_argument("--rules", default="league_rules.json")
    ap.add_argument("--week1", default="2026-09-08", help="Tuesday starting NFL week 1's waiver week")
    ap.add_argument("--tz", default=DEFAULT_TZ)
    args = ap.parse_args()

    out_path = Path(args.out or f"yahoo_waiver_history_{args.year}.json")
    week1 = date.fromisoformat(args.week1)
    try:
        rules = json.loads(Path(args.rules).read_text())
    except Exception:
        rules = {}

    prior = {}
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text())
        except Exception:
            prior = {}
    leagues_out = dict((prior.get("leagues") or {}))
    wrote = []

    for key in [k.strip() for k in args.leagues.split(",") if k.strip()]:
        folder = Path(args.root) / key
        if not folder.is_dir():
            print(f"[{key}] no folder {folder} -- skipping")
            continue
        budget = (rules.get(key) or {}).get("faab_budget")
        assumed = budget is None
        budget = DEFAULT_BUDGET if assumed else budget
        live = {}
        live_file = Path(f"{key}_live_rosters.json")
        if live_file.exists():
            try:
                live = {tid: t.get("team_name") for tid, t in (json.loads(live_file.read_text()).get("teams") or {}).items()}
            except Exception:
                live = {}
        league, err = build_league(key, folder, args.year, week1, args.tz, budget, live)
        if err:
            print(f"::warning::{err} -- keeping any earlier {key} history")
            continue
        league["budget_assumed"] = assumed
        leagues_out[key] = league
        wrote.append(key)
        print(f"[{key}] {len(league['claims'])} FAAB wins, {len(league['free_agent_adds'])} free-agent adds, "
              f"{league['rows_seen']} rows over {league['pages']} page(s); "
              f"oldest {league['oldest']}; complete_history={league['complete_history']}; "
              f"budget ${budget}{' (ASSUMED)' if assumed else ''}")

    if not wrote:
        print("Nothing parsed; leaving the output file as it was.")
        return
    doc = {"year": args.year, "generated": datetime.now(timezone.utc).isoformat(), "leagues": leagues_out}
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
    tmp.replace(out_path)
    print(f"Wrote {out_path} ({', '.join(wrote)})")


if __name__ == "__main__":
    main()
