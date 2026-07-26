"""
predict_kepners_keepers.py
Predicts each Kepners owner's 2 most likely keepers for the upcoming season,
using their PRIOR season's final roster as ground truth for what they
actually have to choose from. Exists because owners are consistently late
entering real keepers into the Google Sheet, which makes it hard to gauge
draft-pick value ahead of time -- this fills the gap with an educated guess
until the real data shows up.

INPUT:  kepners_roster_snapshot_<season>.json
        { "season": 2025,
          "source": "<where this came from -- PDF export, Yahoo API, etc>",
          "rosters": { "<team name>": [
              {"player": "...", "draft_position": "R.P" or null, "current_o_rank": int},
              ...
          ], ... } }
        draft_position uses "round.pick" (e.g. "12.6" = round 12, pick 6).
        null/missing draft_position means the player was a waiver/FA pickup
        that season -- INELIGIBLE to keep per Kepners rules, and excluded.

OUTPUT: kepners_suggested_keepers.json
        Consumed directly by index.html (see KEPS global / renderDraftOrder).
        Real Google Sheet keepers always take priority over these; this file
        only fills in for managers with no confirmed keeper yet.

METHODOLOGY:
  keeper_cost_round = max(1, drafted_round - 2)   [Kepners rule]
  expected_rank     = (keeper_cost_round - 1) * teams + (teams + 1) / 2
                      (the midpoint overall pick number of that round --
                       i.e. "what a pick at this cost would typically deliver")
  surplus           = expected_rank - current_o_rank
                      (positive & large = current value far exceeds what
                       you'd expect for that keeper-round cost -- a bargain)
  Top 2 players per manager by surplus = the prediction.

  This deliberately rewards VALUE RELATIVE TO COST, not raw player quality --
  a round-1 keeper is not actually a good keeper VALUE, since a round-1 slot
  is expected to produce a great player anyway regardless of who you kept.

VALIDATION (2025->2026 run): tested against Sean's own already-confirmed
2026 keepers (the one case with real ground truth available). This model's
top-2 picks for "Mullins" -- Luther Burden III, Bucky Irving -- exactly
matched what Sean had already entered for real. Re-validate the same way
each year if convenient: run this BEFORE checking the Google Sheet, then
compare once your own keepers are confirmed.

USAGE (run this once per offseason, before draft prep begins):
  1. Get a prior-season final-rosters export (currently: a league PDF export,
     hand-transcribed into kepners_roster_snapshot_<season>.json -- see NOTE
     below). Update `avg overall consensus rank` per player using this
     season's Big Board / ESPN values (current_o_rank field).
  2. python predict_kepners_keepers.py kepners_roster_snapshot_2026.json
  3. Commit the resulting kepners_suggested_keepers.json -- index.html picks
     it up automatically, no code changes needed.

NOTE for future years: once Yahoo API access is live and confirmed working,
yahoo_kepners_history.py should be able to pull final rosters + draft
positions directly and emit the same kepners_roster_snapshot_<season>.json
shape -- at that point this manual PDF-transcription step can be retired.
Current_o_rank would still need to come from that year's Big Board/ESPN pull
(convert_bigboard.py / espn_player_values.py), since it reflects the
UPCOMING season's outlook, not the season being snapshotted.
"""
import json
import re
import sys

TEAMS_COUNT = 12  # Kepners league size


def parse_round(draft_position):
    """'12.6' -> 12 (round). None/missing (waiver/FA pickup) -> None (ineligible)."""
    if not draft_position:
        return None
    m = re.match(r"(\d+)\.\d+", str(draft_position))
    return int(m.group(1)) if m else None


def keeper_cost_round(drafted_round, cost_rounds_advance=2):
    return max(1, drafted_round - cost_rounds_advance)


def expected_rank_for_round(round_num, teams=TEAMS_COUNT):
    return (round_num - 1) * teams + (teams + 1) / 2


def predict_team_keepers(players, top_n=2, cost_rounds_advance=2):
    candidates = []
    for p in players:
        drafted_round = parse_round(p.get("draft_position"))
        if drafted_round is None:
            continue  # ineligible: undrafted/waiver pickup
        o_rank = p.get("current_o_rank")
        if o_rank is None:
            continue  # can't score without a current-value estimate
        cost_round = keeper_cost_round(drafted_round, cost_rounds_advance)
        expected = expected_rank_for_round(cost_round)
        surplus = round(expected - o_rank, 1)
        candidates.append({
            "player": p["player"],
            "round": cost_round,
            "surplus": surplus,
        })
    candidates.sort(key=lambda c: -c["surplus"])
    return candidates[:top_n]


def team_name_to_manager(team_name, aliases):
    """Prefix-match a roster-snapshot team name against the (often truncated)
    labels in kepners_team_aliases.json."""
    def norm(s):
        return s.lower().replace("\u2019", "'").replace("...", "").strip()
    tn = norm(team_name)
    for label, info in aliases.items():
        if label == "_comment":
            continue
        ln = norm(label)
        if tn.startswith(ln) or ln.startswith(tn[:10]):
            return info["manager"]
    return None


def main(snapshot_path, aliases_path="kepners_team_aliases.json",
         out_path="kepners_suggested_keepers.json"):
    with open(snapshot_path) as f:
        snapshot = json.load(f)
    with open(aliases_path) as f:
        aliases = json.load(f)

    by_manager = {}
    unmatched_teams = []
    for team_name, players in snapshot["rosters"].items():
        manager = team_name_to_manager(team_name, aliases)
        if manager is None:
            unmatched_teams.append(team_name)
            continue
        by_manager[manager] = predict_team_keepers(players)

    if unmatched_teams:
        print(f"WARNING: {len(unmatched_teams)} team name(s) in the snapshot "
              f"didn't match a known manager alias -- check for a naming "
              f"change: {unmatched_teams}")

    out = {
        "generated": snapshot.get("season"),
        "source_season": snapshot.get("season"),
        "target_season": (snapshot.get("season") or 0) + 1,
        "methodology": (
            "For each player on a manager's actual end-of-season roster who "
            "was genuinely drafted that year (waiver/FA pickups are "
            "ineligible to keep), keeper cost = drafted round - 2. "
            "Surplus = (expected overall rank for a pick at that keeper-cost "
            "round) - (current consensus overall rank). Top 2 by surplus per "
            "manager. Rewards value relative to keeper cost, not raw player "
            "quality."
        ),
        "source_note": snapshot.get("source", "unknown source"),
        "by_manager": by_manager,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {out_path}: {len(by_manager)} managers, "
          f"{len(unmatched_teams)} unmatched team name(s)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python predict_kepners_keepers.py <kepners_roster_snapshot_YYYY.json>")
        sys.exit(1)
    main(sys.argv[1])
