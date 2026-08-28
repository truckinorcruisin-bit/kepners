"""
kepners_analysis.py
Builds per-owner drafting profiles for the Kepners league from parsed snake-
draft history (kepners_draft_history.json, produced by
parse_kepners_draft_txt.py), resolved to real managers via
kepners_team_aliases.json.

This is the snake-draft analog of zimmer_analysis.py, but the mechanics
differ because there's no bid/dollar signal to analyze -- the equivalent
signal in a snake draft is WHEN (which round) an owner takes each position,
which is what this script profiles instead.

CURRENT SAMPLE SIZE: only the 2025 season is available as of this writing
(confirmed with Sean -- no earlier seasons' text exports exist yet). That
means the "league-wide position round distribution" below is a single
season's data point, not an average across years the way Zimmer's
tier_spreads are. Treat these as a BASELINE reference, not a stable trend --
re-run this once more seasons are parsed in and the averages will mean more.

NOT YET POSSIBLE (flagged rather than faked): grading keeper VALUE (e.g. "was
that a good keeper cost?") needs the KEPT player's actual season point total,
which requires an espn_season_stats_<year>.json for the season they were kept
INTO. That file isn't available for Kepners yet (Yahoo API access is still
pending -- see project history). This script profiles keeper USAGE (who kept
what, at what round cost) but not keeper VALUE. Add that once stats are
available, following the same WAR-surplus-per-cost pattern as
zimmer_draft_grades.py.

OUTPUT: kepners_analysis.json
{
  "seasons_analyzed": [2025],
  "position_round_distribution": { "RB": [{"round":1,"count":5}, ...], ... },
  "owners": [
    {
      "manager": "Bobby", "team": "Demin", "team_label_seen": "Denim Like A...",
      "keepers": [{"player":"...", "position":"...", "pro_team":"...", "round":...}],
      "early_position_counts": {"RB":3, "WR":2, ...},   # rounds 1-6
      "first_position_taken": "RB",
      "missed_picks": 0,
      "strategy_note": "..."
    }, ...
  ],
  "unmatched_team_labels": [...]
}
"""
import json
import re
import sys
from collections import defaultdict

EARLY_ROUNDS = 6  # "early" draft window used for positional-lean signal

# Positions where TIMING is the interesting signal rather than volume. RB/WR
# get taken constantly so "when did you take one" says little; QB/TE are
# taken once or twice and cluster hard into runs, so the timing of a single
# pick is genuinely predictive.
TIMING_POSITIONS = ["QB", "TE"]

# A "run" = at least RUN_MIN players at one position taken inside a window of
# RUN_WINDOW consecutive overall picks. Tuned against the real 2025 Kepners
# draft, where QBs went at overall 23/25/28/35 (4 inside 13 picks) -- a
# window much tighter than ~12 would miss that, and much wider would start
# calling the whole draft one long run.
RUN_WINDOW = 14
RUN_MIN = 3


def positional_runs(picks):
    """Finds clusters where a position came off the board in a burst.
    Reports who STARTED each run, which is the actionable part -- the owners
    who repeatedly go first are the ones to watch for triggering the next
    one."""
    runs = []
    for pos in TIMING_POSITIONS:
        sel = sorted(
            (p for p in picks if p["position"] == pos and not p["is_keeper"] and p["overall_pick"]),
            key=lambda p: p["overall_pick"],
        )
        i = 0
        while i < len(sel):
            # greedily extend a window from this pick
            j = i
            while j + 1 < len(sel) and sel[j + 1]["overall_pick"] - sel[i]["overall_pick"] <= RUN_WINDOW:
                j += 1
            if (j - i + 1) >= RUN_MIN:
                members = sel[i:j + 1]
                runs.append({
                    "position": pos,
                    "start_overall": members[0]["overall_pick"],
                    "end_overall": members[-1]["overall_pick"],
                    "start_round": members[0]["round"],
                    "count": len(members),
                    "started_by": members[0].get("manager"),
                    "started_by_team": members[0].get("team"),
                    "members": [
                        {"overall": m["overall_pick"], "round": m["round"],
                         "manager": m.get("manager"), "player": m["player"]}
                        for m in members
                    ],
                })
                i = j + 1
            else:
                i += 1
    return runs


def positional_timing(picks):
    """League-wide reference for WHEN each timing-position comes off the board:
    first/median/last, plus the full ordered sequence. This is the baseline an
    individual owner's aggression is measured against."""
    out = {}
    for pos in TIMING_POSITIONS:
        sel = sorted(
            (p for p in picks if p["position"] == pos and not p["is_keeper"] and p["overall_pick"]),
            key=lambda p: p["overall_pick"],
        )
        if not sel:
            continue
        # An owner's FIRST pick at this position is the meaningful event; a
        # backup QB in round 14 says nothing about their strategy.
        firsts = {}
        for idx, p in enumerate(sel):
            mgr = p.get("manager")
            if mgr and mgr not in firsts:
                firsts[mgr] = {"round": p["round"], "overall": p["overall_pick"],
                               "gone_before": idx, "player": p["player"]}
        rounds = sorted(v["round"] for v in firsts.values())
        median_round = rounds[len(rounds) // 2] if rounds else None
        out[pos] = {
            "total_drafted": len(sel),
            "first_off_board": {"overall": sel[0]["overall_pick"], "round": sel[0]["round"],
                                "manager": sel[0].get("manager"), "player": sel[0]["player"]},
            "median_first_round": median_round,
            "owners_who_drafted": len(firsts),
            "first_pick_by_manager": firsts,
        }
    return out


def timing_profile_for_owner(manager, timing):
    """Per-owner QB/TE timing read, classified against the league baseline.
    'gone_before' (how many at that position were already off the board) is
    the key aggression signal -- it separates the owner who STARTS a run from
    one who merely got caught in it, which a round number alone can't."""
    prof = {}
    for pos, data in timing.items():
        mine = data["first_pick_by_manager"].get(manager)
        if not mine:
            prof[pos] = {"drafted": False,
                         "note": f"never drafted a {pos} (kept one, or streamed off waivers)"}
            continue
        med = data["median_first_round"]
        delta = (mine["round"] - med) if med is not None else None
        if delta is None:
            stance = "unknown"
        elif delta <= -2:
            stance = "aggressive"
        elif delta >= 2:
            stance = "patient"
        else:
            stance = "market"
        prof[pos] = {
            "drafted": True,
            "round": mine["round"],
            "overall": mine["overall"],
            "player": mine["player"],
            "gone_before": mine["gone_before"],
            "vs_median_rounds": delta,
            "stance": stance,
            "started_the_run": mine["gone_before"] == 0,
        }
    return prof


def load_adp(path="kepners_adp_2025.json"):
    """Historical Yahoo ADP, written by convert_bigboard.py from the '2025 Big
    Board' tab (column M, "Y!"). Optional: if it's missing, reach analysis is
    skipped and everything else still works, rather than failing the run."""
    try:
        with open(path) as f:
            return json.load(f).get("yahoo_adp", {})
    except FileNotFoundError:
        return {}


def _norm_name(name):
    """Local copy of the Big Board's name normalizer so this script doesn't
    have to import convert_bigboard (which would drag in openpyxl)."""
    if not name:
        return ""
    s = str(name).lower().strip()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\.?$", "", s).strip()
    s = re.sub(r"[.'’\-]", "", s)
    return re.sub(r"\s+", " ", s)


def attach_adp(picks, adp):
    """Attaches each pick's Yahoo ADP and its reach.

    reach = adp_rank - overall_pick
      positive -> taken EARLIER than the market said (a reach)
      negative -> fell past ADP (a value pick)

    KEEPERS ARE EXCLUDED from reach entirely: a keeper's round is fixed by
    last year's cost, not by a decision made against this year's board, so
    counting them would score owners for a choice they didn't make at that
    pick."""
    matched = unmatched = 0
    misses = []
    for p in picks:
        p["adp"] = None
        p["reach"] = None
        if not p.get("player") or p.get("is_keeper"):
            continue
        rank = adp.get(_norm_name(p["player"]))
        if rank is None:
            unmatched += 1
            misses.append(p["player"])
            continue
        matched += 1
        p["adp"] = rank
        p["reach"] = rank - p["overall_pick"]
    return matched, unmatched, misses


def reach_profile(manager, picks):
    """Per-owner reach summary, overall and for the timing positions.
    Reported as a MEDIAN, not a mean: a single wild round-15 flier on a player
    with ADP 250 would otherwise swamp an owner's whole profile."""
    mine = [p for p in picks if p.get("manager") == manager and p.get("reach") is not None]
    if not mine:
        return {"sample": 0}

    def summarize(subset):
        if not subset:
            return None
        vals = sorted(p["reach"] for p in subset)
        med = vals[len(vals) // 2]
        biggest = max(subset, key=lambda p: p["reach"])
        return {
            "sample": len(vals),
            "median_reach": round(med, 1),
            "biggest_reach": {"player": biggest["player"], "reach": round(biggest["reach"], 1),
                              "overall": biggest["overall_pick"], "adp": biggest["adp"],
                              "position": biggest["position"]},
        }

    out = {"sample": len(mine), "overall": summarize(mine)}
    for pos in TIMING_POSITIONS:
        out[pos] = summarize([p for p in mine if p["position"] == pos])
    return out


def load(history_path, aliases_path):
    history = json.load(open(history_path))
    aliases = json.load(open(aliases_path))
    aliases = {k: v for k, v in aliases.items() if not k.startswith("_")}
    return history, aliases


def resolve_picks(picks, aliases):
    """Attaches resolved team/manager to each pick via the alias map. Picks
    whose team_label isn't in the alias file are left unresolved and their
    labels collected for the unmatched_team_labels flag -- never guessed."""
    unmatched = set()
    resolved = []
    for p in picks:
        alias = aliases.get(p["team_label"])
        if not alias:
            if p["team_label"]:
                unmatched.add(p["team_label"])
            resolved.append({**p, "team": None, "manager": None})
        else:
            resolved.append({**p, "team": alias["team"], "manager": alias["manager"]})
    return resolved, sorted(unmatched)


def position_round_distribution(picks):
    dist = defaultdict(lambda: defaultdict(int))
    for p in picks:
        if p["position"]:
            dist[p["position"]][p["round"]] += 1
    out = {}
    for pos, rounds in dist.items():
        out[pos] = [{"round": r, "count": c} for r, c in sorted(rounds.items())]
    return out


def build_owner_profile(manager, team, team_label, picks, timing=None):
    my_picks = [p for p in picks if p["manager"] == manager]
    my_picks.sort(key=lambda p: p["round"])
    reach = reach_profile(manager, picks)

    keepers = [
        {"player": p["player"], "position": p["position"], "pro_team": p["pro_team"], "round": p["round"]}
        for p in my_picks if p["is_keeper"]
    ]
    missed = sum(1 for p in my_picks if p["player"] is None)

    early = [p for p in my_picks if p["round"] <= EARLY_ROUNDS and p["position"]]
    early_counts = defaultdict(int)
    for p in early:
        early_counts[p["position"]] += 1

    drafted_positions = [p for p in my_picks if p["position"]]
    first_pos = drafted_positions[0]["position"] if drafted_positions else None

    timing_prof = timing_profile_for_owner(manager, timing or {})

    # lightweight strategy note from the actual numbers (same spirit as
    # zimmer_analysis.py's generated notes -- describe what happened, don't
    # editorialize beyond what the numbers show)
    note_bits = []
    if first_pos:
        note_bits.append(f"opened with {first_pos} (round {drafted_positions[0]['round']})")
    if early_counts:
        top_pos = max(early_counts, key=early_counts.get)
        note_bits.append(f"leaned {top_pos} early ({early_counts[top_pos]} of first {EARLY_ROUNDS} rounds)")
    # QB/TE timing -- the part RB/WR-centric profiling misses entirely
    for pos in TIMING_POSITIONS:
        tp = timing_prof.get(pos)
        if not tp:
            continue
        if not tp.get("drafted"):
            note_bits.append(f"no {pos} drafted")
        else:
            bit = f"{pos} in R{tp['round']} ({tp['stance']}"
            if tp.get("started_the_run"):
                bit += ", FIRST off the board"
            elif tp.get("gone_before") is not None:
                bit += f", {tp['gone_before']} gone first"
            bit += ")"
            note_bits.append(bit)
    # Reach vs the Yahoo board that year -- the market these owners actually
    # drafted from.
    if reach.get("overall"):
        ov = reach["overall"]
        direction = "reached" if ov["median_reach"] > 0 else "waited"
        note_bits.append(f"typically {direction} {abs(ov['median_reach']):.0f} picks vs Yahoo ADP "
                         f"(median, n={ov['sample']})")
    for pos in TIMING_POSITIONS:
        rp = reach.get(pos)
        if rp and rp["sample"]:
            d = "early" if rp["median_reach"] > 0 else "late"
            note_bits.append(f"{pos} {abs(rp['median_reach']):.0f} picks {d} vs ADP")
    if keepers:
        note_bits.append(f"kept {len(keepers)} player(s): " +
                          ", ".join(f"{k['player']} (R{k['round']})" for k in keepers))
    else:
        note_bits.append("used 0 keepers")
    if missed:
        note_bits.append(f"{missed} missed/auto-passed pick(s)")

    return {
        "manager": manager, "team": team, "team_label_seen": team_label,
        "keepers": keepers,
        "early_position_counts": dict(early_counts),
        "first_position_taken": first_pos,
        "position_timing": timing_prof,
        "reach_vs_adp": reach,
        "missed_picks": missed,
        "strategy_note": "; ".join(note_bits) + ".",
    }


def build(history_path="kepners_draft_history.json", aliases_path="kepners_team_aliases.json",
          out_path="kepners_analysis.json", adp_path="kepners_adp_2025.json"):
    history, aliases = load(history_path, aliases_path)
    seasons = sorted(int(y) for y in history["seasons"].keys())

    all_picks = []
    for y in seasons:
        all_picks.extend(history["seasons"][str(y)]["picks"])

    resolved, unmatched = resolve_picks(all_picks, aliases)

    adp = load_adp(adp_path)
    if adp:
        m, u, misses = attach_adp(resolved, adp)
        pct = round(100 * m / (m + u)) if (m + u) else 0
        print(f"ADP matched for {m}/{m+u} non-keeper picks ({pct}%).")
        if u:
            print(f"  {u} unmatched (no reach score for these): {sorted(set(misses))[:10]}"
                  f"{' ...' if u > 10 else ''}")
    else:
        print(f"NOTE: {adp_path} not found -- reach-vs-ADP skipped. "
              "Run 'Update Big Board Data' first to generate it.")

    # one profile per manager, built from ALL resolved picks across seasons_analyzed
    managers_seen = {}
    for p in resolved:
        if p["manager"] and p["manager"] not in managers_seen:
            managers_seen[p["manager"]] = (p["team"], p["team_label"])

    timing = positional_timing(resolved)
    runs = positional_runs(resolved)

    owners = [
        build_owner_profile(mgr, team, label, resolved, timing)
        for mgr, (team, label) in sorted(managers_seen.items())
    ]

    out = {
        "seasons_analyzed": seasons,
        "position_round_distribution": position_round_distribution(resolved),
        "position_timing": timing,
        "positional_runs": runs,
        "owners": owners,
        "unmatched_team_labels": unmatched,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Analyzed {len(seasons)} season(s): {seasons}. {len(owners)} owner profiles built.")
    for pos, d in timing.items():
        fo = d["first_off_board"]
        print(f"  {pos}: first off board {fo['player']} at overall {fo['overall']} "
              f"(R{fo['round']}, {fo['manager']}); median first pick R{d['median_first_round']}; "
              f"{d['owners_who_drafted']} owner(s) drafted one")
    for r in runs:
        print(f"  RUN: {r['count']} {r['position']}s between overall {r['start_overall']}-{r['end_overall']} "
              f"(started by {r['started_by']})")
    if len(seasons) == 1:
        print("NOTE: only one season of data -- position_round_distribution is a single-season "
              "baseline, not a multi-year average. Re-run once more seasons are parsed in.")
    if unmatched:
        print(f"WARNING -- {len(unmatched)} team label(s) had no alias match: {unmatched}")
    print(f"Wrote {out_path}.")


if __name__ == "__main__":
    hist = sys.argv[1] if len(sys.argv) > 1 else "kepners_draft_history.json"
    aliases = sys.argv[2] if len(sys.argv) > 2 else "kepners_team_aliases.json"
    out = sys.argv[3] if len(sys.argv) > 3 else "kepners_analysis.json"
    adp = sys.argv[4] if len(sys.argv) > 4 else "kepners_adp_2025.json"
    build(hist, aliases, out, adp)
