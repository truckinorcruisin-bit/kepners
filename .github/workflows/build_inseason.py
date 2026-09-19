"""
build_inseason.py
The in-season equivalent of convert_bigboard.py: takes the raw platform pulls
plus the global projection file and emits ONE normalized artifact the site
reads, inseason_<year>.json.

INPUTS (all optional -- a missing one degrades that league, not the run):
    yahoo_inseason_<year>.json   rosters/FA for Kepners + Miami
    espn_inseason_<year>.json    rosters/FA for Zimmer
    player_ros_<year>.json       rest-of-season projections for everybody
    bigboard.json                roster shapes (already merged from the Excel
                                 Team sheets + league_rules.json)

OUTPUT: inseason_<year>.json

WHAT THIS ADDS BEYOND MERGING
  1. Name-joins every rostered and free-agent player to a ROS projection,
     reusing normalize_name() from convert_bigboard.py so an in-season join
     can't silently diverge from the draft-season one.
  2. Computes each team's OPTIMAL starting lineup from its roster.
  3. Ranks my starter at every starting slot against the same slot on every
     other team -- the "where do I need to improve" read. This is the headline
     number for the Manager Cockpit overview.

WHY OPTIMAL LINEUP RATHER THAN WHO THEY ACTUALLY STARTED
An opponent's lineup this week reflects their attention span as much as their
roster. For "how strong is this team", what matters is the talent they hold,
not whether they remembered to bench a player on bye. Actual lineup_slot is
still carried through in the player rows for the trade engine, which genuinely
does care whether an owner rates a player enough to start him.

NO NETWORK ACCESS IS USED HERE. This script is pure transformation, which is
also what makes it testable without live API credentials.
"""
import os
import sys
import json
from datetime import datetime, timezone

from convert_bigboard import normalize_name

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"inseason_{YEAR}.json"

# Which slot a position may occupy. FLEX is filled after dedicated slots.
FLEX_ELIGIBLE = {"RB", "WR", "TE"}
# Positions we rank and recommend on. K/DEF are carried on rosters but excluded
# from positional strength for the same reason they're excluded from the draft
# optimizer: their supply is elastic and week-to-week streamable, so "your DEF
# ranks 9th" is not an actionable weakness the way "your RB2 ranks 11th" is.
STRENGTH_POSITIONS = {"QB", "RB", "WR", "TE"}

# Yahoo uses terse codes, ESPN uses words. Normalized so the UI has one
# vocabulary and an OUT player can be discounted consistently in both.
INJURY_MAP = {
    "Q": "QUESTIONABLE", "D": "DOUBTFUL", "O": "OUT", "IR": "OUT",
    "NA": "OUT", "PUP": "OUT", "SUSP": "OUT",
    "QUESTIONABLE": "QUESTIONABLE", "DOUBTFUL": "DOUBTFUL", "OUT": "OUT",
    "INJURY_RESERVE": "OUT", "ACTIVE": None, "NORMAL": None,
}
# How much of a player's ROS to trust given his status. DOUBTFUL/OUT are about
# this week, not the rest of the season, so the haircut is deliberately mild --
# it should reorder close calls, not write a star off over one missed game.
INJURY_DISCOUNT = {"QUESTIONABLE": 0.97, "DOUBTFUL": 0.90, "OUT": 0.75}


def load_json(path, label):
    if not os.path.exists(path):
        print(f"  {label}: {path} not found -- skipping.")
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"::warning::{label}: {path} failed to parse ({e}) -- skipping.")
        return None


def canonical_pos(pos):
    """Position codes vary by source: ESPN says D/ST, Yahoo says DEF, and the
    Big Board workbook historically used DE and K-. One vocabulary here."""
    if not pos:
        return None
    p = str(pos).strip().upper()
    if p in ("D/ST", "DST", "DEF", "DE", "D"):
        return "DEF"
    if p in ("K", "K-", "PK"):
        return "K"
    if "/" in p:                      # Yahoo multi-eligibility, e.g. "WR/TE"
        p = p.split("/")[0].strip()
    return p


# Defenses are the one systematic name mismatch across sources: ESPN calls them
# "Texans D/ST", Yahoo says "Houston", and the Big Board says "Houston Texans".
# normalize_name() can't fix this -- it strips punctuation and suffixes, not
# nicknames -- so defenses get their own join key: the team NICKNAME alone.
DST_TOKENS = ("d/st", "dst", "d st", "defense", "def")
NFL_NICKNAMES = {
    "cardinals", "falcons", "ravens", "bills", "panthers", "bears", "bengals",
    "browns", "cowboys", "broncos", "lions", "packers", "texans", "colts",
    "jaguars", "chiefs", "raiders", "chargers", "rams", "dolphins", "vikings",
    "patriots", "saints", "giants", "jets", "eagles", "steelers", "49ers",
    "seahawks", "buccaneers", "titans", "commanders",
}


# City-only fallback, for a source that names a defense "Houston" with no
# nickname. Deliberately EXCLUDES the shared markets -- New York (Giants/Jets)
# and Los Angeles (Rams/Chargers) are genuinely ambiguous from the city alone,
# and guessing there would join a real roster slot to the wrong defense.
CITY_TO_NICKNAME = {
    "arizona": "cardinals", "atlanta": "falcons", "baltimore": "ravens",
    "buffalo": "bills", "carolina": "panthers", "chicago": "bears",
    "cincinnati": "bengals", "cleveland": "browns", "dallas": "cowboys",
    "denver": "broncos", "detroit": "lions", "green bay": "packers",
    "houston": "texans", "indianapolis": "colts", "jacksonville": "jaguars",
    "kansas city": "chiefs", "las vegas": "raiders", "miami": "dolphins",
    "minnesota": "vikings", "new england": "patriots", "new orleans": "saints",
    "philadelphia": "eagles", "pittsburgh": "steelers",
    "san francisco": "49ers", "seattle": "seahawks", "tampa bay": "buccaneers",
    "tennessee": "titans", "washington": "commanders",
}


def defense_key(name):
    """Team nickname out of any of the three defense naming conventions.

    Returns None when no known nickname is present, so a genuine miss stays a
    miss rather than silently joining to whatever the last word happened to be
    -- a wrong defense join is worse than an unmatched one, because it looks
    like real data.
    """
    if not name:
        return None
    n = str(name).lower()
    for tok in DST_TOKENS:
        n = n.replace(tok, " ")
    for word in reversed(n.replace("-", " ").split()):
        w = "".join(ch for ch in word if ch.isalnum())
        if w in NFL_NICKNAMES:
            return w
    cleaned = " ".join(n.replace("-", " ").split())
    return CITY_TO_NICKNAME.get(cleaned)


def join_key(name, pos):
    """The key a player is indexed and looked up under."""
    if canonical_pos(pos) == "DEF":
        dk = defense_key(name)
        if dk:
            return f"dst:{dk}"
    return normalize_name(name or "")


def norm_injury(raw):
    if not raw:
        return None
    return INJURY_MAP.get(str(raw).strip().upper(), None)


def build_ros_index(ros_doc):
    """Normalized name -> projection row.

    On a duplicate name the higher ROS wins. That's the right tiebreak for the
    real collision case (a practice-squad body sharing a name with a starter),
    and it's deterministic, which matters more than being clever: a join that
    picks a different player run to run would make every downstream
    recommendation unreproducible.
    """
    index = {}
    for p in (ros_doc or {}).get("players", []):
        key = join_key(p.get("name"), p.get("position"))
        if not key:
            continue
        prev = index.get(key)
        if prev is None or (p.get("ros_points") or 0) > (prev.get("ros_points") or 0):
            index[key] = p
    return index


def enrich(player, ros_index):
    """Attach projection + normalized status to one roster/FA row."""
    pos = canonical_pos(player.get("position"))
    row = dict(player)
    row["position"] = pos
    row["injury_status"] = norm_injury(player.get("injury_status"))

    match = ros_index.get(join_key(player.get("name"), pos))
    if match:
        row["ros_points"] = match.get("ros_points")
        row["ros_per_week"] = match.get("ros_per_week")
        row["trend"] = match.get("trend")
        row["season_points_actual"] = match.get("season_points_actual")
        # percent_owned is the free-agent engine's sanity check: a "free agent"
        # at 80% owned across ESPN is almost certainly a stale pull.
        if row.get("percent_owned") is None:
            row["percent_owned"] = match.get("percent_owned")
        if not row.get("injury_status"):
            row["injury_status"] = norm_injury(match.get("injury_status"))
        row["ros_matched"] = True
    else:
        row["ros_points"] = None
        row["ros_per_week"] = None
        row["trend"] = None
        row["ros_matched"] = False

    # Effective value = what this player is worth to a lineup right now.
    base = row.get("ros_per_week")
    if base is None:
        row["effective_per_week"] = None
    else:
        row["effective_per_week"] = round(
            base * INJURY_DISCOUNT.get(row.get("injury_status"), 1.0), 2)
    return row


def optimal_lineup(roster, roster_slots):
    """Greedy best legal lineup: dedicated slots first, then FLEX.

    Greedy is exact here, not an approximation, because the slot structure is a
    simple hierarchy -- every FLEX-eligible position also has its own dedicated
    slots, so taking the best player at each dedicated slot can never strand a
    better FLEX option than the one greedy then picks up. (This is NOT true in
    general slot systems; if a superflex/QB-eligible FLEX ever appears in one
    of these leagues, this needs to become a real assignment solve.)

    Returns [{slot, slot_label, player}] with player=None for an unfillable slot.
    """
    pool = [p for p in roster if p.get("effective_per_week") is not None]
    pool.sort(key=lambda p: p["effective_per_week"], reverse=True)
    used = set()
    out = []

    # Dedicated slots, in roster order, so RB1 is always the better of the two.
    counter = {}
    for slot in roster_slots:
        if slot in ("BEN", "IR", "FLEX"):
            continue
        counter[slot] = counter.get(slot, 0) + 1
        label = f"{slot}{counter[slot]}" if roster_slots.count(slot) > 1 else slot
        pick = next((p for i, p in enumerate(pool)
                     if i not in used and p.get("position") == slot), None)
        if pick is not None:
            used.add(pool.index(pick))
        out.append({"slot": slot, "slot_label": label, "player": pick})

    flex_n = roster_slots.count("FLEX")
    for n in range(flex_n):
        label = f"FLEX{n+1}" if flex_n > 1 else "FLEX"
        pick = next((p for i, p in enumerate(pool)
                     if i not in used and p.get("position") in FLEX_ELIGIBLE), None)
        if pick is not None:
            used.add(pool.index(pick))
        out.append({"slot": "FLEX", "slot_label": label, "player": pick})

    return out


def positional_strength(teams, roster_slots):
    """For every starting slot, rank each team's starter league-wide.

    Ranked per SLOT LABEL rather than per position: your RB1 is measured
    against every other team's RB1, not against the whole pool of rostered
    RBs. That's the comparison that answers "where do I need to improve",
    because RB1 and RB2 are different jobs with different market prices.
    """
    lineups = {t["team_id"]: optimal_lineup(t["roster"], roster_slots) for t in teams}
    labels = [s["slot_label"] for s in next(iter(lineups.values()), [])]

    by_label = {}
    for label in labels:
        vals = []
        for tid, lineup in lineups.items():
            slot = next((s for s in lineup if s["slot_label"] == label), None)
            player = slot["player"] if slot else None
            vals.append({
                "team_id": tid,
                "player": player["name"] if player else None,
                "value": player["effective_per_week"] if player else 0.0,
            })
        # Descending: rank 1 is the strongest starter at this slot.
        vals.sort(key=lambda v: v["value"], reverse=True)
        for i, v in enumerate(vals):
            v["rank"] = i + 1
        by_label[label] = vals

    out = {}
    for t in teams:
        rows = []
        for label in labels:
            entries = by_label[label]
            mine = next(e for e in entries if e["team_id"] == t["team_id"])
            values = [e["value"] for e in entries]
            n = len(values)
            median = sorted(values)[n // 2] if n else 0.0
            pos = label.rstrip("0123456789") or label
            rows.append({
                "slot": label,
                "position": pos,
                "player": mine["player"],
                "perWeek": round(mine["value"], 2),
                "rank": mine["rank"],
                "of": n,
                "leagueMedian": round(median, 2),
                "gapToMedian": round(mine["value"] - median, 2),
                "leagueBest": round(max(values), 2) if values else 0.0,
                # Min AND max, so the UI can plot where this player actually
                # falls within the league's real spread at this slot rather
                # than only showing an ordinal. A rank is close to meaningless
                # without the range behind it: QB2-of-12 sounds like a
                # strength, but when the whole league sits between 18.0 and
                # 22.3 per week it is worth well under a point a game.
                "leagueMin": round(min(values), 2) if values else 0.0,
                "leagueMax": round(max(values), 2) if values else 0.0,
                # Only QB/RB/WR/TE/FLEX are worth acting on -- see
                # STRENGTH_POSITIONS. K/DEF are carried but flagged so the UI
                # can grey them out instead of prompting a trade for a kicker.
                "actionable": pos in STRENGTH_POSITIONS or pos == "FLEX",
            })
        out[t["team_id"]] = rows
    return out, lineups


def roster_slots_for(key, bigboard, rules):
    """Roster shape, preferring bigboard.json.

    Only Zimmer's shape lives in league_rules.json; Kepners' and Miami's come
    off their Big Board Team sheets and are merged into bigboard.json by
    convert_bigboard.py. Reading bigboard first means all three resolve from
    one place and nobody has to hand-maintain a second copy that can drift.
    """
    lg = ((bigboard or {}).get("leagues") or {}).get(key) or {}
    slots = lg.get("rosterSlots") or []
    if slots:
        return list(slots)
    return list((rules.get(key, {}) or {}).get("rosterSlots") or [])


def depth_chart(teams, lineups):
    """Rank every player by POSITIONAL DEPTH INDEX across the league.

    The starting-lineup view answers "is my RB1 good?". This answers "is my
    RB3 good?" -- which is the bench question, and it is a real question:
    bench depth is what covers a bye, absorbs an injury, and gives you
    something to trade from.

    METHOD -- deliberately the same one used for starters, extended past the
    starting slots. Within each team, sort that team's players at a position
    by value: RB1, RB2, RB3... Then rank my RB3 against every other team's
    RB3. Comparing like-for-like depth slots is what makes the number mean
    something; ranking a bench RB against the league's whole RB pool would
    just tell you he isn't a starter, which you already knew.

    Two wrinkles worth knowing about:

    1. DEPTH INDEX IS NOT THE SAME AS LINEUP SLOT, because of FLEX. A player
       can be your RB3 by depth and still be starting at FLEX. Each entry is
       therefore tagged `starting` from the actual optimal lineup rather than
       inferred from the index, and the UI lists only the non-starters as
       bench -- otherwise a FLEX starter would appear in both sections and
       read as though you rostered him twice.

    2. TEAMS HAVE UNEQUAL DEPTH. If only 5 teams roster a 4th RB, then RB4 is
       a 12-way comparison where 7 teams hold nothing. Those are scored 0 and
       rank last, which is correct -- holding a playable 4th RB when most of
       the league doesn't IS an advantage -- but it makes deep indices
       progressively less meaningful. `holding` records how many teams
       actually have someone there so the UI can say so instead of implying a
       clean 12-way race.
    """
    by_team_pos = {}
    for t in teams:
        pos_map = {}
        for p in t["roster"]:
            if p.get("effective_per_week") is None:
                continue
            pos_map.setdefault(p["position"], []).append(p)
        for plist in pos_map.values():
            plist.sort(key=lambda p: p["effective_per_week"], reverse=True)
        by_team_pos[t["team_id"]] = pos_map

    # Which players are actually starting, by identity not by name -- two
    # players can share a name across teams, and a name-only check would mark
    # the wrong man as a starter.
    starting_ids = {}
    for tid, lineup in lineups.items():
        starting_ids[tid] = {id(s["player"]) for s in lineup if s["player"]}

    positions = sorted({pos for pm in by_team_pos.values() for pos in pm})
    max_depth = {pos: max((len(pm.get(pos, [])) for pm in by_team_pos.values()), default=0)
                 for pos in positions}

    out = {t["team_id"]: [] for t in teams}
    for pos in positions:
        if pos not in STRENGTH_POSITIONS:
            # K/DEF depth is not a thing worth ranking -- see STRENGTH_POSITIONS.
            continue
        for idx in range(max_depth[pos]):
            entries = []
            for t in teams:
                plist = by_team_pos[t["team_id"]].get(pos, [])
                p = plist[idx] if idx < len(plist) else None
                entries.append({"team_id": t["team_id"], "p": p,
                                "value": p["effective_per_week"] if p else 0.0})
            entries.sort(key=lambda e: e["value"], reverse=True)
            for i, e in enumerate(entries):
                e["rank"] = i + 1
            values = [e["value"] for e in entries]
            holding = sum(1 for e in entries if e["p"] is not None)
            n = len(entries)
            median = sorted(values)[n // 2] if n else 0.0
            for e in entries:
                if e["p"] is None:
                    continue
                out[e["team_id"]].append({
                    "slot": f"{pos}{idx+1}",
                    "position": pos,
                    "depthIndex": idx + 1,
                    "player": e["p"]["name"],
                    "perWeek": round(e["value"], 2),
                    "injury": e["p"].get("injury_status"),
                    "starting": id(e["p"]) in starting_ids.get(e["team_id"], set()),
                    "rank": e["rank"],
                    "of": n,
                    "holding": holding,
                    "leagueMin": round(min(values), 2),
                    "leagueMax": round(max(values), 2),
                    "leagueMedian": round(median, 2),
                })
    return out


def build_league(key, raw, roster_slots, ros_index):
    if not roster_slots:
        print(f"  ::warning::{key}: no rosterSlots found in bigboard.json or "
              f"league_rules.json -- positional strength will be empty for this "
              f"league. Run Update Big Board Data first.")

    teams = []
    for t in raw.get("teams", []):
        team = dict(t)
        team["roster"] = [enrich(p, ros_index) for p in t.get("roster", [])]
        teams.append(team)

    free_agents = [enrich(p, ros_index) for p in raw.get("free_agents", [])]
    # Unprojected free agents are noise -- the deep tail nobody expects to
    # play. Dropped so the waiver engine doesn't have to re-filter.
    #
    # BUT: only when the drop is selective. If NOTHING has a projection, the
    # cause is a broken projection join, not a worthless free-agent pool, and
    # silently emitting an empty list turns a data failure into what looks
    # like a quiet league. Keep them and say so instead.
    valued = [p for p in free_agents if p.get("ros_per_week") is not None]
    if valued or not free_agents:
        free_agents = valued
    else:
        print(f"  ::warning::{key}: all {len(free_agents)} free agents are unprojected. "
              f"Keeping them unvalued -- this is a projection-join failure, not an "
              f"empty waiver wire. Check player_ros_{YEAR}.json before trusting "
              f"anything downstream.")
    free_agents.sort(key=lambda p: p.get("effective_per_week") or 0, reverse=True)

    strength, lineups = ({}, {})
    if roster_slots and teams:
        strength, lineups = positional_strength(teams, roster_slots)

    depth = depth_chart(teams, lineups) if (roster_slots and teams) else {}

    for t in teams:
        t["positionStrength"] = strength.get(t["team_id"], [])
        t["depthChart"] = depth.get(t["team_id"], [])
        t["startingLineup"] = [
            {"slot": s["slot_label"],
             "player": s["player"]["name"] if s["player"] else None,
             "perWeek": s["player"]["effective_per_week"] if s["player"] else None}
            for s in lineups.get(t["team_id"], [])
        ]
        t["rosPerWeekTotal"] = round(
            sum(s["player"]["effective_per_week"]
                for s in lineups.get(t["team_id"], []) if s["player"]), 2)

    matched = sum(1 for t in teams for p in t["roster"] if p.get("ros_matched"))
    total = sum(len(t["roster"]) for t in teams)
    if total:
        pct = 100.0 * matched / total
        print(f"  {key}: {len(teams)} teams, {total} rostered, "
              f"{matched} matched to a projection ({pct:.0f}%), "
              f"{len(free_agents)} usable free agents.")
        if pct < 85:
            # A low match rate quietly poisons everything downstream: an
            # unmatched player reads as worth zero, which makes a real starter
            # look like a hole to fix. Loud on purpose.
            print(f"  ::warning::{key}: only {pct:.0f}% of rostered players matched a "
                  f"projection. Unmatched players count as zero, so positional "
                  f"strength and every recommendation will be wrong until this is "
                  f"fixed. Check name formats (suffixes, defenses) first.")
            unmatched = [p["name"] for t in teams for p in t["roster"]
                         if not p.get("ros_matched")][:15]
            print(f"  Sample unmatched: {', '.join(unmatched)}")

    return {
        "platform": raw.get("platform"),
        "currentWeek": raw.get("current_week"),
        "rosterSlots": roster_slots,
        "teams": teams,
        "freeAgents": free_agents,
    }


def main():
    print(f"Building inseason_{YEAR}.json...")
    yahoo = load_json(f"yahoo_inseason_{YEAR}.json", "Yahoo state")
    espn = load_json(f"espn_inseason_{YEAR}.json", "ESPN state")
    ros = load_json(f"player_ros_{YEAR}.json", "ROS projections")
    bigboard = load_json("bigboard.json", "Big Board") or {}
    rules = load_json("league_rules.json", "League rules") or {}

    if ros is None:
        print("::warning::No ROS projection file. Every player will be unvalued and "
              "the Manager Cockpit will have nothing to recommend. Run the "
              "ESPN ROS Projections step first.")
    ros_index = build_ros_index(ros)
    print(f"Projection index: {len(ros_index)} unique names.")

    leagues = {}
    for doc in (yahoo, espn):
        for key, raw in ((doc or {}).get("leagues") or {}).items():
            leagues[key] = build_league(
                key, raw, roster_slots_for(key, bigboard, rules), ros_index)

    if not leagues:
        raise SystemExit(
            "No league state to build from. Both yahoo_inseason_*.json and "
            "espn_inseason_*.json are missing or empty -- check that the pull "
            "steps above actually succeeded rather than failing quietly."
        )

    current_week = (ros or {}).get("current_week") or next(
        (lg.get("currentWeek") for lg in leagues.values() if lg.get("currentWeek")), None)

    out = {
        "year": YEAR,
        "generated": datetime.now(timezone.utc).isoformat(),
        "currentWeek": current_week,
        "weeksRemaining": (ros or {}).get("weeks_remaining"),
        "leagues": leagues,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE}: {len(leagues)} league(s), week {current_week}.")


if __name__ == "__main__":
    main()
