"""
kepners_draft_grades.py
Grades the Kepners 2025 draft -- especially KEEPER VALUE, which was previously
impossible without Yahoo API access (see project history). This works around
that blocker: Kepners and Zimmer use materially the same scoring format
(confirmed with Sean 2026-07-21 -- only a minor D/ST scoring difference,
which doesn't matter for this), so a player's REAL 2025 season performance
scores the same regardless of which league's roster they sat on. That means
espn_season_stats_2025.json (already produced by Zimmer's own pipeline, no
new API pull needed) can stand in as "what these players actually scored" for
Kepners too.

WHAT THIS ANSWERS: in a SNAKE draft, "cost" is a round number, not a dollar
figure, so Zimmer's $/WAR grading doesn't directly apply. Instead, this fits
a ROUND -> expected WAR curve from this season's actual outcomes (using the
non-keeper picks as the "market rate" baseline), then checks each KEEPER
against that curve: did the round they cost produce more or less WAR than a
normal draft pick at that round would? That's a real, data-grounded answer
to "was that a good keeper?" -- not a guess.

METHOD:
  1. Load kepners_draft_history.json (round, position, player, is_keeper) and
     espn_season_stats_<season>.json (real 2025 points per player).
  2. Match players by normalized name (same normalize_name() convert_bigboard.py
     uses to join the Big Board against ESPN -- consistent matching approach
     across the project). Unmatched players are flagged, not guessed at.
  3. Compute each matched player's actual WAR = actual_points - replacement
     level for their position (same REPLACEMENT_RANK config as
     zimmer_draft_grades.py, for an apples-to-apples WAR definition
     project-wide).
  4. Fit a round -> average actual WAR curve using only NON-keeper picks
     (the "true market rate" -- keepers are deliberately excluded from the
     baseline since including them would bias the curve toward keeper value
     circularly).
  5. For each keeper, find the round on that curve whose average WAR is
     closest to the keeper's actual WAR -- call that their "market-rate
     round." round_surplus = market_rate_round - kept_round. Positive means
     they got that much production for FEWER rounds than the field would
     have paid -- a good keeper. Negative means they paid an earlier round
     than the production justified.

CAVEATS (surfaced in the output, not hidden):
  - n=1 season. The round/WAR curve is noisy with only one draft's worth of
    data points per round (12 picks/round) -- treat verdicts as directional,
    not precise. Will sharpen once more seasons are parsed in.
  - Uses ESPN's D/ST scoring for Kepners D/ST picks, which Sean confirmed
    differs slightly -- immaterial for skill-position keeper grading, which
    is the main use case, but D/ST grades here are a rougher approximation.
  - Bench/late-round "replacement level" players can have noisy or negative
    WAR; this is expected and not a bug.

OUTPUT: kepners_draft_grades.json
"""
import json
import sys
from collections import defaultdict

sys.path.insert(0, ".")
from convert_bigboard import normalize_name
from zimmer_draft_grades import REPLACEMENT_RANK

SCORING_NOTE = (
    "Uses espn_season_stats_<season>.json as a stand-in for Yahoo/Kepners scoring. "
    "Confirmed materially equivalent to Kepners' actual scoring settings (only a "
    "minor D/ST scoring difference, immaterial here) -- confirmed with Sean 2026-07-21."
)


def load_season_points(season, stats_path=None):
    path = stats_path or f"espn_season_stats_{season}.json"
    data = json.load(open(path))
    points_by_norm_name = {}
    for pos, plist in data["players_by_position"].items():
        for p in plist:
            points_by_norm_name[normalize_name(p["name"])] = {
                "points": p.get("total_points") or 0, "pos": pos,
            }
    return points_by_norm_name


def compute_replacement(points_by_norm_name):
    by_pos = defaultdict(list)
    for info in points_by_norm_name.values():
        by_pos[info["pos"]].append(info["points"])
    repl = {}
    for pos, pts in by_pos.items():
        pts_sorted = sorted(pts, reverse=True)
        rank = REPLACEMENT_RANK.get(pos, 20)
        idx = min(rank, len(pts_sorted)) - 1
        if idx >= 0:
            repl[pos] = pts_sorted[idx]
    return repl


def attach_actual_war(picks, points_by_norm_name, replacement):
    unmatched = []
    for p in picks:
        if not p.get("player"):
            p["actual_points"] = None
            p["actual_war"] = None
            continue
        info = points_by_norm_name.get(normalize_name(p["player"]))
        if not info:
            unmatched.append(p["player"])
            p["actual_points"] = None
            p["actual_war"] = None
            continue
        p["actual_points"] = info["points"]
        p["actual_war"] = round(info["points"] - replacement.get(p["position"], 0), 1)
    return sorted(set(unmatched))


def fit_round_war_curve(picks):
    """Average actual WAR per round, using only NON-keeper, matched picks --
    this is the 'market rate' baseline keepers get compared against."""
    by_round = defaultdict(list)
    for p in picks:
        if p["is_keeper"] or p["actual_war"] is None:
            continue
        by_round[p["round"]].append(p["actual_war"])
    curve = []
    for rnd, wars in sorted(by_round.items()):
        curve.append({"round": rnd, "avg_war": round(sum(wars) / len(wars), 1), "n": len(wars)})
    return curve


def market_rate_round(curve, war):
    """Which round's average WAR is closest to this WAR value? Interpolates
    between the two nearest curve points rather than snapping to the nearest
    single round, so the surplus number isn't artificially chunky."""
    if not curve:
        return None
    pts = sorted(curve, key=lambda c: c["round"])
    if war >= pts[0]["avg_war"]:
        return pts[0]["round"]
    if war <= pts[-1]["avg_war"]:
        return pts[-1]["round"]
    for a, b in zip(pts, pts[1:]):
        if a["avg_war"] >= war >= b["avg_war"]:
            span = a["avg_war"] - b["avg_war"]
            if span == 0:
                return a["round"]
            frac = (a["avg_war"] - war) / span
            return round(a["round"] + frac * (b["round"] - a["round"]), 1)
    return pts[-1]["round"]


def grade_keepers(picks, curve):
    graded = []
    for p in picks:
        if not p["is_keeper"] or p["actual_war"] is None:
            continue
        mrr = market_rate_round(curve, p["actual_war"])
        # positive surplus = the player's actual production matches an EARLIER
        # (more valuable) round than what they actually cost as a keeper --
        # i.e. the owner got that production for cheaper than the field would
        # have paid for it. Negative = paid an earlier/pricier round than the
        # output justified.
        surplus = round(p["round"] - mrr, 1) if mrr is not None else None
        verdict = "no curve data"
        if surplus is not None:
            if surplus >= 2:
                verdict = "great value"
            elif surplus >= 0.5:
                verdict = "good value"
            elif surplus > -0.5:
                verdict = "fair"
            else:
                verdict = "overpriced"
        graded.append({
            "manager": p.get("manager"), "team": p.get("team"),
            "player": p["player"], "position": p["position"],
            "kept_round": p["round"], "actual_war": p["actual_war"],
            "market_rate_round": mrr, "round_surplus": surplus, "verdict": verdict,
        })
    graded.sort(key=lambda g: (g["round_surplus"] is None, -(g["round_surplus"] or 0)))
    return graded


def build(history_path="kepners_draft_history.json", aliases_path="kepners_team_aliases.json",
          season=2025, stats_path=None, out_path="kepners_draft_grades.json"):
    history = json.load(open(history_path))
    aliases = json.load(open(aliases_path))
    aliases = {k: v for k, v in aliases.items() if not k.startswith("_")}

    picks = history["seasons"][str(season)]["picks"]
    for p in picks:
        alias = aliases.get(p["team_label"])
        p["manager"] = alias["manager"] if alias else None
        p["team"] = alias["team"] if alias else None

    points_by_norm_name = load_season_points(season, stats_path)
    replacement = compute_replacement(points_by_norm_name)
    unmatched = attach_actual_war(picks, points_by_norm_name, replacement)

    curve = fit_round_war_curve(picks)
    keeper_grades = grade_keepers(picks, curve)

    out = {
        "season": season,
        "scoring_note": SCORING_NOTE,
        "sample_size_note": "n=1 season -- directional, not precise. Re-run once more seasons are added.",
        "round_war_curve": curve,
        "keeper_grades": keeper_grades,
        "unmatched_players": unmatched,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Graded {len(keeper_grades)} keepers for {season} against a {len(curve)}-round WAR curve.")
    if unmatched:
        print(f"WARNING -- {len(unmatched)} player(s) didn't match ESPN's season-stats pool "
              f"(likely name-format mismatches): {unmatched}")
    for g in keeper_grades:
        print(f"  {g['manager']:10} {g['player']:22} R{g['kept_round']:<3} "
              f"WAR {g['actual_war']:>+6.1f}  market-rate R{g['market_rate_round']}  "
              f"surplus {g['round_surplus']:+.1f}  -> {g['verdict']}")
    print(f"Wrote {out_path}.")


if __name__ == "__main__":
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    build(season=season)
