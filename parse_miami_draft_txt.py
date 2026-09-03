"""
parse_miami_draft_txt.py
Parses a Miami Domers draft-results .txt export into
miami_draft_history.json, matching the schema kepners_draft_history.json
already uses (so downstream analysis can treat both leagues the same way).

INPUT FORMAT -- the platform export groups picks under a team header:

    Hannah Lee's Revenge
    1.  (5)  Christian McCaffrey (SF - RB)
    2.  (12) Kenneth Walker III (KC - RB)

The bare number is the ROUND, the parenthesised number is the OVERALL pick,
and the trailing parenthetical is "PRO_TEAM - POSITION".

WHY TEAM LABELS AREN'T THE JOIN KEY: owners rename teams between (and during)
seasons -- "Ya Boi" and "Nearys Nemesis" are the same roster, and
"Monkey Business" is the team bigboard.json calls "BagelNDeli". Matching on
name would silently split one owner's history in two. Instead teams are keyed
by the ORDER THEY APPEAR in the export, which is draft-slot order (team 1
holds pick 1, team 2 holds pick 2, ...), and that maps cleanly onto
bigboard.json's `draftPosition`. Verified against the 2026 file: the eight
headers appear in exactly slot order 1-8.

KEEPERS: the export DOES mark them, with a private-use Unicode character
(U+E03E) appended to the player name -- the platform renders it as a small
keeper icon, and it survives copy/paste as an invisible-looking glyph. That
marker is the primary signal: it's exact, and it can't confuse a keeper with
the same player legitimately drafted by someone else. Verified on the 2026
export: exactly 10 markers, matching all 10 confirmed keepers at the right
rounds. miami_suggested_keepers.json is then used only as a CROSS-CHECK, so a
disagreement between the two is reported rather than silently resolved.

A keeper consumes its owner's pick in that round, which is why those picks
otherwise look like ordinary selections in the export.

USAGE:
    python parse_miami_draft_txt.py "Miami Draft Results.txt" 2026
"""
import json
import re
import sys

TEAMS = 8
PICK_RE = re.compile(r"^\s*(\d+)\.\s*\((\d+)\)\s*(.+?)\s*\(([A-Za-z]+)\s*-\s*([A-Za-z/]+)\)\s*$")
# The platform's keeper icon, appended to the player name in the export.
KEEPER_MARK = "\ue03e"


def normalize(name):
    """Reuse the project's canonical matcher so 'Kenneth Walker III' joins to
    the Big Board's 'Kenneth Walker' and 'Travis Etienne Jr.' to
    'Travis Etienne'."""
    try:
        from convert_bigboard import normalize_name
        return normalize_name(name)
    except Exception:
        s = str(name).lower()
        s = re.sub(r"[.'''\-]", "", s)
        s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
        return re.sub(r"\s+", " ", s).strip()


def load_keeper_index(path="miami_suggested_keepers.json"):
    """{normalized_player: cost_round} for every CONFIRMED keeper."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"  (no {path} -- keepers will not be flagged)")
        return {}
    out = {}
    for manager, picks in (data.get("by_manager") or {}).items():
        for p in picks:
            if p.get("confirmed") and p.get("player"):
                out[normalize(p["player"])] = p.get("round")
    return out


def load_manager_by_slot(path="bigboard.json"):
    """{draft_slot: manager} so parsed teams carry a stable owner identity
    rather than only a renameable team label."""
    try:
        with open(path) as f:
            bb = json.load(f)
    except FileNotFoundError:
        return {}
    return {
        t["draftPosition"]: t.get("manager")
        for t in bb.get("leagues", {}).get("miami", {}).get("teams", [])
        if t.get("draftPosition")
    }


def parse(txt_path):
    teams = []          # [{label, picks:[...]}] in file order == draft slot order
    current = None
    with open(txt_path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            m = PICK_RE.match(line)
            if m:
                if current is None:
                    raise SystemExit(f"Pick line before any team header: {line!r}")
                rnd, overall, player, pro, pos = m.groups()
                marked = KEEPER_MARK in player
                # Strip the marker (and any private-use glyph generally) so
                # the stored name joins cleanly against the Big Board.
                clean = "".join(c for c in player if not (0xE000 <= ord(c) <= 0xF8FF))
                current["picks"].append({
                    "round": int(rnd),
                    "overall_pick": int(overall),
                    "player": re.sub(r"\s+", " ", clean).strip(),
                    "pro_team": pro.upper(),
                    "position": pos.upper(),
                    "marked_keeper": marked,
                })
            else:
                # Not a pick line -> a team header. Guard against stray text:
                # a real header is always followed by pick lines, so a header
                # with zero picks would surface as an empty team below.
                current = {"label": line, "picks": []}
                teams.append(current)
    return teams


def main(txt_path, season):
    keeper_idx = load_keeper_index()
    mgr_by_slot = load_manager_by_slot()

    teams = parse(txt_path)
    empty = [t["label"] for t in teams if not t["picks"]]
    if empty:
        raise SystemExit(f"Team header(s) with no picks -- check the export format: {empty}")
    if len(teams) != TEAMS:
        print(f"  WARNING: parsed {len(teams)} teams, expected {TEAMS}")

    picks = []
    keepers_found = []
    mismatches = []
    for slot, t in enumerate(teams, start=1):
        manager = mgr_by_slot.get(slot)
        for p in t["picks"]:
            norm = normalize(p["player"])
            cost = keeper_idx.get(norm)
            # The export's own marker is authoritative. The suggested-keepers
            # file is a cross-check only: agreement is expected, and any
            # disagreement is surfaced rather than quietly picking a winner,
            # since either side could be the stale one.
            is_keeper = p["marked_keeper"]
            expected = cost is not None and cost == p["round"]
            if is_keeper and not expected:
                mismatches.append(
                    f"{manager or t['label']}: {p['player']} R{p['round']} marked a keeper "
                    f"in the export but {'costed R'+str(cost) if cost is not None else 'not listed'} "
                    f"in miami_suggested_keepers.json")
            if expected and not is_keeper:
                mismatches.append(
                    f"{manager or t['label']}: {p['player']} R{p['round']} expected a keeper "
                    f"from miami_suggested_keepers.json but not marked in the export")
            if is_keeper:
                keepers_found.append(f"{manager or t['label']}: {p['player']} (R{p['round']})")
            picks.append({
                "round": p["round"],
                "pick_in_round": ((p["overall_pick"] - 1) % TEAMS) + 1,
                "overall_pick": p["overall_pick"],
                "team_label": t["label"],
                "draft_slot": slot,
                "manager": manager,
                "player": p["player"],
                "pro_team": p["pro_team"],
                "position": p["position"],
                "is_keeper": is_keeper,
            })

    picks.sort(key=lambda x: x["overall_pick"])
    out = {"seasons": {str(season): {"draft_type": "snake", "teams": TEAMS,
                                     "source": txt_path, "picks": picks}}}
    with open("miami_draft_history.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote miami_draft_history.json: {len(picks)} picks across {len(teams)} teams.")
    unmapped = sorted({t['label'] for t, s in zip(teams, range(1, len(teams)+1))
                       if not mgr_by_slot.get(s)})
    if unmapped:
        print(f"  WARNING: no manager mapped for slot(s) with label(s): {unmapped}")
    print(f"  Flagged {len(keepers_found)} keeper(s) from the export's own marker:")
    for k in keepers_found:
        print(f"    {k}")
    if mismatches:
        print(f"  WARNING: {len(mismatches)} keeper disagreement(s) vs miami_suggested_keepers.json:")
        for m in mismatches:
            print(f"    {m}")
    else:
        print("  Cross-check: export markers agree with miami_suggested_keepers.json.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python parse_miami_draft_txt.py "<draft results.txt>" [season]')
        sys.exit(1)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 2026)
