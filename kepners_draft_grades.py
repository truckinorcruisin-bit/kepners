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
figure, so Zimmer's $/WAR grading doesn't directly apply. This grades each
KEEPER by comparing their actual WAR against the SAME-POSITION peer group
drafted around that round this season -- i.e. "did this keeper outproduce or
underproduce what a normal (non-keeper) pick at this cost, this position,
actually delivered?" That's a direct, same-units (WAR) comparison.

METHOD (revised per Sean's feedback -- see below for what changed and why):
  1. Load kepners_draft_history.json (round, position, player, is_keeper) and
     espn_season_stats_<season>.json (real 2025 points per player).
  2. Match players by normalized name (same normalize_name() convert_bigboard.py
     uses to join the Big Board against ESPN -- consistent matching approach
     across the project). Unmatched players are flagged, not guessed at.
  3. Compute each matched player's actual WAR = actual_points - replacement
     level for their position (same REPLACEMENT_RANK config as
     zimmer_draft_grades.py, for an apples-to-apples WAR definition
     project-wide).
  4. For each keeper, build a PEER GROUP: all non-keeper picks of the SAME
     POSITION drafted within a round-window centered on the keeper's round
     (starts at +/-2 rounds, widens up to +/-6 if fewer than MIN_PEER_SAMPLE
     matches are found -- thin position/round cells are common with only one
     season of data). surplus_war = keeper's actual_war - peer group's
     average actual_war -- the "opportunity cost" comparison: what would a
     normal pick, same cost, same position, have produced instead?
  5. VALUE FLOOR (the fix this revision makes): a keeper with actual_war <= 0
     produced no value above replacement, full stop -- so it CANNOT be graded
     "good" or "great" no matter how the round-cost math looks. A late-round
     zero-WAR keeper isn't a discount; it's a wasted roster/keeper slot that
     could have gone to literally any replacement-level player. So:
       - actual_war <= 0 AND peers meaningfully beat 0  -> "bust" (real
         opportunity cost paid: similar-cost peers at this position DID
         produce value and this pick didn't)
       - actual_war <= 0 AND peers were also near/below 0 -> "fair (no value,
         but so was the field)" -- capped at fair, not rewarded for beating a
         low bar with literally nothing
       - actual_war > 0 -> graded on surplus_war vs peers (great/good/fair/
         overpriced)

WHAT THIS REPLACES: the previous version found "which round's average WAR
matches this player's WAR" and compared that round to the keeper's actual
round. The flaw (per Sean): that let a zero-production keeper score as "great
value" whenever a late round's average also happened to be near zero --
rewarding cost efficiency on an output of nothing. Comparing directly to
same-position, same-cost peers (in WAR, not rounds) plus the value floor
fixes that.

CAVEATS (surfaced in the output, not hidden):
  - n=1 season, and (position, round-window) peer cells can still be thin --
    peer_sample_n and the actual window used are recorded per keeper so you
    can see exactly how much data backed each grade. Treat as directional,
    not precise; will sharpen once more seasons are parsed in.
  - Uses ESPN's D/ST scoring for Kepners D/ST picks, which Sean confirmed
    differs slightly -- immaterial for skill-position keeper grading, which
    is the main use case.

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

# Peer-window config: start at +/-2 rounds around the keeper's round, widen up
# to +/-6 if fewer than MIN_PEER_SAMPLE same-position non-keeper picks are
# found in that window. Never falls back to mixing positions -- WAR scales
# differ too much across positions (a QB's replacement-level WAR isn't
# comparable to a K's) for that to mean anything.
PEER_WINDOW_START = 2
PEER_WINDOW_MAX = 6
MIN_PEER_SAMPLE = 3

# Surplus-vs-peers thresholds (in WAR points) for the verdict labels, and the
# "meaningfully above zero" bar used by the value floor. All tunable; flagged
# as directional given the single-season sample (see caveats above).
SURPLUS_GREAT = 15
SURPLUS_GOOD = 5
SURPLUS_FAIR_FLOOR = -5
PEER_MEANINGFUL_BAR = 5


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


def build_position_round_index(picks):
    """position -> round -> [actual_war, ...] for NON-keeper, matched picks --
    the raw material the peer-window average is built from. Keepers excluded
    (the baseline must be the 'market', not circular against itself)."""
    idx = defaultdict(lambda: defaultdict(list))
    for p in picks:
        if p["is_keeper"] or p["actual_war"] is None:
            continue
        idx[p["position"]][p["round"]].append(p["actual_war"])
    return idx


def peer_avg_war(idx, position, round_, window=PEER_WINDOW_START, max_window=PEER_WINDOW_MAX,
                  min_sample=MIN_PEER_SAMPLE):
    """Average actual WAR of same-POSITION, non-keeper picks within +/-window
    rounds of round_. Widens the window (never crosses positions) until
    min_sample is met or max_window is hit. Returns (avg, n, window_used) or
    (None, 0, None) if even the widest window can't find enough peers."""
    by_round = idx.get(position, {})
    w = window
    while w <= max_window:
        wars = []
        for r, vals in by_round.items():
            if abs(r - round_) <= w:
                wars.extend(vals)
        if len(wars) >= min_sample:
            return round(sum(wars) / len(wars), 1), len(wars), w
        w += 1
    # last attempt at max_window even if still thin, so we at least report
    # something with n < min_sample -- flagged via peer_sample_n in the output
    wars = [v for r, vals in by_round.items() if abs(r - round_) <= max_window for v in vals]
    if wars:
        return round(sum(wars) / len(wars), 1), len(wars), max_window
    return None, 0, None


def grade_keepers(picks):
    idx = build_position_round_index(picks)
    graded = []
    for p in picks:
        if not p["is_keeper"] or p["actual_war"] is None:
            continue
        avg, n, w = peer_avg_war(idx, p["position"], p["round"])
        actual_war = p["actual_war"]

        if avg is None:
            surplus, verdict = None, "insufficient peer data"
        else:
            surplus = round(actual_war - avg, 1)
            if actual_war <= 0:
                # VALUE FLOOR: no production above replacement means no value,
                # regardless of how the round-cost math reads. Can't be "good"
                # or "great" -- at best "fair" if the whole peer group also
                # whiffed at this cost, or "bust" if peers proved real value
                # was gettable at this cost/position and this pick missed it.
                verdict = "bust (no value added)" if avg > PEER_MEANINGFUL_BAR else "fair (no value, but so was the field)"
            elif surplus >= SURPLUS_GREAT:
                verdict = "great value"
            elif surplus >= SURPLUS_GOOD:
                verdict = "good value"
            elif surplus > SURPLUS_FAIR_FLOOR:
                verdict = "fair"
            else:
                verdict = "overpriced"

        graded.append({
            "manager": p.get("manager"), "team": p.get("team"),
            "player": p["player"], "position": p["position"],
            "kept_round": p["round"], "actual_war": actual_war,
            "peer_avg_war": avg, "peer_sample_n": n, "peer_window_rounds": w,
            "surplus_war": surplus, "verdict": verdict,
        })

    # Sort best-to-worst by surplus, but a zero-floored entry (actual_war<=0)
    # is capped at 0 for SORTING purposes even if the raw surplus number looks
    # positive (e.g. actual_war=0 vs a negative-WAR peer group) -- otherwise a
    # do-nothing keeper could out-rank a genuinely productive one just because
    # the field was even worse. The verdict label already reflects this; the
    # sort order should agree with it.
    def sort_key(g):
        if g["surplus_war"] is None:
            return (1, 0)
        val = g["surplus_war"] if g["actual_war"] > 0 else min(g["surplus_war"], 0)
        return (0, -val)
    graded.sort(key=sort_key)
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

    keeper_grades = grade_keepers(picks)

    out = {
        "season": season,
        "scoring_note": SCORING_NOTE,
        "sample_size_note": "n=1 season -- directional, not precise. Re-run once more seasons are added.",
        "methodology_note": (
            "Each keeper's actual WAR is compared to the average actual WAR of same-position, "
            "non-keeper picks within a round-window centered on the keeper's round (widens if thin). "
            "Keepers with 0 or negative actual WAR are capped at 'fair' at best, regardless of round "
            "cost -- no production means no value, even if the round was cheap."
        ),
        "keeper_grades": keeper_grades,
        "unmatched_players": unmatched,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Graded {len(keeper_grades)} keepers for {season} against same-position peer windows.")
    if unmatched:
        print(f"WARNING -- {len(unmatched)} player(s) didn't match ESPN's season-stats pool "
              f"(likely name-format mismatches): {unmatched}")
    for g in keeper_grades:
        peer_txt = f"peer avg {g['peer_avg_war']:+.1f} (n={g['peer_sample_n']}, +/-{g['peer_window_rounds']}rd)" if g['peer_avg_war'] is not None else "no peer data"
        surplus_txt = f"surplus {g['surplus_war']:+.1f}" if g['surplus_war'] is not None else ""
        print(f"  {g['manager']:10} {g['player']:22} R{g['kept_round']:<3} "
              f"WAR {g['actual_war']:>+6.1f}  {peer_txt}  {surplus_txt}  -> {g['verdict']}")
    print(f"Wrote {out_path}.")


if __name__ == "__main__":
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    build(season=season)
