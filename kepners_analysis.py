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
import sys
from collections import defaultdict

EARLY_ROUNDS = 6  # "early" draft window used for positional-lean signal


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


def build_owner_profile(manager, team, team_label, picks):
    my_picks = [p for p in picks if p["manager"] == manager]
    my_picks.sort(key=lambda p: p["round"])

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

    # lightweight strategy note from the actual numbers (same spirit as
    # zimmer_analysis.py's generated notes -- describe what happened, don't
    # editorialize beyond what the numbers show)
    note_bits = []
    if first_pos:
        note_bits.append(f"opened with {first_pos} (round {drafted_positions[0]['round']})")
    if early_counts:
        top_pos = max(early_counts, key=early_counts.get)
        note_bits.append(f"leaned {top_pos} early ({early_counts[top_pos]} of first {EARLY_ROUNDS} rounds)")
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
        "missed_picks": missed,
        "strategy_note": "; ".join(note_bits) + ".",
    }


def build(history_path="kepners_draft_history.json", aliases_path="kepners_team_aliases.json",
          out_path="kepners_analysis.json"):
    history, aliases = load(history_path, aliases_path)
    seasons = sorted(int(y) for y in history["seasons"].keys())

    all_picks = []
    for y in seasons:
        all_picks.extend(history["seasons"][str(y)]["picks"])

    resolved, unmatched = resolve_picks(all_picks, aliases)

    # one profile per manager, built from ALL resolved picks across seasons_analyzed
    managers_seen = {}
    for p in resolved:
        if p["manager"] and p["manager"] not in managers_seen:
            managers_seen[p["manager"]] = (p["team"], p["team_label"])

    owners = [
        build_owner_profile(mgr, team, label, resolved)
        for mgr, (team, label) in sorted(managers_seen.items())
    ]

    out = {
        "seasons_analyzed": seasons,
        "position_round_distribution": position_round_distribution(resolved),
        "owners": owners,
        "unmatched_team_labels": unmatched,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Analyzed {len(seasons)} season(s): {seasons}. {len(owners)} owner profiles built.")
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
    build(hist, aliases, out)
