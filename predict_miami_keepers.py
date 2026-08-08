"""
predict_miami_keepers.py
Predicts each Miami Domers owner's 2 most likely keepers for the upcoming
season, plus grades every eligible option, using the SAME Keeper Value formula
the site uses for Kepners:

    Keeper Value = Surplus x (WAR / 100)
    Surplus      = player WAR - average WAR of same-position players near the
                   same keeper-cost round

Miami-specific differences from the Kepners version:

  * WAR is the LEAGUE-SPECIFIC value (bigboard.json -> warByLeague.miami), not
    the 12-team baseline. Miami is an 8-team league, so replacement level is
    far shallower (better players sit on waivers) and the same player is worth
    meaningfully less above replacement than in Kepners. Using the 12-team
    number here would systematically overstate every Miami keeper.

  * Keeper cost is drafted round - 3 (Kepners is -2).

  * NEW 2026 UDFA RULE: a team may keep up to one undrafted free agent /
    waiver pickup at a 13th-round cost. Those players have no draft round, so
    they're costed at round 13 and flagged udfa=true. The rule is encoded as
    one OF the two keeper slots (not a bonus third) -- see league_rules.json.

INPUT:  miami_roster_snapshot_<season>.json  (eligible players per team)
        bigboard.json                        (WAR + consensus rank)
OUTPUT: miami_suggested_keepers.json         (consumed by index.html)

Confirmed keepers listed in the snapshot's "confirmed_keepers" block are passed
through as confirmed=true so the site can show them green rather than as amber
predictions.

USAGE:  python predict_miami_keepers.py miami_roster_snapshot_2025.json
"""
import json
import sys

TEAMS = 8
LEAGUE = "miami"
COST_ROUNDS_ADVANCE = 3
UDFA_COST_ROUND = 13
KEEPER_VALUE_WAR_CONST = 100
PEER_WINDOW_START, PEER_WINDOW_MAX, MIN_PEER_SAMPLE = 0, 6, 5


def rank_to_round(rank, teams=TEAMS):
    if not isinstance(rank, (int, float)):
        return None
    return int((rank - 1) // teams) + 1


def normalize(name):
    # Reuse the project's canonical matcher -- it strips Jr/Sr/II/III suffixes
    # and punctuation, which a naive alphanumeric-only normalize does not
    # ("Kenneth Walker III" vs the Big Board's "Kenneth Walker").
    try:
        from convert_bigboard import normalize_name
        return normalize_name(name)
    except Exception:
        return "".join(ch for ch in str(name).lower() if ch.isalnum())


def build_index(players):
    idx = {}
    for p in players:
        idx[normalize(p["name"])] = p
    return idx


def find_player(idx, name):
    p = idx.get(normalize(name))
    if p:
        return p
    # Team defenses: snapshot says "Texans", Big Board says "Houston Texans".
    for key, cand in idx.items():
        if cand.get("pos") == "DEF" and key.endswith(normalize(name)):
            return cand
    return None


def war_for(p):
    """League-specific WAR, falling back to the flat baseline if a per-league
    value isn't present (i.e. bigboard.json predates the per-league change)."""
    by_league = p.get("warByLeague") or {}
    return by_league.get(LEAGUE, p.get("projectedWar"))


def build_peer_pool(players):
    pool = {}
    for p in players:
        w = war_for(p)
        r = rank_to_round(p.get("avgRank"))
        if w is None or r is None:
            continue
        pool.setdefault(p["pos"], {}).setdefault(r, []).append(w)
    return pool


def peer_avg_war(pool, pos, round_):
    by_round = pool.get(pos, {})
    for w in range(PEER_WINDOW_START, PEER_WINDOW_MAX + 1):
        wars = [v for r, vals in by_round.items() if abs(r - round_) <= w for v in vals]
        if len(wars) >= MIN_PEER_SAMPLE:
            return sum(wars) / len(wars), len(wars), w
    return None, 0, None


DEFAULT_TIERS = {"elitePlus": 160, "elite": 70, "great": 20, "good": 4, "fairFloor": -6}


def verdict_for(kv, tiers=None):
    """Tier labels come from the league's own derived cutoffs (bigboard.json ->
    leagues.miami.keeperValueTiers), not fixed numbers. Keeper Value scales with
    league depth, so 12-team thresholds would make an 8-team league read 'fair'
    for essentially every option."""
    t = tiers or DEFAULT_TIERS
    if kv is None:
        return "no data"
    if kv >= t["elitePlus"]:
        return "elite value+"
    if kv >= t["elite"]:
        return "elite value"
    if kv >= t["great"]:
        return "great value"
    if kv >= t["good"]:
        return "good value"
    if kv > t["fairFloor"]:
        return "fair"
    return "reach"


def grade_option(entry, idx, pool, tiers=None):
    bp = find_player(idx, entry["player"])
    is_udfa = bool(entry.get("udfa"))
    if is_udfa:
        cost_round = UDFA_COST_ROUND
    else:
        dr = entry.get("draft_round")
        if dr is None:
            return None
        cost_round = max(1, dr - COST_ROUNDS_ADVANCE)

    war = war_for(bp) if bp else None
    peer, n, w = peer_avg_war(pool, bp["pos"], cost_round) if bp else (None, 0, None)
    surplus = round(war - peer, 1) if (war is not None and peer is not None) else None

    # VALUE FLOOR -- mirrors the site's grading. Keeper Value multiplies surplus
    # by WAR, so a NEGATIVE WAR flips the sign: a scrub projected far below
    # replacement, measured against an even worse peer group, produces a large
    # positive score and outranks genuine starters. A player at or below
    # replacement has no keeper value at any cost, so don't score them.
    if war is not None and war <= 0:
        kv, verdict = None, "no value (at/below replacement)"
    elif surplus is not None:
        kv = round(surplus * (war / KEEPER_VALUE_WAR_CONST), 1)
        verdict = verdict_for(kv, tiers)
    else:
        kv, verdict = None, "no data"

    return {
        "player": bp["name"] if bp else entry["player"],
        "pos": bp["pos"] if bp else None,
        "matched": bool(bp),
        "udfa": is_udfa,
        "drafted_round": entry.get("draft_round"),
        "round": cost_round,
        "war": war,
        "peer_avg_war": round(peer, 1) if peer is not None else None,
        "peer_n": n,
        "peer_window": w,
        "surplus": surplus,
        "keeper_value": kv,
        "verdict": verdict,
        "was_keeper_2025": bool(entry.get("was_keeper_2025")),
    }


def pick_best_two(options):
    """Top 2 by Keeper Value, honouring the max-1-UDFA rule."""
    ranked = sorted(
        [o for o in options if o["keeper_value"] is not None],
        key=lambda o: -o["keeper_value"],
    )
    chosen, udfa_used = [], False
    for o in ranked:
        if o["udfa"] and udfa_used:
            continue  # only one UDFA keeper allowed
        chosen.append(o)
        udfa_used = udfa_used or o["udfa"]
        if len(chosen) == 2:
            break
    return chosen


def main(snapshot_path, bigboard_path="bigboard.json",
         out_path="miami_suggested_keepers.json"):
    with open(snapshot_path) as f:
        snap = json.load(f)
    with open(bigboard_path) as f:
        bb = json.load(f)

    players = bb["players"]
    idx = build_index(players)
    pool = build_peer_pool(players)
    tiers = (bb.get("leagues", {}).get(LEAGUE, {}) or {}).get("keeperValueTiers")

    confirmed = snap.get("confirmed_keepers", {})
    by_manager, all_options, unmatched = {}, {}, []

    for team, entries in snap["rosters"].items():
        graded = []
        for e in entries:
            g = grade_option(e, idx, pool, tiers)
            if g is None:
                continue
            if not g["matched"]:
                unmatched.append(f"{team}: {g['player']}")
            graded.append(g)
        graded.sort(key=lambda o: -(o["keeper_value"] if o["keeper_value"] is not None else -999))
        all_options[team] = graded

        if team in confirmed:
            # Real, locked-in choices win over any prediction.
            by_manager[team] = [
                {**(next((g for g in graded if normalize(g["player"]) == normalize(c["player"])), {})),
                 "player": c["player"], "round": c["round"], "confirmed": True}
                for c in confirmed[team]
            ]
        else:
            by_manager[team] = [{**o, "confirmed": False} for o in pick_best_two(graded)]

    out = {
        "generated": snap.get("season"),
        "source_season": snap.get("season"),
        "target_season": (snap.get("season") or 0) + 1,
        "league": LEAGUE,
        "methodology": (
            "Keeper Value = Surplus x (WAR / 100), where WAR is Miami-specific "
            "(8-team replacement level, not the 12-team baseline) and Surplus is "
            "WAR minus the average WAR of same-position players near the same "
            "keeper-cost round. Cost = drafted round - 3; UDFAs cost a 13th and "
            "are limited to one per team."
        ),
        "source_note": snap.get("source", "unknown"),
        "tiers": tiers,
        "unmatched_players": sorted(set(unmatched)),
        "by_manager": by_manager,
        "all_options": all_options,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {out_path}: {len(by_manager)} teams, {len(set(unmatched))} unmatched name(s)")
    for team, picks in by_manager.items():
        tag = "CONFIRMED" if picks and picks[0].get("confirmed") else "predicted"
        desc = ", ".join(
            f"{p['player']} (R{p['round']}{', UDFA' if p.get('udfa') else ''}"
            f"{', KV ' + str(p['keeper_value']) if p.get('keeper_value') is not None else ''})"
            for p in picks
        )
        print(f"  {team:10s} [{tag}] {desc}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python predict_miami_keepers.py <snapshot.json> [bigboard.json] [out.json]")
        sys.exit(1)
    main(*sys.argv[1:4])
