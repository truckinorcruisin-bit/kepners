"""
bid_calibration.py
Builds bid_calibration.json -- the STATIC foundation for Mission Control's
recommended bid. Converts "WAR units" into "dollars" by fitting a blended
WAR->$ curve, so the live draft engine can turn any player's projected WAR
into a base bid.

WHY STATIC: this needs the full historical dataset (all graded Zimmer drafts)
plus ESPN's full current-year auction-value pool. It's computed once, ahead of
draft day, and the live engine just reads the resulting small JSON. Live
adjustments (scarcity, roster fit, budget, opponents) are layered ON TOP of this
base at draft time -- they are NOT in this file.

TWO SOURCES, BLENDED (per the design decision):
  1. ZIMMER HISTORY -- for every graded pick, we have (war, actual cost paid).
     Captures how THIS league actually spends. Smaller sample, league-specific.
     Source: zimmer_draft_grades.py's graded picks -- but that script doesn't
     persist per-pick war+cost, so we recompute the same join here from
     zimmer_draft_history.json + espn_season_stats_<year>.json.
  2. ESPN 2026 -- for every player with both a projected WAR and an
     auction_value_avg, that pairing is (war -> recommended $). Captures the
     broader market's current read. Larger sample, more stable, less
     league-specific.

DIMINISHING RETURNS via PIECEWISE WAR BANDS (not a single slope):
Rather than assume one $/WAR rate, players are bucketed into WAR bands and we
compute average $/WAR WITHIN each band. Elite bands show a higher $/WAR (people
pay a premium for the scarce top tier); marginal bands show less. That band
structure IS the diminishing-returns curve -- derived from data, not assumed.

BLEND WEIGHT (config): within each band, the two sources' $/WAR are combined as
  blended = ZIMMER_WEIGHT * zimmer_rate + (1 - ZIMMER_WEIGHT) * espn_rate
Default leans ESPN (larger sample) but keeps real league behavior in the mix.

REQUIRES (all already produced by earlier steps):
  zimmer_draft_history.json
  espn_season_stats_<year>.json         (for historical WAR -- actual points)
  espn_player_values_<year>.json        (for ESPN projected WAR + auction values)

OUTPUT: bid_calibration.json
{
  "generated": "...", "zimmer_weight": 0.4,
  "war_bands": [ {"min_war":.., "max_war":..} , ... ],
  "dollar_per_war": {
     "GLOBAL": [ {band, zimmer_rate, espn_rate, blended_rate, n_zimmer, n_espn}, ... ],
     "RB": [...], "WR": [...], ...    # per-position where enough sample; else GLOBAL
  },
  "notes": [...]
}
"""
import json
import os
import glob
from collections import defaultdict
from statistics import mean

# Reuse the SAME replacement-rank + graded-years config as the grading script,
# so historical WAR here matches historical WAR there exactly.
from zimmer_draft_grades import REPLACEMENT_RANK, GRADED_YEARS
from zimmer_analysis import OWNER_ALIASES, EXCLUDED_OWNERS, normalize_owner

OUT_FILE = "bid_calibration.json"
ZIMMER_WEIGHT = 0.4  # 40% Zimmer-history / 60% ESPN. Tune toward 1.0 for more
                     # league-specificity, toward 0.0 for more market stability.

# Positions this calibration covers. K/DEF are deliberately EXCLUDED: they're
# always min-bid ($0-1) at the end of the draft, so bid strategy is irrelevant
# there -- and their many $1 picks with tiny WAR badly distort a $/WAR rate.
# Restricting to the skill positions keeps every band rate meaningful.
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}

# WAR bands (points above replacement). Chosen to separate the elite tier
# (where premiums live) from the long tail. Tunable.
WAR_BANDS = [
    {"min_war": 80, "max_war": 9999, "label": "elite"},
    {"min_war": 50, "max_war": 80, "label": "strong"},
    {"min_war": 25, "max_war": 50, "label": "solid"},
    {"min_war": 5, "max_war": 25, "label": "marginal"},
    {"min_war": -9999, "max_war": 5, "label": "replacement"},
]
MIN_BAND_SAMPLE = 4  # need at least this many points in a (position,band) to
                     # trust a position-specific rate; else fall back to GLOBAL


def band_for_war(war):
    for b in WAR_BANDS:
        if b["min_war"] <= war < b["max_war"]:
            return b["label"]
    return "replacement"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def latest_file(pattern):
    files = sorted(glob.glob(pattern), reverse=True)
    return files[0] if files else None


def compute_replacement(season_stats):
    """Replacement-level actual points per position, same method as grading."""
    repl = {}
    for pos, plist in season_stats["players_by_position"].items():
        plist_sorted = sorted(plist, key=lambda x: x.get("total_points") or 0, reverse=True)
        rank = REPLACEMENT_RANK.get(pos, 20)
        idx = min(rank, len(plist_sorted)) - 1
        if idx >= 0:
            repl[pos] = plist_sorted[idx].get("total_points") or 0
    return repl


def gather_zimmer_points():
    """(position, war, cost) tuples from real historical picks."""
    if not os.path.exists("zimmer_draft_history.json"):
        print("No zimmer_draft_history.json -- skipping Zimmer side of blend.")
        return []
    history = load_json("zimmer_draft_history.json")
    out = []
    for year in GRADED_YEARS:
        sfile = f"espn_season_stats_{year}.json"
        if not os.path.exists(sfile):
            continue
        season_stats = load_json(sfile)
        season_points = {}
        for pos, plist in season_stats["players_by_position"].items():
            for p in plist:
                season_points[p["player_id"]] = p["total_points"]
        repl = compute_replacement(season_stats)

        sdata = history["seasons"].get(str(year))
        if not sdata or sdata.get("draft_type") != "auction":
            continue
        for p in sdata.get("picks", []):
            if not p.get("cost") or p["cost"] <= 0:
                continue
            pts = season_points.get(p.get("player_id"))
            if pts is None:
                continue
            pos = (p.get("position") or "").upper()
            if pos not in SKILL_POSITIONS:
                continue
            war = pts - repl.get(pos, 0)
            out.append((pos, war, p["cost"]))
    return out


def gather_espn_points():
    """(position, projected_war, auction_value) tuples from ESPN's current pool."""
    pvfile = latest_file("espn_player_values_*.json")
    if not pvfile:
        print("No espn_player_values_*.json -- skipping ESPN side of blend.")
        return []
    data = load_json(pvfile)
    # replacement from projected points
    by_pos = defaultdict(list)
    for p in data["players"]:
        by_pos[p["position"]].append(p)
    repl = {}
    for pos, plist in by_pos.items():
        plist.sort(key=lambda x: x.get("projected_total_points") or 0, reverse=True)
        rank = REPLACEMENT_RANK.get(pos, 20)
        idx = min(rank, len(plist)) - 1
        if idx >= 0:
            repl[pos] = plist[idx].get("projected_total_points") or 0

    out = []
    for p in data["players"]:
        av = p.get("auction_value_avg")
        proj = p.get("projected_total_points")
        if not av or av <= 0 or proj is None:
            continue
        pos = p["position"]
        if pos not in SKILL_POSITIONS:
            continue
        war = proj - repl.get(pos, 0)
        out.append((pos, war, av))
    return out


def rate_by_band(points):
    """points: list of (position, war, dollars). Returns nested dict
    band -> position -> (rate, n) and band -> GLOBAL -> (rate, n).
    Rate = sum(dollars)/sum(war) within the band (dollar-weighted, and only
    over positive-WAR entries -- negative/zero WAR can't define a $/WAR rate)."""
    # bucket
    buckets = defaultdict(lambda: defaultdict(lambda: {"war": 0.0, "dollars": 0.0, "n": 0}))
    for pos, war, dollars in points:
        if war <= 0:
            continue
        band = band_for_war(war)
        buckets[band][pos]["war"] += war
        buckets[band][pos]["dollars"] += dollars
        buckets[band][pos]["n"] += 1
        buckets[band]["GLOBAL"]["war"] += war
        buckets[band]["GLOBAL"]["dollars"] += dollars
        buckets[band]["GLOBAL"]["n"] += 1
    rates = {}
    for band, posmap in buckets.items():
        rates[band] = {}
        for pos, agg in posmap.items():
            rate = agg["dollars"] / agg["war"] if agg["war"] > 0 else None
            rates[band][pos] = {"rate": rate, "n": agg["n"]}
    return rates


def build():
    zimmer_pts = gather_zimmer_points()
    espn_pts = gather_espn_points()
    print(f"Zimmer sample: {len(zimmer_pts)} picks; ESPN sample: {len(espn_pts)} players.")

    zimmer_rates = rate_by_band(zimmer_pts)
    espn_rates = rate_by_band(espn_pts)

    positions = ["GLOBAL", "QB", "RB", "WR", "TE"]
    dollar_per_war = {}
    for pos in positions:
        rows = []
        for b in WAR_BANDS:
            band = b["label"]
            z = zimmer_rates.get(band, {}).get(pos, {"rate": None, "n": 0})
            e = espn_rates.get(band, {}).get(pos, {"rate": None, "n": 0})

            # fall back to GLOBAL for a source if this position lacks sample
            if (z["rate"] is None or z["n"] < MIN_BAND_SAMPLE) and pos != "GLOBAL":
                z = zimmer_rates.get(band, {}).get("GLOBAL", {"rate": None, "n": 0})
            if (e["rate"] is None or e["n"] < MIN_BAND_SAMPLE) and pos != "GLOBAL":
                e = espn_rates.get(band, {}).get("GLOBAL", {"rate": None, "n": 0})

            zr, er = z["rate"], e["rate"]
            if zr is not None and er is not None:
                blended = ZIMMER_WEIGHT * zr + (1 - ZIMMER_WEIGHT) * er
            elif zr is not None:
                blended = zr
            elif er is not None:
                blended = er
            else:
                blended = None

            rows.append({
                "band": band,
                "min_war": b["min_war"], "max_war": b["max_war"],
                "zimmer_rate": round(zr, 3) if zr is not None else None,
                "espn_rate": round(er, 3) if er is not None else None,
                "blended_rate": round(blended, 3) if blended is not None else None,
                "n_zimmer": z["n"], "n_espn": e["n"],
            })
        dollar_per_war[pos] = rows

    notes = derive_notes(dollar_per_war)
    out = {
        "generated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "zimmer_weight": ZIMMER_WEIGHT,
        "positions_included": sorted(SKILL_POSITIONS),
        "war_bands": WAR_BANDS,
        "min_band_sample": MIN_BAND_SAMPLE,
        "dollar_per_war": dollar_per_war,
        "notes": notes,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE}.")
    for row in dollar_per_war["GLOBAL"]:
        print(f"  {row['band']:12} blended ${row['blended_rate']}/WAR "
              f"(Z:{row['zimmer_rate']} n={row['n_zimmer']} | E:{row['espn_rate']} n={row['n_espn']})")


def derive_notes(dpw):
    notes = []
    g = {r["band"]: r["blended_rate"] for r in dpw["GLOBAL"] if r["blended_rate"] is not None}
    if "elite" in g and "solid" in g and g["solid"]:
        ratio = g["elite"] / g["solid"]
        notes.append(
            f"Elite-tier WAR costs ~{ratio:.1f}x more per point than solid-tier "
            f"(${g.get('elite')}/WAR vs ${g.get('solid')}/WAR) -- the diminishing-returns "
            f"premium for top players, derived from data."
        )
    notes.append(
        "blended_rate is the WAR->$ conversion the live bid engine multiplies a "
        "player's projected WAR by to get a BASE bid, before live scarcity/roster/"
        "budget/opponent adjustments."
    )
    return notes


if __name__ == "__main__":
    build()
