#!/usr/bin/env python3
"""
yahoo_transactions_parse.py -- Yahoo league transactions pages -> FAAB bid history.

The Safari Shortcut saves each league's Transactions page(s) under
    yahoo_transactions_raw/<league>/all_<offset>.html.gz    (the "All Transactions" tab, 25 rows a page)
    yahoo_transactions_raw/<league>/faab_<offset>.html.gz   (the "FAB Offers" tab)
    yahoo_transactions_raw/<league>/page_1.html.gz          (legacy name for the first "all" page)
This turns them into yahoo_waiver_history_<year>.json, in the same shape the ESPN leagues'
waiver_history_<year>.json uses, so the cockpit's FAAB assistant can read both.

THREE SOURCES, COMBINED:
  all_<n>      "All Transactions": every completed move. Winning FAAB claims ("$N Waiver"), free-agent adds.
               Verified against real pages. Only the WINNING bid is shown.
  faab_<n>     "FAB Offers": one block per contested auction: "$N Winning Offer", then each LOSING offer as
               "<team> $M (Lower Offer | Lower waiver priority)", then "Awarded To: <team>". This is where the
               losing bids are. Parsed from the visible text so it tolerates markup changes; if a page's layout
               isn't recognized the run says so and quotes the first block it could not read.
  standings_0  "Standings": the "Waiver Bdgt" column is each team's exact REMAINING budget (authoritative; summing
               bids is only a cross-check) plus "Waiver" (priority) and "Moves".
Auctions seen in both All Transactions and FAB Offers are merged, so a win is never double counted.

WHAT YAHOO'S "ALL TRANSACTIONS" PAGE DOES AND DOESN'T SHOW (verified on real Kepners/Miami pages):
  * A completed FAAB claim: the added player is labelled "$N  Waiver" (N = the winning bid),
    with the dropped player labelled "To Waivers" in the same row.
  * A free-agent pickup: "Free Agent" (no bid). Not counted as a bid.
  * LOSING bids are NOT listed on the All Transactions tab (they come from FAB Offers, above).
  * Times have no year and no time zone. The year is inferred from the season; the zone defaults to
    America/New_York (only used to place a row in the right waiver week).

WEEKS: waiver weeks run Tuesday -> Monday, starting the Tuesday before the first NFL Thursday game
(--week1, default 2026-09-08). A claim processed early Wednesday morning belongs to the week being
played that Thursday, which matches how the ESPN history labels its claims.

BUDGET: the total FAAB budget is not on these pages. It comes from league_rules.json -> <league>.faab_budget;
if absent, it is the larger of 100 and the biggest "Waiver Bdgt" on the Standings page, flagged "budget_assumed".
Each team's SPENT figure is budget minus its Standings remaining balance whenever Standings was captured.
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
PAGE_RE = re.compile(r"^(?:(?P<kind>all|faab|standings)_(?P<off>\d+)|page_(?P<legacy>\d+))\.html(?:\.gz)?$")
TEAM_HREF_RE = re.compile(r"/f1/\d+/(\d+)")
BID_RE = re.compile(r"^\$\s*(\d+)\s+Waiver", re.I)
FAB_WIN_RE = re.compile(r"\$\s*(\d+)\s+Winning Offer", re.I)
LOSER_RE = re.compile(r"(?P<team>[^$()]+?)\s*\$\s*(?P<bid>\d+)\s*\((?P<why>[^)]*)\)")
STAMP_RE = re.compile(r"[A-Z][a-z]{2}\s+\d{1,2},\s*\d{1,2}:\d{2}\s*[ap]m", re.I)
POS_RE = re.compile(r"(?P<nfl>[A-Za-z]{2,4})\s+-\s+(?P<pos>[A-Z/]+)\s*$")
SAME_AUCTION_MS = 6 * 3600 * 1000   # an "All" win and a "FAB" win are the same auction if within 6 hours


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
    text = re.sub(r",(?=\S)", ", ", text)           # FAB Offers writes "Oct 7,3:55 am"
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



def norm(t):
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()


def team_key(name):
    """Team names differ only in quote style between pages (’ vs ')."""
    return norm(name).replace("\u2019", "'").replace("\u2018", "'").casefold()


def norm_player(name):
    n = team_key(name)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


def player_id_in(el):
    a = el.select_one("a[data-ys-playerid]")
    if a:
        return a.get("data-ys-playerid")
    for a in el.find_all("a", href=True):
        m = re.search(r"/nfl/players/(\d+)", a["href"])
        if m:
            return m.group(1)
    return None


def team_links_in(el):
    """{team_key(name): team_id} for every league-team link inside el."""
    out = {}
    for a in el.find_all("a", href=True):
        m = TEAM_HREF_RE.search(a["href"])
        if m and a.get_text(strip=True):
            out[team_key(a.get_text(strip=True))] = int(m.group(1))
    return out


def find_offer_blocks(soup):
    """The element holding each FAB offer. Markup-agnostic: prefer table rows; otherwise climb from the text."""
    blocks = []
    for tr in soup.find_all("tr"):
        txt = norm(tr.get_text(" ", strip=True))
        if len(FAB_WIN_RE.findall(txt)) == 1:
            blocks.append(tr)
    if blocks:
        return blocks
    seen = set()
    for node in soup.find_all(string=re.compile(r"Winning Offer", re.I)):
        el = node.parent
        while el is not None and el.name not in ("html", "body", "[document]"):
            txt = norm(el.get_text(" ", strip=True))
            if len(FAB_WIN_RE.findall(txt)) == 1 and STAMP_RE.search(txt):
                if id(el) not in seen:
                    seen.add(id(el)); blocks.append(el)
                break
            el = el.parent
    return blocks


def parse_offer_block(el, known_teams):
    """One contested auction -> dict, or None. Works from the block's visible text."""
    text = norm(el.get_text(" ", strip=True))
    m = FAB_WIN_RE.search(text)
    if not m:
        return None
    before, after = text[: m.start()].strip(), text[m.end():].strip()
    before = re.sub(r"^[^A-Za-z0-9]+", "", before)       # the "$" icon (and any other glyph) left of the player
    pm = POS_RE.search(before)
    if pm:
        name, nfl, pos = before[: pm.start()].strip(), pm.group("nfl"), pm.group("pos")
    else:
        name, nfl, pos = before, None, None
    parts = re.split(r"Awarded To:?", after, maxsplit=1, flags=re.I)
    offers_txt, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    sm = STAMP_RE.search(tail) or STAMP_RE.search(after)
    winner_name = norm(tail[: sm.start()]) if (sm and tail and STAMP_RE.search(tail)) else None
    ids = dict(known_teams)
    ids.update(team_links_in(el))
    links = [a for a in el.find_all("a", href=True) if TEAM_HREF_RE.search(a["href"]) and a.get_text(strip=True)]
    if not winner_name and links:
        winner_name = links[-1].get_text(strip=True)
    losers = []
    for lm in LOSER_RE.finditer(offers_txt):
        tname = norm(lm.group("team"))
        losers.append({"team_name": tname, "team_id": ids.get(team_key(tname)),
                       "bid": int(lm.group("bid")), "why": norm(lm.group("why"))})
    return {"player": {"id": player_id_in(el), "name": name, "position": pos, "nfl_team": nfl},
            "win_bid": int(m.group(1)), "winner_name": winner_name,
            "winner_id": ids.get(team_key(winner_name)) if winner_name else None,
            "stamp": sm.group(0) if sm else None, "losers": losers}


def parse_faab_offers(html, known_teams):
    """-> (auctions, unparsed_sample or None, n_blocks)"""
    soup = BeautifulSoup(html, "html.parser")
    blocks = find_offer_blocks(soup)
    auctions, bad = [], None
    for el in blocks:
        a = parse_offer_block(el, known_teams)
        if a and a["stamp"] and a["winner_id"] is not None:
            auctions.append(a)
        elif bad is None:
            bad = norm(el.get_text(" ", strip=True))[:300]
    return auctions, bad, len(blocks)


def _money(t):
    m = re.search(r"\$?\s*(\d+)", t or "")
    return int(m.group(1)) if m else None


def parse_standings(html):
    """{team_id: {team_name, remaining, waiver_rank, moves}} from the 'Waiver Bdgt' column, or None."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        header = None
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            labels = [norm(c.get_text(" ", strip=True)).lower() for c in cells]
            if any(("bdgt" in l or "budget" in l) and "waiver" in l for l in labels):
                header = labels
                break
        if not header:
            continue
        ib = next(i for i, l in enumerate(header) if ("bdgt" in l or "budget" in l) and "waiver" in l)
        iw = next((i for i, l in enumerate(header) if l == "waiver"), None)
        im = next((i for i, l in enumerate(header) if l == "moves"), None)
        out = {}
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) != len(header):
                continue                                  # division banner rows, the header itself
            a = tr.find("a", href=TEAM_HREF_RE)
            if not a:
                continue
            tid = int(TEAM_HREF_RE.search(a["href"]).group(1))
            rem = _money(cells[ib].get_text(" ", strip=True))
            if rem is None:
                continue
            out[tid] = {"team_name": a.get_text(strip=True), "remaining": rem,
                        "waiver_rank": _money(cells[iw].get_text(" ", strip=True)) if iw is not None else None,
                        "moves": _money(cells[im].get_text(" ", strip=True)) if im is not None else None}
        if out:
            return out
    return None


def same_player(a, b):
    if a.get("id") and b.get("id") and str(a["id"]) == str(b["id"]):
        return True
    return bool(a.get("name") and b.get("name") and norm_player(a["name"]) == norm_player(b["name"]))


def build_league(key, folder, season, week1, tz_name, budget, live_teams, budget_given=True):
    pages = list_pages(folder)
    all_pages = [(off, p) for kind, off, p in pages if kind == "all"]
    faab_pages = [(off, p) for kind, off, p in pages if kind == "faab"]
    stand_pages = [(off, p) for kind, off, p in pages if kind == "standings"]
    if not all_pages and not faab_pages:
        return None, f"[{key}] no transaction pages in {folder}"

    # ---------- team directory (ids <-> names), from the live rosters and from every page ----------
    known = {team_key(n): int(t) for t, n in (live_teams or {}).items() if n}

    # ---------- All Transactions: completed moves ----------
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
            known.setdefault(team_key(r["team_name"]), r["team_id"])
            sig = (r["team_id"], r["stamp"], tuple(x["id"] for x in r["adds"]), tuple(x["id"] for x in r["drops"]))
            if sig in seen:
                continue
            seen.add(sig)
            events.append(r)

    teams = {}
    for tid, name in (live_teams or {}).items():
        teams[int(tid)] = {"team_id": int(tid), "team_name": name, "manager": None, "faab_spent": 0,
                           "waiver_rank": None, "acquisitions": 0}

    def team_row(tid, name):
        return teams.setdefault(tid, {"team_id": tid, "team_name": name, "manager": None, "faab_spent": 0,
                                      "waiver_rank": None, "acquisitions": 0})

    wins, fa_adds = [], []
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
        t = team_row(tid, r["team_name"])
        adds, drops = r["adds"], r["drops"]
        for i, a in enumerate(adds):
            drop = drops[i] if i < len(drops) else None
            drop_ref = {"id": drop["id"], "name": drop["name"]} if drop else None
            add_ref = {"id": a["id"], "name": a["name"], "position": a["position"]}
            base = {"season": season, "week": wk, "date": int(dt.timestamp() * 1000), "team_id": tid,
                    "add": add_ref, "drop": drop_ref, "source": "yahoo"}
            t["acquisitions"] += 1
            if a["via"] == "waiver":
                wins.append(dict(base, type="WAIVER", status="EXECUTED", won=True, bid=a["bid"]))
            else:
                fa_adds.append(dict(base, type="FREEAGENT"))

    # ---------- FAB Offers: contested auctions with the losing offers ----------
    auctions, fab_bad, fab_blocks, fab_dupes = [], None, 0, 0
    sigs = set()
    for off, p in faab_pages:
        try:
            got, bad, nblocks = parse_faab_offers(read_page(p), known)
        except Exception as e:
            print(f"  [{key}] could not read {p.name}: {e}", file=sys.stderr)
            continue
        fab_blocks += nblocks
        fab_bad = fab_bad or bad
        for a in got:
            sig = (a["winner_id"], a["stamp"], norm_player(a["player"]["name"] or ""))
            if sig in sigs:
                fab_dupes += 1
                continue
            sigs.add(sig)
            auctions.append(a)

    claims, covered = [], set()
    for a in auctions:
        dt = parse_stamp(a["stamp"], season, tz_name)
        if dt is None:
            continue
        ms, wk = int(dt.timestamp() * 1000), waiver_week(dt, week1)
        oldest = dt if oldest is None or dt < oldest else oldest
        newest = dt if newest is None or dt > newest else newest
        add_ref = {"id": a["player"]["id"], "name": a["player"]["name"], "position": a["player"]["position"]}
        # the matching "All Transactions" win carries the dropped player; use it, and don't count it twice
        drop_ref = None
        for idx, w in enumerate(wins):
            if idx in covered or w["team_id"] != a["winner_id"]:
                continue
            if same_player(w["add"], add_ref) and abs(w["date"] - ms) <= SAME_AUCTION_MS:
                covered.add(idx); drop_ref = w["drop"]
                if not add_ref["id"]:
                    add_ref["id"] = w["add"]["id"]
                break
        base = {"season": season, "week": wk, "date": ms, "type": "WAIVER", "add": add_ref, "source": "yahoo-faab-offers"}
        claims.append(dict(base, status="EXECUTED", won=True, team_id=a["winner_id"], bid=a["win_bid"], drop=drop_ref))
        team_row(a["winner_id"], a["winner_name"])
        for l in a["losers"]:
            if l["team_id"] is None:
                continue
            why = l["why"].lower()
            code = "FAILED_LOWER_PRIORITY" if "priority" in why else ("FAILED_LOWER_OFFER" if "lower offer" in why else "FAILED_OTHER")
            team_row(l["team_id"], l["team_name"])
            claims.append(dict(base, status=code, won=False, team_id=l["team_id"], bid=l["bid"], drop=None, reason=l["why"]))
    claims += [w for idx, w in enumerate(wins) if idx not in covered]   # uncontested wins (no FAB Offers entry)
    for c in claims:
        if c["won"]:
            team_row(c["team_id"], None)["acquisitions"] += 0
    claims.sort(key=lambda c: (c["week"], c["date"], 0 if c["won"] else 1))
    fa_adds.sort(key=lambda c: (c["week"], c["date"]))

    # ---------- spending: from bids, then reconciled against Standings ----------
    bids_spent = {}
    for c in claims:
        if c["won"]:
            bids_spent[c["team_id"]] = bids_spent.get(c["team_id"], 0) + (c["bid"] or 0)
    standings, stand_note = None, None
    for off, p in stand_pages:
        try:
            standings = parse_standings(read_page(p))
        except Exception as e:
            print(f"  [{key}] could not read {p.name}: {e}", file=sys.stderr)
        if standings:
            break
    if stand_pages and not standings:
        stand_note = "standings page present but no 'Waiver Bdgt' table was recognized"

    budget_source = "league_rules.json" if budget_given else "assumed"
    if standings and not budget_given:
        top = max(v["remaining"] for v in standings.values())
        if top >= budget:        # at least one team untouched at the top figure: that IS the budget
            budget, budget_source = top, "largest 'Waiver Bdgt' on the Standings page"
    for tid, t in teams.items():
        t["faab_spent_from_bids"] = bids_spent.get(tid, 0)
        t["faab_spent"] = bids_spent.get(tid, 0)
    gap = []
    if standings:
        for tid, st in standings.items():
            t = team_row(tid, st["team_name"])
            t["team_name"] = t["team_name"] or st["team_name"]
            t["faab_remaining"] = st["remaining"]
            t["faab_spent"] = max(0, budget - st["remaining"])        # the authoritative figure
            t["waiver_rank"] = st["waiver_rank"]
            t["moves"] = st["moves"]
            if t["faab_spent"] > bids_spent.get(tid, 0):
                gap.append({"team_id": tid, "standings_spent": t["faab_spent"], "bids_spent": bids_spent.get(tid, 0)})
            elif t["faab_spent"] < bids_spent.get(tid, 0):
                gap.append({"team_id": tid, "standings_spent": t["faab_spent"], "bids_spent": bids_spent.get(tid, 0),
                            "note": "bids exceed the Standings balance (budget assumption may be too low)"})

    last_off = max(page_rows) if page_rows else 0
    complete = bool(oldest and oldest.date() < week1) or (bool(page_rows) and page_rows.get(last_off, 25) < 25)
    contested = sum(1 for a in auctions if a["losers"])
    out = {
        "platform": "yahoo", "faab": True, "budget": budget, "priorBudget": budget,
        "teams": {str(k): v for k, v in sorted(teams.items(), key=lambda kv: (kv[0] is None, kv[0]))},
        "claims": claims, "free_agent_adds": fa_adds,
        "source": "yahoo transactions pages (all moves + FAB offers + standings)",
        "pages": pages_ok, "faab_pages_seen": len(faab_pages), "standings_pages_seen": len(stand_pages),
        "rows_seen": len(events), "rows_unparsed": unparsed,
        "fab_auctions": len(auctions), "fab_contested": contested, "fab_blocks_unparsed": max(0, fab_blocks - len(auctions) - fab_dupes),
        "budget_source": budget_source, "standings_ok": bool(standings),
        "history_gap": gap,
        "oldest": oldest.isoformat() if oldest else None, "newest": newest.isoformat() if newest else None,
        "complete_history": complete and not gap,
    }
    notes = []
    if faab_pages and not auctions:
        notes.append("FAB Offers page(s) captured but no offers were recognized" + (f"; first block: {fab_bad!r}" if fab_bad else ""))
    elif out["fab_blocks_unparsed"]:
        notes.append(f"{out['fab_blocks_unparsed']} FAB Offers block(s) could not be read" + (f"; e.g. {fab_bad!r}" if fab_bad else ""))
    if stand_note:
        notes.append(stand_note)
    out["notes"] = notes
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
        league, err = build_league(key, folder, args.year, week1, args.tz, budget, live, budget_given=not assumed)
        if err:
            print(f"::warning::{err} -- keeping any earlier {key} history")
            continue
        league["budget_assumed"] = assumed and league["budget_source"] == "assumed"
        leagues_out[key] = league
        wrote.append(key)
        wins = [c for c in league["claims"] if c["won"]]
        lost = len(league["claims"]) - len(wins)
        print(f"[{key}] {len(wins)} FAAB wins + {lost} losing offers ({league['fab_contested']} contested auctions), "
              f"{len(league['free_agent_adds'])} free-agent adds; {league['rows_seen']} rows over {league['pages']} page(s); "
              f"oldest {league['oldest']}; complete_history={league['complete_history']}; "
              f"budget ${league['budget']} ({league['budget_source']}); standings_ok={league['standings_ok']}")
        for n in league["notes"]:
            print(f"::warning::[{key}] {n}")
        for g in league["history_gap"][:12]:
            tn = (league["teams"].get(str(g["team_id"])) or {}).get("team_name")
            print(f"  [{key}] spending check: {tn}: Standings says ${g['standings_spent']} spent, parsed bids total "
                  f"${g['bids_spent']}" + (f" ({g['note']})" if g.get("note") else " (older wins not captured yet)"))

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
