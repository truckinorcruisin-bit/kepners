"""
parse_kepners_draft_txt.py
Parses a Yahoo "draft results" text export (copy/pasted from the Yahoo draft
recap page) into structured JSON, for the Kepners league. Reusable across
seasons -- Yahoo's export format is consistent year to year.

FORMAT (as observed in the 2025 export):
    Round <N>
    <pick>.\t<Player Name>[KEEPER_MARKER]
    (<ProTeam> - <POS>)
    <Team/Owner Label>
    ... repeated for all 12 picks, then next "Round <N>" ...

Two wrinkles handled:
  1. KEEPER MARKER: Yahoo's UI shows a small keeper badge/icon next to kept
     players. When copy/pasted as plain text, that icon degrades into the
     private-use Unicode codepoint U+E03E (invisible in most renderers --
     confirmed by scanning the raw bytes of the 2025 export). Any player name
     ending in that codepoint is a keeper; the codepoint is stripped from the
     stored name.
  2. MISSED PICKS: an empty/auto-passed pick renders as a single line
     "<pick>.\t--empty--\t<Team Label>" instead of the normal 3-line block
     (no player, no pro-team/position line). Handled explicitly -- these
     produce a pick record with player=None rather than crashing the parser.

TEAM LABELS: Yahoo's export truncates long team names with "..." (e.g.
"Denim Like A..."). This parser does NOT attempt to match these against the
Big Board's manager names -- that mapping needs a human to confirm once
(team names can be reused/renamed season to season and the truncation makes
automatic matching unreliable). The raw truncated label is stored as-is,
consistent every round for the same team, so per-owner grouping within a
single season's history is still accurate; cross-referencing to real
managers is a separate, explicit step (see kepners_team_aliases.json).

OUTPUT: kepners_draft_history.json (or merges into it if it already exists),
keyed by season year:
{
  "seasons": {
    "2025": {
      "draft_type": "snake",
      "picks": [
        {"round":1,"pick_in_round":1,"overall_pick":1,"team_label":"Denim Like A...",
         "player":"Saquon Barkley","pro_team":"PHI","position":"RB","is_keeper":false},
        ...
      ]
    }
  }
}

USAGE: python3 parse_kepners_draft_txt.py <draft_results.txt> [season_year] [out.json]
"""
import json
import os
import re
import sys

KEEPER_MARKER = "\ue03e"


def parse(text, teams_per_round=12):
    lines = [l.rstrip("\r") for l in text.split("\n")]
    picks = []
    round_num = None
    i = 0
    overall = 0
    while i < len(lines):
        line = lines[i].strip()

        m_round = re.match(r"^Round\s+(\d+)$", line)
        if m_round:
            round_num = int(m_round.group(1))
            i += 1
            continue

        m_pick = re.match(r"^(\d+)\.\t(.+)$", lines[i])
        if m_pick and round_num is not None:
            pick_in_round = int(m_pick.group(1))
            rest = m_pick.group(2)
            overall += 1

            if rest.startswith("--empty--"):
                # single-line missed-pick case: "--empty--\t<Team Label>"
                parts = rest.split("\t")
                team_label = parts[1].strip() if len(parts) > 1 else None
                picks.append({
                    "round": round_num, "pick_in_round": pick_in_round,
                    "overall_pick": overall, "team_label": team_label,
                    "player": None, "pro_team": None, "position": None,
                    "is_keeper": False,
                })
                i += 1
                continue

            player_raw = rest
            is_keeper = player_raw.endswith(KEEPER_MARKER)
            player = player_raw.rstrip(KEEPER_MARKER).strip()

            # next non-blank line: "(ProTeam - POS)"
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            m_team = re.match(r"^\((\w+)\s*-\s*(\w+)\)$", lines[j].strip()) if j < len(lines) else None
            pro_team = m_team.group(1).upper() if m_team else None
            position = m_team.group(2).upper() if m_team else None

            # next non-blank line after that: team/owner label
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            team_label = lines[k].strip() if k < len(lines) else None

            picks.append({
                "round": round_num, "pick_in_round": pick_in_round,
                "overall_pick": overall, "team_label": team_label,
                "player": player, "pro_team": pro_team, "position": position,
                "is_keeper": is_keeper,
            })
            i = k + 1
            continue

        i += 1

    return picks


def validate(picks, teams_per_round=12):
    warnings = []
    by_round = {}
    for p in picks:
        by_round.setdefault(p["round"], []).append(p)
    for rnd, rps in sorted(by_round.items()):
        if len(rps) != teams_per_round:
            warnings.append(f"Round {rnd} has {len(rps)} picks, expected {teams_per_round}.")
        seen_slots = sorted(p["pick_in_round"] for p in rps)
        if seen_slots != list(range(1, teams_per_round + 1)):
            warnings.append(f"Round {rnd} pick slots look off: {seen_slots}")
    n_keepers = sum(1 for p in picks if p["is_keeper"])
    n_empty = sum(1 for p in picks if p["player"] is None)
    return warnings, n_keepers, n_empty


def build(src, season, out_path, teams_per_round=12):
    text = open(src, encoding="utf-8").read()
    picks = parse(text, teams_per_round)
    warnings, n_keepers, n_empty = validate(picks, teams_per_round)

    history = {}
    if os.path.exists(out_path):
        history = json.load(open(out_path))
    history.setdefault("seasons", {})
    history["seasons"][str(season)] = {"draft_type": "snake", "picks": picks}

    with open(out_path, "w") as f:
        json.dump(history, f, indent=2)

    print(f"Parsed {len(picks)} picks across {len(set(p['round'] for p in picks))} rounds "
          f"for season {season}.")
    print(f"Keepers found: {n_keepers}. Missed/empty picks: {n_empty}.")
    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(" -", w)
    else:
        print("No structural warnings -- every round has a full, contiguous set of picks.")
    print(f"Wrote {out_path}.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 parse_kepners_draft_txt.py <draft_results.txt> [season_year] [out.json]")
        sys.exit(1)
    src = sys.argv[1]
    season = sys.argv[2] if len(sys.argv) > 2 else "2025"
    out_path = sys.argv[3] if len(sys.argv) > 3 else "kepners_draft_history.json"
    build(src, season, out_path)
