"""
zimmer_analysis.py
Reads zimmer_draft_history.json and produces zimmer_analysis.json -- the compact
summary the site's "Zimmer History" view renders.

CONFIG YOU MAY NEED TO EDIT AS THE LEAGUE CHANGES:
  OWNER_ALIASES   -- merge display-name variants of the same real person
                     (e.g. ESPN showing "mmckenn5" some years, "DC Matt" others)
  EXCLUDED_OWNERS -- owners to drop from PER-OWNER profiling only (e.g. a
                     one-season member no longer in the league). Their picks
                     still count toward the league-wide tier bid spreads --
                     that's real market data -- they're just excluded from the
                     per-manager tendency/strategy section, which is about
                     people you'll actually face at the table.

ANALYSES PRODUCED:

1. TIER BID SPREADS (league-wide, all owners, all seasons)
   Within each season, players are ranked by winning bid within their position,
   then bucketed into simplified tiers (WR1, WR2, ...). "WR1" = the priciest
   WRs that year, so it means the same thing every season even as the actual
   players change. Pooled across all auction seasons: high/low/avg/spread.

2. PER-OWNER TENDENCIES (three independent axes, not one label)
   a. Spend concentration -- top-3-players' share of total budget. High =
      stars-and-scrubs; low = balanced.
   b. Positional lean -- each owner's share of budget on a position, compared
      to the league-wide average share on that position. Surfaces a real
      "priority position" and "punt position" per owner.
   c. Nomination behavior -- using the nominated_by field, what's the average
      *final* price of players THIS owner puts up for bid (regardless of who
      wins them)? High = they nominate premium names (drives bidding wars /
      tips their hand on targets); low = they float cheap fliers (feeling out
      the room / hoarding budget).
   These three combine into a short tag list per owner instead of a single
   bucket, since real strategies are usually a mix (e.g. "stars-and-scrubs at
   RB, but a quiet accumulator elsewhere").

3. STRATEGY NOTE per owner -- one concrete, tactical sentence for head-to-head
   bidding, generated from the actual numbers above (not hardcoded per person).

Run:  python zimmer_analysis.py
"""
import json
from collections import defaultdict
from statistics import mean, median

HISTORY_FILE = "zimmer_draft_history.json"
OUT_FILE = "zimmer_analysis.json"

# Merge display-name variants that refer to the same real person.
OWNER_ALIASES = {
    "mmckenn5": "DC Matt",
}
# Owners excluded from per-owner profiling (still counted in tier spreads).
EXCLUDED_OWNERS = {
    "Woody H. FTW",
}

# How many players per position make up each tier bucket, per season.
# Tuned to a 12-team league's rough starting-lineup demand.
TIER_SIZES = {
    "QB": [12, 12],                # QB1, QB2
    "RB": [12, 12, 12, 12],        # RB1..RB4
    "WR": [12, 12, 12, 12],        # WR1..WR4
    "TE": [12, 12],                # TE1, TE2
    "K":  [12],
    "DEF": [12], "D/ST": [12],
}
STUD_THRESHOLD = 40    # $ -- a marquee/anchor buy
BARGAIN_THRESHOLD = 5  # $ -- a value dart
LEAN_NOTABLE_PCT = 4.0 # percentage-point deviation from league avg to call out a position lean


def normalize_owner(name):
    return OWNER_ALIASES.get(name, name)


def tier_label(position, rank_within_pos):
    """rank_within_pos is 0-indexed (0 = most expensive at that position)."""
    sizes = TIER_SIZES.get(position, [12, 12, 12, 12, 12])
    cum = 0
    for i, size in enumerate(sizes):
        cum += size
        if rank_within_pos < cum:
            return f"{position}{i+1}"
    return f"{position}{len(sizes)+1}+"


def owner_key(pick, teams):
    """Map a pick to a stable, normalized owner identity."""
    tid = str(pick.get("team_id"))
    team = teams.get(tid) or teams.get(pick.get("team_id"))
    if team and team.get("owners"):
        return normalize_owner(team["owners"][0])
    return normalize_owner(pick.get("team_name") or "Unknown")


def build():
    with open(HISTORY_FILE) as f:
        history = json.load(f)

    tier_bids = defaultdict(list)
    tier_examples = defaultdict(list)

    owner_spend = defaultdict(list)
    owner_studs = defaultdict(int)
    owner_bargains = defaultdict(int)
    owner_pos_spend = defaultdict(lambda: defaultdict(list))
    owner_seasons = defaultdict(set)
    owner_nom_costs = defaultdict(list)  # cost of players THIS owner nominated (any winner)

    league_pos_spend = defaultdict(list)  # pos -> all winning bids league-wide (included owners only)
    league_all_costs = []

    for season, sdata in history.get("seasons", {}).items():
        if sdata.get("draft_type") != "auction":
            continue
        teams = sdata.get("teams", {})
        picks = [p for p in sdata.get("picks", []) if (p.get("cost") or 0) > 0]

        # -- tier spreads: every pick counts, regardless of owner exclusions --
        by_pos = defaultdict(list)
        for p in picks:
            pos = (p.get("position") or "").upper()
            if pos:
                by_pos[pos].append(p)
        for pos, plist in by_pos.items():
            plist.sort(key=lambda p: p.get("cost") or 0, reverse=True)
            for rank, p in enumerate(plist):
                tier = tier_label(pos, rank)
                cost = p["cost"]
                tier_bids[(pos, tier)].append(cost)
                tier_examples[(pos, tier)].append((cost, p.get("player"), season))

        # -- per-owner tendencies: excluded owners skipped entirely --
        for p in picks:
            ok = owner_key(p, teams)
            if ok in EXCLUDED_OWNERS:
                continue
            cost = p["cost"]
            pos = (p.get("position") or "").upper()

            owner_spend[ok].append(cost)
            owner_seasons[ok].add(season)
            if cost >= STUD_THRESHOLD:
                owner_studs[ok] += 1
            if cost <= BARGAIN_THRESHOLD:
                owner_bargains[ok] += 1
            if pos:
                owner_pos_spend[ok][pos].append(cost)
                league_pos_spend[pos].append(cost)
            league_all_costs.append(cost)

            nom = normalize_owner(p.get("nominated_by")) if p.get("nominated_by") else None
            if nom and nom not in EXCLUDED_OWNERS:
                owner_nom_costs[nom].append(cost)

    # ---- tier spreads output ----
    tiers_out = []
    for (pos, tier), bids in tier_bids.items():
        srt = sorted(tier_examples[(pos, tier)], key=lambda x: x[0])
        low_ex, high_ex = srt[0], srt[-1]
        tiers_out.append({
            "position": pos, "tier": tier, "n": len(bids),
            "high": max(bids), "low": min(bids),
            "avg": round(mean(bids), 1), "median": round(median(bids), 1),
            "spread": max(bids) - min(bids),
            "high_example": {"player": high_ex[1], "cost": high_ex[0], "season": high_ex[2]},
            "low_example": {"player": low_ex[1], "cost": low_ex[0], "season": low_ex[2]},
        })
    pos_order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5, "D/ST": 5}
    tiers_out.sort(key=lambda t: (pos_order.get(t["position"], 9), t["tier"]))

    # ---- league-wide baselines for positional lean ----
    league_total = sum(league_all_costs) or 1
    league_pos_share = {
        pos: 100 * sum(bids) / league_total for pos, bids in league_pos_spend.items()
    }
    league_avg_pick_cost = mean(league_all_costs) if league_all_costs else 0

    # ---- per-owner output ----
    owners_out = []
    for owner, spend in owner_spend.items():
        total = sum(spend)
        n_seasons = len(owner_seasons[owner])
        spend_sorted = sorted(spend, reverse=True)
        top3_share = round(100 * sum(spend_sorted[:3]) / total, 1) if total else 0

        pos_avg = {pos: round(mean(b), 1) for pos, b in owner_pos_spend[owner].items() if b}
        pos_share = {pos: 100 * sum(b) / total for pos, b in owner_pos_spend[owner].items() if total}
        # deviation from league-average share, per position
        pos_deviation = {
            pos: round(share - league_pos_share.get(pos, 0), 1)
            for pos, share in pos_share.items()
        }
        priority_pos = max(pos_deviation.items(), key=lambda kv: kv[1]) if pos_deviation else None
        punt_pos = min(pos_deviation.items(), key=lambda kv: kv[1]) if pos_deviation else None

        nom_costs = owner_nom_costs.get(owner, [])
        avg_nom_cost = round(mean(nom_costs), 1) if nom_costs else None
        nom_deviation = round((avg_nom_cost - league_avg_pick_cost), 1) if avg_nom_cost is not None else None

        concentration_style = (
            "stars" if top3_share >= 45 else "value" if top3_share <= 30 else "balanced"
        )
        nomination_style = (
            None if nom_deviation is None else
            "driver" if nom_deviation >= 5 else
            "quiet" if nom_deviation <= -5 else "neutral"
        )

        tags = [{"stars": "Stars & Scrubs", "balanced": "Balanced", "value": "Value Hunter"}[concentration_style]]
        if priority_pos and priority_pos[1] >= LEAN_NOTABLE_PCT:
            tags.append(f"{priority_pos[0]}-Priority")
        if punt_pos and punt_pos[1] <= -LEAN_NOTABLE_PCT:
            tags.append(f"Punts {punt_pos[0]}")
        if nomination_style == "driver":
            tags.append("Market Driver")
        elif nomination_style == "quiet":
            tags.append("Quiet Accumulator")

        owners_out.append({
            "owner": owner,
            "seasons": n_seasons,
            "avg_studs_per_draft": round(owner_studs[owner] / n_seasons, 1) if n_seasons else 0,
            "avg_bargains_per_draft": round(owner_bargains[owner] / n_seasons, 1) if n_seasons else 0,
            "top3_spend_share_pct": top3_share,
            "max_bid_ever": max(spend) if spend else 0,
            "pos_avg_spend": pos_avg,
            "priority_position": priority_pos[0] if priority_pos and priority_pos[1] >= LEAN_NOTABLE_PCT else None,
            "priority_position_deviation_pct": priority_pos[1] if priority_pos and priority_pos[1] >= LEAN_NOTABLE_PCT else None,
            "punt_position": punt_pos[0] if punt_pos and punt_pos[1] <= -LEAN_NOTABLE_PCT else None,
            "punt_position_deviation_pct": punt_pos[1] if punt_pos and punt_pos[1] <= -LEAN_NOTABLE_PCT else None,
            "avg_nomination_result_cost": avg_nom_cost,
            "nomination_style": nomination_style,
            "tags": tags,
        })
    owners_out.sort(key=lambda o: o["top3_spend_share_pct"], reverse=True)

    for o in owners_out:
        o["strategy_note"] = strategy_note(o, league_avg_pick_cost)

    headlines = derive_headlines(tiers_out, owners_out)

    out = {
        "league_id": history.get("league_id"),
        "seasons_analyzed": sorted(
            s for s, d in history.get("seasons", {}).items()
            if d.get("draft_type") == "auction"
        ),
        "tier_spreads": tiers_out,
        "owners": owners_out,
        "headlines": headlines,
        "config": {
            "tier_sizes": TIER_SIZES,
            "stud_threshold": STUD_THRESHOLD,
            "bargain_threshold": BARGAIN_THRESHOLD,
            "owner_aliases": OWNER_ALIASES,
            "excluded_owners": sorted(EXCLUDED_OWNERS),
        },
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE}: {len(tiers_out)} tier rows, {len(owners_out)} owners, "
          f"{len(out['seasons_analyzed'])} seasons.")


def strategy_note(o, league_avg_pick_cost):
    """One concrete, tactical sentence for head-to-head bidding against this owner."""
    parts = []
    style = "stars" if o["top3_spend_share_pct"] >= 45 else "value" if o["top3_spend_share_pct"] <= 30 else "balanced"

    if style == "stars":
        base = (f"Puts {o['top3_spend_share_pct']}% of budget into their top 3 players -- "
                f"let bidding wars run long on their early targets")
        if o["priority_position"]:
            base += f", especially at {o['priority_position']}"
        base += ", then attack them late once they're down to scraps"
        if o["punt_position"]:
            base += f" (they consistently go cheap at {o['punt_position']}, so contest that position early instead)"
        parts.append(base + ".")
    elif style == "value":
        base = (f"Rarely commits big money (top 3 players are only {o['top3_spend_share_pct']}% of budget) -- "
                f"they're unlikely to be your ceiling competition on premium names")
        if o["avg_bargains_per_draft"]:
            base += f", but they hit ~{o['avg_bargains_per_draft']} bargains a draft, so expect stiff competition on the $1-5 tier late"
        parts.append(base + ".")
    else:
        base = "Spends fairly evenly across the roster -- expect steady, competitive bids on most players rather than a clear window to exploit"
        if o["priority_position"]:
            base += f", though they lean {o['priority_position']}-heavy"
        parts.append(base + ".")

    if o["nomination_style"] == "driver" and o["avg_nomination_result_cost"]:
        parts.append(
            f"When they nominate a player, it tends to sell for ~${o['avg_nomination_result_cost']} "
            f"(vs. ~${round(league_avg_pick_cost,1)} league average) -- treat their nominations as real targets, not fodder."
        )
    elif o["nomination_style"] == "quiet" and o["avg_nomination_result_cost"]:
        parts.append(
            f"Their nominations tend to sell cheap (~${o['avg_nomination_result_cost']}) -- "
            f"likely floating fliers to drain the room's attention, not tipping their actual targets."
        )

    return " ".join(parts)


def derive_headlines(tiers, owners):
    h = []
    if tiers:
        widest = max(tiers, key=lambda t: t["spread"])
        h.append(
            f"Widest bid volatility: {widest['position']} {widest['tier']} ranged "
            f"${widest['low']}-${widest['high']} (spread ${widest['spread']}), meaning "
            f"there's real value to be had if you wait out the top of the market here."
        )
    for pos in ["RB", "WR", "QB", "TE"]:
        t1 = next((t for t in tiers if t["position"] == pos and t["tier"] == f"{pos}1"), None)
        if t1:
            h.append(
                f"{pos}1-tier players have averaged ${t1['avg']} "
                f"(low ${t1['low']}, high ${t1['high']}) -- budget anchor for elite {pos}."
            )
    if owners:
        top = owners[0]
        h.append(
            f"{top['owner']} runs the most top-heavy builds -- "
            f"{top['top3_spend_share_pct']}% of budget on their 3 priciest players on average."
        )
        balanced = min(owners, key=lambda o: o["top3_spend_share_pct"])
        h.append(
            f"{balanced['owner']} spreads spend the most evenly "
            f"({balanced['top3_spend_share_pct']}% on top 3) -- a balanced-roster drafter."
        )
        drivers = [o for o in owners if o["nomination_style"] == "driver"]
        if drivers:
            names = ", ".join(o["owner"] for o in drivers)
            h.append(f"Market drivers to watch when they nominate: {names}.")
    return h


if __name__ == "__main__":
    build()
