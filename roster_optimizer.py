"""
roster_optimizer.py
Answers: "what team maximizes total 16-man roster WAR under a $200 auction
budget, assuming players cost ~ESPN's recommended bid?"

FRAMING (per decisions made with Sean):
  - Optimizes WAR summed across the FULL 16-man roster (bench counts too --
    e.g. for injury insurance), not just the 9 starters.
  - Player cost is assumed to equal ESPN's recommended auction bid
    (espnRecommendedBid) -- i.e. "what if the market priced everyone exactly
    at ESPN's crowd-sourced value and I won every player at that price."
    This is a WHAT-IF planning exercise, not a live bid strategy -- real
    auctions have competition and variance around these prices, and this
    optimizer doesn't know what anyone else has drafted.

ROSTER TEMPLATE (Zimmer): QB, RB, RB, WR, WR, WR, TE, FLEX(RB/WR/TE), DEF, K,
BEN x6 (any position). Matches ASSUMED_ROSTER in index.html -- keep in sync
if that ever changes.

METHOD: exact 0/1 assignment integer program (player x slot), solved with the
CBC solver via PuLP:
    maximize   sum_(p,s) war[p] * y[p,s]
    subject to sum_s y[p,s] <= 1                       (each player used once)
               sum_(p eligible for s) y[p,s] == 1       (each slot filled)
               sum_(p,s) cost[p] * y[p,s] <= BUDGET
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
ROSTER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF", "K",
          "BEN", "BEN", "BEN", "BEN", "BEN", "BEN"]
ELIGIBLE = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "DEF": {"DEF"}, "K": {"K"},
    "FLEX": {"RB", "WR", "TE"},
    "BEN": {"QB", "RB", "WR", "TE", "DEF", "K"},
}
VALID_POS = {"QB", "RB", "WR", "TE", "DEF", "K"}


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


def report(roster_slots, roster_template):
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
    print(f"\nBudget remaining: ${BUDGET - total_cost}")


def to_json(roster_slots, roster_template, budget):
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
        "budget": budget,
        "roster": rows,
        "total_war": round(total_war, 1),
        "total_cost": total_cost,
        "budget_remaining": budget - total_cost,
        "assumptions": [
            "Cost assumed to equal ESPN's recommended auction bid (espnRecommendedBid) -- "
            "a what-if if you won every player at exactly that price, not a live bid strategy.",
            "Optimizes total 16-man roster WAR (bench included), found via exact integer "
            "programming (CBC), not a greedy heuristic.",
        ],
    }


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "bigboard.json"
    out_json = sys.argv[2] if len(sys.argv) > 2 else "roster_optimizer_result.json"
    players = load_players(src)
    print(f"Loaded {len(players)} players with both WAR and an ESPN bid from {src}.")
    if len(players) < len(ROSTER):
        print("Not enough priced players to fill a roster -- run Update Player Values first.")
        sys.exit(1)
    roster_slots, status = optimize(players)
    if status != "Optimal":
        print(f"Solver status: {status} -- check budget/roster feasibility.")
        sys.exit(1)
    report(roster_slots, ROSTER)
    with open(out_json, "w") as f:
        json.dump(to_json(roster_slots, ROSTER, BUDGET), f, indent=2)
    print(f"\nWrote {out_json} for the front end.")
