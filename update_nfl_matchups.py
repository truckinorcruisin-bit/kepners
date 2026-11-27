#!/usr/bin/env python3
"""
update_nfl_matchups.py
NFL schedule + per-position matchup "bumps" for the fantasy-playoff weeks.

TWO SOURCES, DELIBERATELY SEPARATE:
  1. ESPN fantasy API  (view=proTeamSchedules_wl)  -> the full-season schedule: opponent, home/away,
     bye week for every NFL team.  Same host the other ESPN pulls already use.
  2. FantasyTeamAdvice "Playoff Strength of Schedule" public pages -> per-position SAFPA
     (Schedule-Adjusted Fantasy Points Allowed) for each team's opponent in weeks 14-17.  SAFPA is the
     actual points-per-game bump a matchup gives that position, already adjusted for opponent quality.
     NOTE: FTA computes it from PPR scoring.  Our leagues are standard, so treat the numbers as a
     reliable *ranking and direction*, and a slight overstatement of the size for RB/WR/TE.

If either source changes shape the script fails LOUDLY and writes nothing, rather than committing
garbage.  `--probe` writes the raw responses to probe_out/ and prints the structure it sees, so a shape
change can be diagnosed from the Actions log alone.

OUTPUT (normal run):  nfl_schedule_<year>.json   matchup_ratings_<year>.json
USAGE:   python update_nfl_matchups.py [year] [--probe]
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

YEAR = next((int(a) for a in sys.argv[1:] if a.isdigit()), 2026)
PROBE = "--probe" in sys.argv
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 kepners-draft-control/1.0")
PAUSE = 6.0            # politeness gap between FTA page fetches (+ up to 3s random jitter)
BACKOFF = [10, 30, 60, 120]   # waits after a transient failure (503/429/network); Retry-After wins if sent
TRANSIENT = {429, 500, 502, 503, 504}
FTA_BASE = "https://fantasyteamadvice.com/nfl/strength-of-schedule/playoff"
FTA_WEEKS = [14, 15, 16, 17]
# tab name -> our position key.  "" is the combined/overall tab.  On the DST tab FTA grades a team's OWN
# defense against the offense it faces (positive = good for that team's D/ST).  That direction is checked
# automatically every run (see dst_direction_check) because it's the one tab that reads the other way round.
# K is left out for now.
FTA_TABS = {"": "ALL", "qb": "QB", "rb": "RB", "wr": "WR", "te": "TE", "dst": "DEF"}
OPTIONAL_POS = {"DEF"}
ESPN_URL = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}"
            "?view=proTeamSchedules_wl")

NAMES = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}
TEAMS = sorted(set(NAMES.values()))
ALIASES = {"LVR": "LV", "JAC": "JAX", "WSH": "WAS", "LA": "LAR", "GNB": "GB", "KAN": "KC",
           "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB", "ARZ": "ARI", "OAK": "LV",
           "SD": "LAC", "STL": "LAR"}


def norm_team(t):
    """Canonical 2-3 letter team key (matches TEAM_BYE_2026 in convert_bigboard.py)."""
    if t is None:
        return None
    t = str(t).strip().upper()
    t = ALIASES.get(t, t)
    return t if t in TEAMS else None


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def load_prev():
    """The last good matchup file, if any. Used as a per-position fallback when a page won't load."""
    try:
        return json.load(open(f"matchup_ratings_{YEAR}.json"))
    except Exception:                                                  # noqa: BLE001
        return None


SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})


def http_get(url, tries=5, as_json=False):
    """GET with patient retries. A 503/429 from a public site is almost always 'slow down' or a brief
    outage, so wait it out (10s, 30s, 60s, 120s, or the server's Retry-After) instead of failing in 6
    seconds. Non-transient errors (403/404) get one retry, then fail."""
    last = None
    for i in range(tries):
        wait = BACKOFF[min(i, len(BACKOFF) - 1)]
        try:
            r = SESSION.get(url, timeout=40)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            last = f"HTTP {r.status_code}"
            ra = r.headers.get("Retry-After", "")
            if ra.isdigit():
                wait = min(int(ra), 180)
            if r.status_code not in TRANSIENT and i >= 1:
                break
        except Exception as e:                                       # noqa: BLE001
            last = str(e)
        if i < tries - 1:
            print(f"  retry {i + 1}/{tries - 1} for {url.split('?')[0]} after {last}; waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")


# ---------------------------------------------------------------------------------------------
# 1. ESPN schedule
# ---------------------------------------------------------------------------------------------
def parse_espn(data):
    """ESPN proTeamSchedules_wl -> {TEAM: {"bye": int|None, "games": {"1": {"opp","home"}}}}.
    Tolerant of small shape changes; raises ValueError (with what it saw) if it can't find teams."""
    pts = (data.get("settings") or {}).get("proTeams") or data.get("proTeams")
    if not pts:
        raise ValueError(f"no proTeams in ESPN response; top-level keys = {list(data)[:12]}, "
                         f"settings keys = {list((data.get('settings') or {}))[:12]}")
    id_to_abbr = {}
    for t in pts:
        a = norm_team(t.get("abbrev"))
        if a:
            id_to_abbr[t.get("id")] = a
    teams = {}
    for t in pts:
        me = id_to_abbr.get(t.get("id"))
        if not me:
            continue                                   # the id-0 "no team" placeholder etc.
        games = {}
        for wk, glist in (t.get("proGamesByScoringPeriod") or {}).items():
            for g in glist or []:
                h, a = g.get("homeProTeamId"), g.get("awayProTeamId")
                if me == id_to_abbr.get(h) and id_to_abbr.get(a):
                    games[str(wk)] = {"opp": id_to_abbr[a], "home": True}
                elif me == id_to_abbr.get(a) and id_to_abbr.get(h):
                    games[str(wk)] = {"opp": id_to_abbr[h], "home": False}
        bye = t.get("byeWeek") or None
        if not bye and games:
            missing = [w for w in range(1, 19) if str(w) not in games]
            bye = missing[0] if len(missing) == 1 else None
        teams[me] = {"bye": bye, "games": games}
    return teams


# ---------------------------------------------------------------------------------------------
# 2. FantasyTeamAdvice playoff-SoS pages
# ---------------------------------------------------------------------------------------------
CELL_TITLE = re.compile(r"^\s*(vs|@)\s+([A-Za-z]{2,4})\s*[\u2014\u2013\-]+\s*SAFPA:\s*([+-]?\d+(?:\.\d+)?)", re.I)
CELL_TEXT = re.compile(r"^\s*(vs|@)\s+([A-Za-z]{2,4})\b.*?SAFPA:\s*([+-]?\d+(?:\.\d+)?)", re.I | re.S)


def identify_team(text):
    low = text.lower()
    for name, ab in NAMES.items():
        if name.lower() in low:
            return ab
    for tok in re.findall(r"[A-Za-z]{2,4}", text):
        ab = norm_team(tok)
        if ab:
            return ab
    return None


def parse_cell(td):
    """One schedule cell -> None (unparseable), 'BYE', or {"opp","home","safpa"}."""
    txt = td.get_text(" ", strip=True)
    if re.fullmatch(r"\W*BYE\W*", txt, re.I):
        return "BYE"
    for a in td.find_all("a"):
        m = CELL_TITLE.match(a.get("title") or "")
        if m:
            opp = norm_team(m.group(2))
            if opp:
                return {"opp": opp, "home": m.group(1).lower() == "vs", "safpa": float(m.group(3))}
    m = CELL_TEXT.match(txt)
    if m:
        opp = norm_team(m.group(2))
        if opp:
            return {"opp": opp, "home": m.group(1).lower() == "vs", "safpa": float(m.group(3))}
    return None


def parse_fta(html):
    """FTA playoff-SoS page -> {TEAM: {"bye": int|None, "weeks": {"15": {opp,home,safpa}}}}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    best = None
    for tbl in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in tbl.find_all("th")]
        wk_cols = [h for h in heads if re.search(r"\b(?:Week|W)\s*\d{1,2}\b", h)]
        score = len(wk_cols) + (2 if any("Bye" in h for h in heads) else 0)
        if wk_cols and (best is None or score > best[0]):
            best = (score, tbl, heads)
    if not best:
        raise ValueError("no schedule table with week columns found on the page")
    _, tbl, heads = best
    # column index -> week number
    col_week = {}
    for i, h in enumerate(heads):
        m = re.search(r"\b(?:Week|W)\s*(\d{1,2})\b", h)
        if m:
            col_week[i] = int(m.group(1))
    bye_col = next((i for i, h in enumerate(heads) if "Bye" in h), None)
    out = {}
    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        # header cells and row cells normally line up 1:1; if a leading column is a <th> in the rows
        # (or the reverse) shift by the difference so week columns still land on the right cell.
        off = len(tds) - len(heads)
        if off not in (-1, 0, 1):
            continue
        # team: first cell that resolves to a team
        team = None
        for td in tds[:3]:
            team = identify_team(td.get_text(" ", strip=True)) or identify_team(
                " ".join(img.get("alt", "") for img in td.find_all("img")))
            if team:
                break
        if not team:
            continue
        weeks, bye = {}, None
        for i, wk in col_week.items():
            i += off
            if i < 0 or i >= len(tds):
                continue
            c = parse_cell(tds[i])
            if c == "BYE":
                bye = wk
            elif c:
                weeks[str(wk)] = c
        if bye_col is not None and 0 <= bye_col + off < len(tds):
            m = re.search(r"\d+", tds[bye_col + off].get_text(" ", strip=True))
            if m:
                bye = int(m.group(0))
        out[team] = {"bye": bye, "weeks": weeks}
    return out


def label_cuts(values):
    """Quantile cut points for colouring: bottom 15% avoid, next 20% unfavorable, middle 30% average,
    next 20% favorable, top 15% smash.  Computed per position so each position's palette is meaningful."""
    v = sorted(values)
    if len(v) < 10:
        return None
    q = lambda p: v[min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))]
    return [q(0.15), q(0.35), q(0.65), q(0.85)]


def label_for(x, cuts):
    if cuts is None:
        return "average"
    if x <= cuts[0]:
        return "avoid"
    if x <= cuts[1]:
        return "unfavorable"
    if x < cuts[2]:
        return "average"
    if x < cuts[3]:
        return "favorable"
    return "smash"


def offense_strength():
    """Projected fantasy points per week of each NFL team's offense (QB1+RB1-2+WR1-3+TE1), from the ROS
    projections already in inseason_<year>.json (every rostered and free-agent player, all leagues)."""
    try:
        d = json.load(open(f"inseason_{YEAR}.json"))
    except Exception:                                                  # noqa: BLE001
        return {}
    seen, by_team = set(), {}
    for L in (d.get("leagues") or {}).values():
        pool = [p for t in L.get("teams", []) for p in t.get("roster", [])] + list(L.get("freeAgents", []))
        for p in pool:
            nm, pos, tm = p.get("name"), p.get("position"), norm_team(p.get("pro_team"))
            v = p.get("effective_per_week") or p.get("ros_per_week")
            if not nm or nm in seen or pos not in ("QB", "RB", "WR", "TE") or not tm or v is None:
                continue
            seen.add(nm)
            by_team.setdefault(tm, {}).setdefault(pos, []).append(float(v))
    take = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
    return {t: sum(sum(sorted(v.get(pos, []), reverse=True)[:n]) for pos, n in take.items())
            for t, v in by_team.items()}


def dst_direction_check(def_tbl):
    """A D/ST should score MORE against a WEAK offense. So across every (team, week) cell, the DST value
    should be negatively correlated with the opposing offense's projected strength. Returns (corr, n, ok)."""
    off = offense_strength()
    xs, ys = [], []
    for t, d in def_tbl.items():
        for g in d["weeks"].values():
            if g["opp"] in off:
                xs.append(g["safpa"]); ys.append(off[g["opp"]])
    n = len(xs)
    if n < 30:
        return None, n, None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs); syy = sum((b - my) ** 2 for b in ys)
    corr = sxy / ((sxx * syy) ** 0.5) if sxx > 0 and syy > 0 else 0.0
    # ok=True only for a clearly negative relationship; near zero = can't tell (None); positive = suspect
    return round(corr, 3), n, (True if corr <= -0.1 else (None if corr < 0.1 else False))


# ---------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------
def validate(espn, fta_by_pos):
    problems = []
    for pos, tbl in fta_by_pos.items():
        if len(tbl) != 32:
            problems.append(f"FTA {pos}: parsed {len(tbl)} teams, expected 32")
        for wk in ("15", "16", "17"):
            n = sum(1 for t in tbl.values() if wk in t["weeks"])
            if n < 28:
                problems.append(f"FTA {pos}: only {n} teams have a parsed Week {wk} game (expected ~28-32)")
    # symmetry on the combined table: if A is at B in week w, B must be home vs A in week w
    allt = fta_by_pos.get("ALL") or {}
    bad = 0
    for t, d in allt.items():
        for wk, g in d["weeks"].items():
            back = (allt.get(g["opp"]) or {}).get("weeks", {}).get(wk)
            if not back or back["opp"] != t or back["home"] == g["home"]:
                bad += 1
    if bad > 2:
        problems.append(f"FTA ALL: {bad} schedule cells fail the A-vs-B / B-at-A symmetry check")
    # byes: FTA vs ESPN vs the hardcoded map
    try:
        from convert_bigboard import TEAM_BYE_2026 as HARD
    except Exception:                                                  # noqa: BLE001
        HARD = {}
    for t in TEAMS:
        seen = {}
        if espn and espn.get(t, {}).get("bye"):
            seen["espn"] = espn[t]["bye"]
        if allt.get(t, {}).get("bye"):
            seen["fta"] = allt[t]["bye"]
        if HARD.get(t):
            seen["hardcoded"] = HARD[t]
        if len(set(seen.values())) > 1:
            problems.append(f"bye-week disagreement for {t}: {seen}")
    if espn:
        for t, d in espn.items():
            if len(d["games"]) not in (17, 18):
                problems.append(f"ESPN {t}: {len(d['games'])} games parsed (expected 17)")
    return problems


# ---------------------------------------------------------------------------------------------
def main():
    os.makedirs("probe_out", exist_ok=True) if PROBE else None
    # --- ESPN ---
    espn, espn_err = None, None
    try:
        raw = http_get(ESPN_URL, as_json=True)
        if PROBE:
            json.dump(raw, open("probe_out/espn_proTeamSchedules.json", "w"))
            print("ESPN top-level keys:", list(raw)[:15])
            print("ESPN settings keys:", list((raw.get("settings") or {}))[:15])
            pts = (raw.get("settings") or {}).get("proTeams") or []
            print("ESPN proTeams:", len(pts), "| first keys:", list(pts[0])[:15] if pts else None)
        espn = parse_espn(raw)
        print(f"ESPN schedule parsed for {len(espn)} teams")
    except Exception as e:                                            # noqa: BLE001
        espn_err = str(e)
        print(f"::warning::ESPN schedule failed: {e}")

    # --- FTA ---
    # A page that won't load (after patient retries) falls back to that position's table from the last good
    # file, flagged stale, instead of sinking the whole run. The run only goes red when NOTHING is fresh.
    import random
    prev = None if PROBE else load_prev()
    prev_pos = (prev or {}).get("positions") or {}
    prev_stale = (prev or {}).get("stale_positions") or {}
    stale = {}
    fta = {}
    first = True
    for tab, pos in FTA_TABS.items():
        qs = "&".join(f"weeks%5B%5D={w}" for w in FTA_WEEKS)
        url = f"{FTA_BASE}?{('tab=' + tab + '&') if tab else ''}{qs}"
        if not first:
            time.sleep(PAUSE + random.uniform(0, 3))
        first = False
        SESSION.headers["Referer"] = FTA_BASE
        try:
            html = http_get(url)
            if PROBE:
                open(f"probe_out/fta_{tab or 'all'}.html", "w", encoding="utf-8").write(html)
            fta[pos] = parse_fta(html)
            print(f"FTA {pos}: {len(fta[pos])} teams")
        except Exception as e:                                        # noqa: BLE001
            old = (prev_pos.get(pos) or {}).get("teams")
            if old:
                fta[pos] = old
                stale[pos] = prev_stale.get(pos) or (prev or {}).get("generated")
                print(f"::warning::FTA {pos} unavailable ({e}); keeping the last good {pos} table "
                      f"from {stale[pos]}")
            elif pos in OPTIONAL_POS:
                print(f"::warning::FTA {pos} unavailable ({e}); continuing without {pos}")
            elif PROBE:
                print(f"::warning::FTA {pos} failed in probe: {e}")
            else:
                fail(f"FTA {pos} failed and there is no earlier data to fall back on: {e}")

    fresh = [p for p in fta if p not in stale]
    if not PROBE and not fresh:
        fail("no FTA page loaded this run (all failed); leaving the last good files untouched")
    if not PROBE and "ALL" in stale and len(stale) < len(fta):
        # the combined table drives the symmetry check; if only it is stale, still fine to proceed
        pass

    if PROBE:
        for pos, tbl in fta.items():
            t = next(iter(tbl))
            print(f"  sample {pos}: {t} -> {json.dumps(tbl[t])[:300]}")
        print("Probe finished; no data files written.")
        return

    # validate DEF on its own first; if it's off, drop it (with a warning) instead of blocking everything
    if "DEF" in fta:
        dprob = [p for p in validate(None, {"DEF": fta["DEF"]}) if p.startswith("FTA DEF")]
        if dprob:
            for p in dprob:
                print(f"::warning::{p}")
            print("::warning::dropping DEF matchups this run; QB-TE still update")
            fta.pop("DEF")
    problems = validate(espn, {k: v for k, v in fta.items() if k not in OPTIONAL_POS})
    if problems:
        for p in problems:
            print(f"::error::{p}")
        fail(f"{len(problems)} validation problem(s); nothing written")

    stale = {k: v for k, v in stale.items() if k in fta}      # DEF may have been dropped by validation
    now = datetime.now(timezone.utc).isoformat()
    # schedule file: ESPN full season if we have it, else the FTA weeks-14-17 slice
    if espn:
        sched = {"year": YEAR, "generated": now, "source": "espn proTeamSchedules_wl", "teams": espn}
    else:
        print(f"::warning::falling back to FTA-only schedule (weeks 14-17). ESPN error: {espn_err}")
        sched = {"year": YEAR, "generated": now, "source": "fta (weeks 14-17 only)",
                 "teams": {t: {"bye": d["bye"], "games": {w: {"opp": g["opp"], "home": g["home"]}
                                                          for w, g in d["weeks"].items()}}
                           for t, d in fta["ALL"].items()}}
    json.dump(sched, open(f"nfl_schedule_{YEAR}.json", "w"), indent=1)

    dcorr, dn, dok = dst_direction_check(fta["DEF"]) if "DEF" in fta else (None, 0, None)
    print(f"DST direction check: corr(DST value, opposing offense strength) = {dcorr} over {dn} cells "
          f"-> {'OK (negative, as expected)' if dok else ('NOT VERIFIED (too weak to tell)' if dok is None else 'SUSPECT')}")
    if dok is False:
        print("::warning::DST values rise against STRONGER offenses -- the tab may read the other way round. "
              "The site will flag the defense numbers as unverified until this passes.")

    positions = {}
    for pos, tbl in fta.items():
        cuts = label_cuts([g["safpa"] for d in tbl.values() for g in d["weeks"].values()])
        positions[pos] = {"cuts": cuts, "teams": {}}
        for t, d in tbl.items():
            positions[pos]["teams"][t] = {"bye": d["bye"], "weeks": {
                w: dict(g, label=label_for(g["safpa"], cuts)) for w, g in d["weeks"].items()}}
    ratings = {
        "year": YEAR, "generated": now, "weeks": FTA_WEEKS,
        "source": "fantasyteamadvice.com playoff strength of schedule (SAFPA)",
        "scoring_basis": "ppr",
        "note": ("safpa = points per game the opposing defense allows at this position versus a "
                 "league-average defense, adjusted for the quality of offenses faced. Computed on PPR "
                 "scoring: trust the ranking and direction; for RB/WR/TE in standard leagues the "
                 "size is slightly overstated."),
        "positions": positions,
        "dst_check": {"corr": dcorr, "cells": dn, "ok": dok},
        "stale_positions": stale,
    }
    json.dump(ratings, open(f"matchup_ratings_{YEAR}.json", "w"), indent=1)
    print(f"Wrote nfl_schedule_{YEAR}.json and matchup_ratings_{YEAR}.json")


if __name__ == "__main__":
    main()
