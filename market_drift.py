"""Slot-based market drift + TruRank.

WHAT THIS MEASURES
------------------
Not "did this player get cheaper" -- players change teams, get hurt, and rookies
have no prior year at all. Instead this compares POSITIONAL SLOTS year over
year: what did WR4 cost last year vs what does WR4 cost this year? Every player
inherits the drift of the slot he occupies, so rookies and 2026-only names cost
nothing.

Two components, both measured in RANK UNITS (picks) so they can be added:

  priceDrift(pos, n)  = avgRank_2026(pos, n) - avgRank_2025(pos, n)
      Positive = that slot goes LATER this year (a bigger overall rank number)
      = cheaper to buy.

  talentDrift(pos, n) = warRank_2025(pos, n) - warRank_2026(pos, n)
      Positive = this year's occupant of the slot is BETTER, relative to his own
      season's player pool, than last year's occupant was relative to his.

  TruRank = priceDrift + TALENT_WEIGHT * talentDrift
      Positive = genuinely better value than the same slot offered last year.
      Talent is deliberately down-weighted -- see TALENT_WEIGHT below.

WHY TALENT DRIFT IS NEEDED (the confound)
-----------------------------------------
A slot getting cheaper has two possible causes: the market is mispricing that
tier (real edge), or the tier is genuinely worse this year (correctly priced).
Price drift alone cannot tell them apart, so a price-only board would light up
green on every position that simply got worse. Subtracting the talent change
isolates the mispricing.

WHY WAR *RANK* AND NOT WAR ITSELF
---------------------------------
2026 numbers are projections; 2025 numbers are actuals. Projections are
compressed toward the mean and actuals are dispersed by outcome luck, so the two
WAR curves differ in both scale AND shape -- z-scoring fixes the scale but not
the shape. Converting each year's WAR to a rank WITHIN THAT YEAR'S OWN POOL is
distribution-free: it survives both differences, and it lands in the same units
as price drift, so no arbitrary WAR-to-picks exchange rate is needed.

The pool is capped at TALENT_POOL_SIZE for both years so the two rankings span a
comparable universe (2025 season stats include every rostered player; 2026
values include a different count).

SMOOTHING
---------
Raw slot-to-slot drift is noisy -- WR17 up 9, WR18 down 4, purely ADP jitter.
Both components are averaged over a +/-SMOOTH_WINDOW slot window before being
combined. Note this is deliberately the OPPOSITE of the keeper-grading peer
window, which tries the exact bucket first: there the exact bucket is the
signal, here it's the noise.

BANDS
-----
Colour bands are percentile-derived per league (the same approach as
keeperValueTiers) because drift magnitude scales with league depth. But
percentiles alone would always paint ~12% of the board green even in a year with
no real movement, so an absolute NEUTRAL_DEADBAND overrides the percentile and
forces anything under half a round to neutral.

KNOWN LIMITATIONS
-----------------
- 2025 talent is measured on ACTUAL production, so a slot whose 2025 occupant
  busted will read as "2026 is better" partly on hindsight. Smoothing dampens
  this but does not remove it.
- n=1 prior season. Directional, not predictive.
- K/DEF are excluded entirely, consistent with every other WAR-based feature.
"""
import json
import os
import re

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

# Both years' WAR rankings are truncated to this many players so the two pools
# span a comparable universe. Roughly board depth + a cushion.
TALENT_POOL_SIZE = 200

# +/- this many positional slots are averaged together.
SMOOTH_WINDOW = 2

# Talent drift gets a wider window and less than full weight. The two halves of
# TruRank are NOT equally trustworthy: price drift is a clean like-for-like
# measurement (two ADP snapshots built the same way), while talent drift
# compares 2026 projections against 2025 actuals across a methodology gap. It is
# also noisiest exactly where it matters most -- near the top of the WAR
# ranking players are packed within a few points of each other, so a small
# projection-vs-outcome difference swings the RANK a long way.
#
# Half weight makes talent a corrective rather than a co-equal signal: it can
# veto a price bargain that isn't real (a tier that genuinely got worse), but it
# can't manufacture a bargain on its own out of last season's variance.
TALENT_SMOOTH_WINDOW = 4
TALENT_WEIGHT = 0.5

# Neighbourhood size (in overall board rank) used to detrend talent drift
# against draft depth. See the long note in compute_for_league.
DETREND_WINDOW = 40

# |TruRank| below this many picks is forced to neutral regardless of where it
# lands in the percentile distribution -- stops a quiet year being coloured as
# though it had signal. ~half a round in a 12-team league.
NEUTRAL_DEADBAND = 6.0

# Percentile cutoffs for the five colour bands.
BAND_PCTS = {"strongPos": 88, "pos": 68, "neg": 32, "strongNeg": 12}


# ---------------------------------------------------------------- 2025 board --

def _detect_columns(ws, max_scan=12):
    """Find the header row on the 2025 tab and map the columns we need.

    The 2025 tab is a different sheet than 2026 and may not share its exact
    layout, so columns are detected by header text rather than hardcoded.
    Returns (col_map, header_row) or (None, None) if no header row is found.
    """
    wants = {
        "player": re.compile(r"^\s*(player|name)\s*$", re.I),
        "team": re.compile(r"^\s*team\s*$", re.I),
        "pos": re.compile(r"^\s*pos(ition)?\.?\s*$", re.I),
        "avgRank": re.compile(r"^\s*(avg|average)\.?\s*(rank|rk)\s*$", re.I),
    }
    for r in range(1, min(max_scan, ws.max_row) + 1):
        found = {}
        for c in range(1, min(ws.max_column, 60) + 1):
            v = ws.cell(r, c).value
            if not isinstance(v, str):
                continue
            for key, rx in wants.items():
                if key not in found and rx.match(v):
                    found[key] = c
        if "player" in found and "avgRank" in found:
            return found, r
    return None, None


def read_2025_board(wb, sheet="2025 Big Board", fallback_cols=None):
    """Parse last season's Big Board tab -> [{name, team, pos, avgRank}].

    Rows are validated the same defensive way as the 2026 tab: a real player row
    has team AND pos populated. The workbook is known to carry a trailing
    scratch list of bare names, and boundaries must be data-driven, never
    row-number-based.
    """
    from convert_bigboard import canonical_position, norm  # lazy: avoids a cycle

    if sheet not in wb.sheetnames:
        print(f"Note: no '{sheet}' tab in the workbook -- market drift disabled. "
              f"Tabs present: {wb.sheetnames}")
        return []

    ws = wb[sheet]
    cols, header_row = _detect_columns(ws)
    if cols:
        print(f"Market drift: '{sheet}' header found on row {header_row}; "
              f"columns {cols}")
    elif fallback_cols:
        cols, header_row = fallback_cols, 6
        print(f"WARNING: could not detect headers on '{sheet}'; falling back to "
              f"the 2026 column map {cols}. Verify the parsed count below looks "
              f"right before trusting the drift numbers.")
    else:
        print(f"WARNING: could not detect headers on '{sheet}' and no fallback "
              f"supplied -- market drift disabled.")
        return []

    rows, skipped_scratch, bad_rank = [], 0, 0
    for r in range(header_row + 1, ws.max_row + 1):
        name = norm(ws.cell(r, cols["player"]).value)
        if not name:
            continue
        team = norm(ws.cell(r, cols["team"]).value) if "team" in cols else None
        raw_pos = norm(ws.cell(r, cols["pos"]).value) if "pos" in cols else None
        if not team or not raw_pos:
            skipped_scratch += 1
            continue
        pos = canonical_position(raw_pos)
        rank = ws.cell(r, cols["avgRank"]).value
        if not isinstance(rank, (int, float)):
            bad_rank += 1
            continue
        rows.append({"name": name, "team": team, "pos": pos,
                     "avgRank": float(rank)})

    print(f"Market drift: parsed {len(rows)} ranked 2025 players "
          f"({skipped_scratch} scratch/incomplete rows skipped, "
          f"{bad_rank} with unusable avgRank).")
    return rows


# ---------------------------------------------------------------- talent ranks --

def _war_rank_map(war_by_name):
    """{normalized name -> WAR} -> {normalized name -> 1-based rank}, truncated
    to TALENT_POOL_SIZE. Anyone outside the pool is simply absent."""
    ordered = sorted((n for n, w in war_by_name.items() if w is not None),
                     key=lambda n: war_by_name[n], reverse=True)
    return {n: i + 1 for i, n in enumerate(ordered[:TALENT_POOL_SIZE])}


def war_ranks_2026(players, league_key):
    from convert_bigboard import normalize_name

    war = {}
    for p in players:
        if p.get("pos") not in SKILL_POSITIONS:
            continue
        w = (p.get("warByLeague") or {}).get(league_key)
        if w is None:
            w = p.get("projectedWar")
        if w is not None:
            war[normalize_name(p["name"])] = w
    return _war_rank_map(war)


def war_ranks_2025(replacement_ranks, path="espn_season_stats_2025.json"):
    """Last season's ACTUAL WAR, ranked. Same replacement config as 2026 so any
    per-position offset cancels when the two ranks are differenced."""
    from convert_bigboard import canonical_position, normalize_name

    if not os.path.exists(path):
        print(f"Note: {path} missing -- drift will be price-only.")
        return {}
    with open(path) as f:
        data = json.load(f)

    by_pos = {}
    for raw_pos, plist in (data.get("players_by_position") or {}).items():
        pos = canonical_position(raw_pos)
        if pos not in SKILL_POSITIONS:
            continue
        by_pos.setdefault(pos, []).extend(
            {"name": p.get("name"), "points": p.get("total_points") or 0}
            for p in plist if p.get("name"))

    war = {}
    for pos, plist in by_pos.items():
        plist.sort(key=lambda x: x["points"], reverse=True)
        rank = replacement_ranks.get(pos, 20)
        idx = min(rank, len(plist)) - 1
        repl = plist[idx]["points"] if idx >= 0 else 0
        for p in plist:
            war[normalize_name(p["name"])] = p["points"] - repl
    return _war_rank_map(war)


# ---------------------------------------------------------------- drift math --

def _slots(rows):
    """Order each position's players by consensus rank -> positional slot list."""
    by_pos = {}
    for r in rows:
        if r.get("pos") in SKILL_POSITIONS and r.get("avgRank") is not None:
            by_pos.setdefault(r["pos"], []).append(r)
    for plist in by_pos.values():
        plist.sort(key=lambda x: x["avgRank"])
    return by_pos


def _smooth(values, window=SMOOTH_WINDOW):
    """Centred moving average over the slot axis, ignoring None entries."""
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - window), min(len(values), i + window + 1)
        vals = [v for v in values[lo:hi] if v is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * (q / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _band(tru, cuts):
    if tru is None or cuts is None:
        return "none"
    if abs(tru) < NEUTRAL_DEADBAND:
        return "neutral"
    if tru >= cuts["strongPos"]:
        return "strong-pos"
    if tru >= cuts["pos"]:
        return "pos"
    if tru <= cuts["strongNeg"]:
        return "strong-neg"
    if tru <= cuts["neg"]:
        return "neg"
    return "neutral"


def compute_for_league(players, board_2025, league_key, replacement_ranks):
    """Attaches marketDrift[league_key] to every 2026 player and returns the
    band cutoffs used. Mutates `players`."""
    from convert_bigboard import normalize_name

    slots26 = _slots([
        {"name": p["name"], "pos": p.get("pos"),
         "avgRank": p["avgRank"] if isinstance(p.get("avgRank"), (int, float)) else None,
         "ref": p}
        for p in players])
    slots25 = _slots(board_2025)

    wr26 = war_ranks_2026(players, league_key)
    wr25 = war_ranks_2025(replacement_ranks)

    per_player, all_tru = {}, []

    # --- pass 1: raw per-slot components -----------------------------------
    raw, recs = {}, []
    for pos, cur in slots26.items():
        prev = slots25.get(pos, [])
        depth = min(len(cur), len(prev))
        price_raw, talent_raw = [], []
        for n in range(len(cur)):
            if n >= depth:
                price_raw.append(None)
                talent_raw.append(None)
                continue
            # 2026 minus 2025: a LARGER rank number means the slot falls later
            # in the draft this year, i.e. it got cheaper -> positive.
            price_raw.append(cur[n]["avgRank"] - prev[n]["avgRank"])
            a = wr26.get(normalize_name(cur[n]["name"]))
            b = wr25.get(normalize_name(prev[n]["name"]))
            t = (b - a) if (a is not None and b is not None) else None
            talent_raw.append(t)
            if t is not None:
                recs.append((cur[n]["avgRank"], pos, n, t))
        raw[pos] = (price_raw, talent_raw, depth)

    # --- pass 2: detrend talent against board depth ------------------------
    # Price drift is self-centring: overall ranks are a permutation of 1..N in
    # both seasons, so the shifts necessarily sum to zero. Talent drift is not,
    # and its bias is NOT a constant that a global mean would remove.
    #
    # 2026 talent is projections; 2025 talent is actuals. Actuals carry a full
    # season of outcome variance that projections (regressed toward the mean)
    # do not. A noisier year scatters its top slots away from the top of the WAR
    # ranking, so EVERY early slot looks like it "improved" in 2026 and every
    # deep slot looks like it got worse -- a systematic tilt that runs with board
    # depth. Left in, the whole first few rounds would glow green.
    #
    # So the baseline is computed locally in overall-rank space: each slot is
    # compared against the median talent drift of its ~DETREND_WINDOW nearest
    # neighbours on the board, regardless of position. What survives is the only
    # thing that's actionable -- did this slot's talent hold up better or worse
    # than everything else going around the same point in the draft. A position
    # that genuinely collapses still shows, because its neighbours at that depth
    # are mostly other positions and they didn't.
    recs.sort(key=lambda x: x[0])
    vals = [r[3] for r in recs]
    baseline = {}
    half = DETREND_WINDOW // 2
    for i, (_, pos, n, t) in enumerate(recs):
        lo, hi = max(0, i - half), min(len(recs), i + half + 1)
        baseline[(pos, n)] = _percentile(sorted(vals[lo:hi]), 50) or 0.0

    for pos, cur in slots26.items():
        price_raw, talent_raw, depth = raw[pos]
        talent_raw = [None if v is None else v - baseline.get((pos, n), 0.0)
                      for n, v in enumerate(talent_raw)]
        price_s = _smooth(price_raw)
        talent_s = _smooth(talent_raw, TALENT_SMOOTH_WINDOW)

        for n, entry in enumerate(cur):
            p = entry["ref"]
            if n >= depth:
                sample = "beyond-2025-depth"
                price = talent = tru = None
            elif price_s[n] is None:
                sample = "none"
                price = talent = tru = None
            elif talent_s[n] is None:
                sample = "price-only"
                price, talent = round(price_s[n], 1), None
                tru = price
            else:
                sample = "ok"
                price = round(price_s[n], 1)
                talent = round(talent_s[n] * TALENT_WEIGHT, 1)
                tru = round(price + talent, 1)
            per_player[id(p)] = {
                "slot": f"{pos}{n + 1}", "price": price, "talent": talent,
                "tru": tru, "sample": sample,
                "priceRaw": round(price_raw[n], 1) if n < depth and price_raw[n] is not None else None,
            }
            # price-only slots count toward the band distribution too --
            # otherwise a missing 2025 stats file leaves zero rated slots and
            # the whole board silently renders neutral.
            if tru is not None and sample in ("ok", "price-only"):
                all_tru.append(tru)

    all_tru.sort()
    cuts = ({k: round(_percentile(all_tru, q), 1) for k, q in BAND_PCTS.items()}
            if len(all_tru) >= 20 else None)
    if cuts is None:
        print(f"Market drift [{league_key}]: only {len(all_tru)} rated slots -- "
              f"too few to derive bands; everything renders neutral.")

    for p in players:
        rec = per_player.get(id(p))
        if rec is None:
            rec = {"slot": None, "price": None, "talent": None, "tru": None,
                   "sample": "n/a", "priceRaw": None}
        rec["band"] = _band(rec["tru"], cuts) if rec["sample"] in ("ok", "price-only") else "none"
        p.setdefault("marketDrift", {})[league_key] = rec

    return cuts


def compute(players, board_2025, leagues, replacement_ranks_by_league):
    """Top-level entry. Returns {league_key: band cutoffs} for the site to show
    in its legend. No-ops safely if the 2025 tab was missing."""
    if not board_2025:
        for p in players:
            p["marketDrift"] = {}
        return {}
    bands = {}
    for lkey in leagues:
        ranks = replacement_ranks_by_league.get(lkey)
        if not ranks:
            continue
        bands[lkey] = compute_for_league(players, board_2025, lkey, ranks)
        rated = sum(1 for p in players
                    if (p.get("marketDrift") or {}).get(lkey, {}).get("sample") == "ok")
        print(f"Market drift [{lkey}]: {rated} players rated; bands={bands[lkey]}")
    return bands
