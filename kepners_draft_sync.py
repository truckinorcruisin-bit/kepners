"""
kepners_draft_sync.py
Pulls the LIVE Kepners "Draft Order and Keepers" Google Sheet (the doc owners
fill in themselves as they claim draft slots and lock in keepers) and writes
kepners_draft_order.json for convert_bigboard.py to merge in.

WHY THIS EXISTS: this is real-time human coordination data (who picks in
which slot, who's keeping which player) that lives in a Google Sheet Sean
doesn't control the format of -- it changes as owners fill it in over the
weeks before the draft. Re-run this workflow any time to pick up the latest
state; there's no need to re-upload anything by hand.

THE SHEET (public, "anyone with the link can view" -- confirmed via a plain
fetch with no login prompt): columns D/F/G/H/I/J on the "2026 Draft Order"
tab hold:
    D = Draft Order (pick number, 1-12)
    F = Team        (manager's short name/nickname, e.g. "Dittoe")
    G = Keeper #1 (player name)      H = Keeper #1 Round
    I = Keeper #2 (player name)      J = Keeper #2 Round
Columns A/B/C are an earlier informal sign-up mechanism + stray chat in the
sheet and are ignored -- D/F/G/H/I/J is the clean, headered table.

FETCH METHOD: Google Sheets' public CSV export endpoint, no auth needed for
a publicly-shared sheet:
    https://docs.google.com/spreadsheets/d/<SHEET_ID>/export?format=csv
This defaults to the FIRST tab in the workbook. If "2026 Draft Order" is ever
not the first tab, pass its gid explicitly (see SHEET_GID below).

NAME MATCHING: the sheet's manager nicknames ("Dittoe") are matched against
the Kepners Team sheet's manager names (from bigboard.json, e.g. "Ditto")
via light normalization + MANAGER_ALIASES. Unmatched managers are flagged in
the output rather than silently dropped or guessed -- same "flag, don't
guess" pattern as convert_bigboard.py's unmatched-player handling.
"""
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone

SHEET_ID = "1PS8ftU0kXOBOtzesCxsA5X2FaXl-0Fzr"
SHEET_GID = None  # set to a specific tab's gid if "2026 Draft Order" isn't first
OUT_FILE = "kepners_draft_order.json"

# Add entries here if a manager's nickname in the Google Sheet doesn't match
# their name in the Big Board's Kepners Team sheet (e.g. "Dittoe" vs "Ditto").
# Key = normalized sheet name, value = normalized Big Board manager name.
MANAGER_ALIASES = {
    "dittoe": "ditto",
}


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def fetch_csv():
    import urllib.request
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    if SHEET_GID:
        url += f"&gid={SHEET_GID}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse(csv_text, known_managers):
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return [], []

    header = rows[0]

    def col(name):
        for i, h in enumerate(header):
            if norm(h) == norm(name):
                return i
        return None

    c_pick = col("Draft Order")
    c_team = col("Team")
    c_k1 = col("Keeper #1")
    c_k1r = col("Keeper #1 Round")
    c_k2 = col("Keeper #2")
    c_k2r = col("Keeper #2 Round")

    if c_pick is None or c_team is None:
        raise ValueError(
            f"Couldn't find 'Draft Order'/'Team' columns in the sheet header: {header}. "
            f"The sheet layout may have changed -- update the column names above."
        )

    known_norm = {norm(m): m for m in known_managers}

    draft_order = []
    unmatched = []
    for row in rows[1:]:
        def cell(idx):
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        team_raw = cell(c_team)
        if not team_raw:
            continue  # blank trailer rows
        pick_raw = cell(c_pick)
        # Draft slots are drawn separately from (and usually later than)
        # keepers being locked in, so a blank pick number here does NOT mean
        # the row is empty -- it just means the slot draw hasn't happened
        # yet. Keep the row with pick=None rather than dropping real keeper
        # data on the floor; the site can show "TBD" for an unassigned slot.
        pick = int(pick_raw) if pick_raw.isdigit() else None

        team_norm = norm(team_raw)
        resolved = known_norm.get(MANAGER_ALIASES.get(team_norm, team_norm))
        if not resolved:
            unmatched.append(team_raw)
            resolved = None

        def as_round(v):
            return int(v) if v.strip().isdigit() else None

        draft_order.append({
            "pick": pick,  # None if the slot hasn't been drawn yet
            "manager_raw": team_raw,
            "manager": resolved,  # None if unmatched -- see unmatched_managers
            "keeper1_name": cell(c_k1) or None,
            "keeper1_round": as_round(cell(c_k1r)),
            "keeper2_name": cell(c_k2) or None,
            "keeper2_round": as_round(cell(c_k2r)),
        })
    # Unassigned slots (pick=None) sort after all assigned ones.
    draft_order.sort(key=lambda r: (r["pick"] is None, r["pick"]))
    return draft_order, sorted(set(unmatched))


def load_known_managers(bigboard_path="bigboard.json"):
    try:
        data = json.load(open(bigboard_path))
        teams = data.get("leagues", {}).get("kepners", {}).get("teams", [])
        return [t["manager"] for t in teams if t.get("manager")]
    except Exception:
        return []


def build(bigboard_path="bigboard.json"):
    known_managers = load_known_managers(bigboard_path)
    if not known_managers:
        print("Warning: no known Kepners managers found in bigboard.json -- "
              "name matching will be skipped and all managers will show as unmatched. "
              "Run Update Big Board first.")
    csv_text = fetch_csv()
    draft_order, unmatched = parse(csv_text, known_managers)

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}",
        "draft_order": draft_order,
        "unmatched_managers": unmatched,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {OUT_FILE}: {len(draft_order)} draft slots.")
    if unmatched:
        print(f"WARNING -- {len(unmatched)} manager name(s) in the sheet didn't match a known "
              f"Kepners manager: {unmatched}. Add an alias in MANAGER_ALIASES or fix the sheet.")
    filled_keepers = sum(1 for r in draft_order if r["keeper1_name"] or r["keeper2_name"])
    print(f"{filled_keepers}/{len(draft_order)} teams have at least one keeper entered so far.")


if __name__ == "__main__":
    bigboard = sys.argv[1] if len(sys.argv) > 1 else "bigboard.json"
    build(bigboard)
