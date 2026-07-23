"""
roster_optimizer.py
Answers: "what team maximizes total roster WAR under a $200 auction budget,
assuming players cost ~ESPN's recommended bid?"

FRAMING (per decisions made with Sean):
  - Optimizes WAR summed across the full skill-position roster (bench counts
    too -- e.g. for injury insurance), not just the starters.
  - Player cost is assumed to equal ESPN's recommended auction bid
    (espnRecommendedBid) -- i.e. "what if the market priced everyone exactly
    at ESPN's crowd-sourced value and I won every player at that price."
    This is a WHAT-IF planning exercise, not a live bid strategy -- real
    auctions have competition and variance around these prices, and this
    optimizer doesn't know what anyone else has drafted.
  - K and DEF are EXCLUDED from the optimizer entirely (not modeled at all).
    Same reasoning as bid_calibration.py's SKILL_POSITIONS exclusion: those
    positions are always near-min-bid picks with no meaningful WAR signal to
    optimize against, so asking an ILP to "maximize WAR" for a slot where
    there's no real decision to make isn't just unnecessary, it's actively
    the wrong model -- and in practice, real-world K/DEF auction data is often
    sparse/null early in the offseason (zero priced kickers = a mandatory
    slot with literally nothing eligible = guaranteed Infeasible). Real K/DEF
    spend is instead assumed as a flat KDEF_RESERVE carved out of the budget
    up front, so the skill-position optimization reflects the money actually
    available for it.

ROSTER TEMPLATE (skill positions only): QB, RB, RB, WR, WR, WR, TE,
FLEX(RB/WR/TE), BEN x6 (any skill position) = 14 slots. The REAL Zimmer roster
also needs 1 DEF + 1 K (see ASSUMED_ROSTER in index.html for the full 16-slot
live-draft template) -- those aren't modeled here, just budgeted for via
KDEF_RESERVE.

METHOD: exact 0/1 assignment integer program (player x slot), solved with the
CBC solver via PuLP:
    maximize   sum_(p,s) war[p] * y[p,s]
    subject to sum_s y[p,s] <= 1                       (each player used once)
               sum_(p eligible for s) y[p,s] == 1       (each slot filled)
               sum_(p,s) cost[p] * y[p,s] <= (BUDGET - KDEF_RESERVE)
This is EXACT, not greedy -- it finds the true WAR-maximizing roster for the
given prices/budget, not just a reasonable-looking one. (A naive greedy
"take highest WAR/$ each round" can miss the true optimum because of the
budget and multi-slot interaction; ILP doesn't have that blind spot.)

REQUIRES: bigboard.json with projectedWar + espnRecommendedBid populated
(i.e. after running Update Big Board + Update Player Values workflows).

USAGE: python3 roster_optimizer.py [bigboard.json]
"""
import json
import sys
import pulp

BUDGET = 200
# Reserved for the 1 DEF + 1 K you'll still draft in the real league, at the
# typical min-bid each ($1). Not optimized -- just carved out so the skill-
# position budget below reflects money actually available for it. Tune if
# your league's min bid differs.
KDEF_RESERVE_PER_SLOT = 1
KDEF_SLOTS = 2
KDEF_RESERVE = KDEF_RESERVE_PER_SLOT * KDEF_SLOTS

ROSTER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX",
          "BEN", "BEN", "BEN", "BEN", "BEN", "BEN"]
ELIGIBLE = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"},
    "FLEX": {"RB", "WR", "TE"},
    "BEN": {"QB", "RB", "WR", "TE"},
}
VALID_POS = {"QB", "RB", "WR", "TE"}  # K/DEF excluded entirely -- see docstring


def load_players(path):
    data = json.load(open(path))
    players = []
    for p in data["players"]:
        cost = p.get("espnRecommendedBid")
        war = p.get("projectedWar")
        if cost is None or war is None or p.get("pos") not in VALID_POS:
            continue
        players.append({
            "id": p["id"], "name": p["name"], "pos": p["pos"], "team": p.get("team"),
            "cost": max(1, round(cost)), "war": war,
        })
    return players


def optimize(players, roster=ROSTER, budget=BUDGET):
    slots = list(enumerate(roster))  # unique index per slot, e.g. RB gets two separate slot indices

    prob = pulp.LpProblem("roster_war_max", pulp.LpMaximize)

    # y[(player_idx, slot_idx)] created only for LEGAL (player, slot) pairs --
    # keeps the problem small (legal combinations, not the full player x slot grid)
    y = {}
    for pi, p in enumerate(players):
        for si, slot_type in slots:
            if p["pos"] in ELIGIBLE[slot_type]:
                y[(pi, si)] = pulp.LpVariable(f"y_{pi}_{si}", cat="Binary")

    # objective: total roster WAR
    prob += pulp.lpSum(players[pi]["war"] * var for (pi, si), var in y.items())

    # each player used at most once
    for pi in range(len(players)):
        vars_for_p = [var for (pi2, si), var in y.items() if pi2 == pi]
        if vars_for_p:
            prob += pulp.lpSum(vars_for_p) <= 1

    # each slot filled by exactly one eligible player
    for si, slot_type in slots:
        vars_for_s = [var for (pi, si2), var in y.items() if si2 == si]
        prob += pulp.lpSum(vars_for_s) == 1

    # budget
    prob += pulp.lpSum(players[pi]["cost"] * var for (pi, si), var in y.items()) <= budget

    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if pulp.LpStatus[prob.status] != "Optimal":
        return None, pulp.LpStatus[prob.status]

    result = [None] * len(roster)
    for (pi, si), var in y.items():
        if var.value() == 1:
            result[si] = players[pi]
    return result, "Optimal"


def min_feasible_cost(players, roster=ROSTER):
    """Same slot-assignment structure as optimize(), but MINIMIZES total cost
    with NO budget cap -- i.e. 'what's the cheapest possible legal roster,
    ignoring budget entirely?' Used only to diagnose an Infeasible result from
    optimize(): if this is itself infeasible, some slot type has zero eligible
    players (checked directly below, cheaper than solving); if it solves, the
    resulting cost tells us whether $BUDGET was simply too tight."""
    slots = list(enumerate(roster))
    prob = pulp.LpProblem("roster_min_cost", pulp.LpMinimize)
    y = {}
    for pi, p in enumerate(players):
        for si, slot_type in slots:
            if p["pos"] in ELIGIBLE[slot_type]:
                y[(pi, si)] = pulp.LpVariable(f"z_{pi}_{si}", cat="Binary")
    prob += pulp.lpSum(players[pi]["cost"] * var for (pi, si), var in y.items())
    for pi in range(len(players)):
        vars_for_p = [var for (pi2, si), var in y.items() if pi2 == pi]
        if vars_for_p:
            prob += pulp.lpSum(vars_for_p) <= 1
    for si, slot_type in slots:
        vars_for_s = [var for (pi, si2), var in y.items() if si2 == si]
        prob += pulp.lpSum(vars_for_s) == 1
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None, pulp.LpStatus[prob.status]
    return round(pulp.value(prob.objective)), "Optimal"


def diagnose_infeasibility(players, roster=ROSTER, budget=BUDGET):
    """Explains WHY optimize() came back Infeasible, rather than leaving a
    bare solver status. Checks the cheap, direct cause first (a required slot
    type has zero eligible priced players -- e.g. ESPN hasn't published
    meaningful K/DEF auction values yet this early in the offseason, a common
    real case), then falls back to an exact minimum-cost solve to see if the
    budget itself was simply too tight."""
    lines = []
    # direct check: does every slot TYPE have at least one eligible player?
    slot_types = set(roster)
    starved = []
    for slot_type in sorted(slot_types):
        eligible_pos = ELIGIBLE[slot_type]
        n = sum(1 for p in players if p["pos"] in eligible_pos)
        if n == 0:
            starved.append(slot_type)
    if starved:
        lines.append(
            f"BLOCKER: no priced players are eligible for slot type(s) {starved}. "
            f"This usually means ESPN hasn't published meaningful auction values/"
            f"projections for that position yet (common for K/DEF early in the "
            f"offseason) -- those players get filtered out by load_players() "
            f"because espnRecommendedBid or projectedWar is null for all of them."
        )
        return lines

    # every slot type has SOME eligible players -- check whether $budget is
    # simply too tight for even the cheapest legal roster
    min_cost, status = min_feasible_cost(players, roster)
    if status != "Optimal":
        lines.append(
            f"Could not find ANY legal roster even ignoring budget (solver status: {status}). "
            f"This points to a slot-combination issue (e.g. not enough total players across "
            f"FLEX-eligible or BEN-eligible positions to cover every slot simultaneously)."
        )
    elif min_cost > budget:
        lines.append(
            f"BLOCKER: the cheapest possible legal 16-man roster costs ${min_cost}, "
            f"which is more than your ${budget} budget. Budget is too tight for the "
            f"current player pool/prices -- not a bug, just an infeasible ask as configured."
        )
    else:
        lines.append(
            f"Cheapest legal roster costs ${min_cost} (within budget ${budget}), so budget "
            f"isn't the blocker -- the Infeasible result may be a solver quirk. Try re-running; "
            f"if it persists, share this diagnostic output."
        )
    return lines


def report(roster_slots, roster_template, skill_budget):
    total_war = sum(p["war"] for p in roster_slots if p)
    total_cost = sum(p["cost"] for p in roster_slots if p)
    print(f"\n{'SLOT':6} {'PLAYER':22} {'POS':4} {'TEAM':5} {'COST':>6} {'WAR':>8}")
    print("-" * 55)
    for slot_type, p in zip(roster_template, roster_slots):
        if not p:
            print(f"{slot_type:6} (empty -- infeasible)")
            continue
        print(f"{slot_type:6} {p['name']:22} {p['pos']:4} {(p['team'] or ''):5} "
              f"${p['cost']:>5} {p['war']:>+8.1f}")
    print("-" * 55)
    print(f"{'TOTAL':6} {'':22} {'':4} {'':5} ${total_cost:>5} {total_war:>+8.1f}")
    print(f"\nSkill-position budget remaining: ${skill_budget - total_cost} "
          f"(of ${skill_budget} skill budget)")
    print(f"Plus ${KDEF_RESERVE} reserved for K + DEF (not modeled) -- "
          f"total budget ${BUDGET} = ${skill_budget} skill + ${KDEF_RESERVE} K/DEF.")


def to_json(roster_slots, roster_template, skill_budget):
    rows = []
    for slot_type, p in zip(roster_template, roster_slots):
        rows.append({
            "slot": slot_type,
            "id": p["id"] if p else None, "name": p["name"] if p else None,
            "pos": p["pos"] if p else None, "team": p["team"] if p else None,
            "cost": p["cost"] if p else None, "war": p["war"] if p else None,
        })
    total_war = sum(p["war"] for p in roster_slots if p)
    total_cost = sum(p["cost"] for p in roster_slots if p)
    return {
        "generated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "budget": BUDGET,
        "skill_budget": skill_budget,
        "kdef_reserve": KDEF_RESERVE,
        "roster": rows,
        "total_war": round(total_war, 1),
        "total_cost": total_cost,
        "budget_remaining": skill_budget - total_cost,
        "assumptions": [
            "Cost assumed to equal ESPN's recommended auction bid (espnRecommendedBid) -- "
            "a what-if if you won every player at exactly that price, not a live bid strategy.",
            "Optimizes total skill-position roster WAR (bench included), found via exact "
            "integer programming (CBC), not a greedy heuristic.",
            f"K and DEF are excluded entirely (no meaningful WAR signal to optimize -- same "
            f"reasoning as bid_calibration.py's skill-position-only calibration). Real K/DEF "
            f"spend is assumed as a flat ${KDEF_RESERVE} reserve (${KDEF_RESERVE_PER_SLOT} x "
            f"{KDEF_SLOTS} slots) carved out of the ${BUDGET} total, leaving ${skill_budget} "
            f"for this optimization.",
        ],
    }


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "bigboard.json"
    out_json = sys.argv[2] if len(sys.argv) > 2 else "roster_optimizer_result.json"
    skill_budget = BUDGET - KDEF_RESERVE
    players = load_players(src)
    print(f"Loaded {len(players)} skill-position (QB/RB/WR/TE) players with both WAR and an "
          f"ESPN bid from {src}. (K/DEF excluded entirely -- see script docstring.)")
    print(f"Optimizing against ${skill_budget} (${BUDGET} total minus ${KDEF_RESERVE} reserved for K/DEF).")
    if len(players) < len(ROSTER):
        print("Not enough priced players to fill a roster -- run Update Player Values first.")
        sys.exit(1)
    roster_slots, status = optimize(players, budget=skill_budget)
    if status != "Optimal":
        print(f"Solver status: {status}.")
        print("Diagnosing why...")
        for line in diagnose_infeasibility(players, budget=skill_budget):
            print(f"  {line}")
        sys.exit(1)
    report(roster_slots, ROSTER, skill_budget)
    with open(out_json, "w") as f:
        json.dump(to_json(roster_slots, ROSTER, skill_budget), f, indent=2)
    print(f"\nWrote {out_json} for the front end.")
