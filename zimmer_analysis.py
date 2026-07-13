"""
zimmer_analysis.py
Reads zimmer_draft_history.json and produces zimmer_analysis.json -- the compact
summary the site's "Zimmer History" view renders.

Two analyses, matching the two questions asked:

1. TIER BID SPREADS (across all owners, all seasons)
   Within each season, players are ranked by winning bid within their position,
   then bucketed into simplified tiers (WR1, WR2, ... RB1, RB2, ...). "WR1" =
   the most expensive WRs that year, so it means the same thing every season even
   as the actual players change. For each (position, tier) we report high bid,
   low bid, average, and the spread, pooled across all six drafts.

   Tier bucket sizes are chosen to reflect real auction roster construction
   (see TIER_SIZES) -- e.g. ~the top 12 WRs by spend are "WR1"s (about one per
   team), the next 12 "WR2"s, etc.

2. PER-OWNER TENDENCIES
   Aggregated by OWNER, not team name (team names change year to year). For each
   owner we compute spend concentration (how top-heavy their roster is), which
   positions they pay up for vs. get cheap, average number of "studs" (>$40)
   bought, and how many bargains (<$5) they hit. These describe an owner's
   structural strategy (stars-and-scrubs vs. balanced), which is the part of
   auction behavior that actually repeats year to year and is worth prepping for.

Run:  python zimmer_analysis.py
"""
import json
from collections import defaultdict
from statistics import mean, median

HISTORY_FILE = "zimmer_draft_history.json"
OUT_FILE = "zimmer_analysis.json"

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
STUD_THRESHOLD = 40   # $ -- a marquee/anchor buy
BARGAIN_THRESHOLD = 5 # $ -- a value dart


def tier_label(position, rank_within_pos):
    """rank_within_pos is 0-indexed (0 = most expensive at that position)."""
    sizes = TIER_SIZES.get(position, [12, 12, 12, 12, 12])
    cum = 0
    for i, size in enumerate(sizes):
        cum += size
        if rank_within_pos < cum:
            return f"{position}{i+1}"
    # Anything beyond the defined buckets falls into a final "depth" tier
    return f"{position}{len(sizes)+1}+"


def owner_key(pick, teams):
    """Map a pick to a stable owner identity. Falls back to team name if the
    draft data has no owner attached (older seasons sometimes don't)."""
    tid = str(pick.get("team_id"))
    team = teams.get(tid) or teams.get(pick.get("team_id"))
    if team and team.get("owners"):
        return team["owners"][0]  # primary owner
    return pick.get("team_name") or "Unknown"


def build():
    with open(HISTORY_FILE) as f:
        history = json.load(f)

    # --- 1. Tier bid spreads, pooled across seasons ---
    # tier_bids[(pos, tier)] = [list of winning bids across all seasons]
    tier_bids = defaultdict(list)
    # also keep example players for context (highest & lowest bid in each tier)
    tier_examples = defaultdict(list)

    # --- per-owner accumulators ---
    owner_spend = defaultdict(list)        # owner -> list of all bids
    owner_studs = defaultdict(int)         # owner -> count of >$40 buys
    owner_bargains = defaultdict(int)      # owner -> count of <$5 buys
    owner_pos_spend = defaultdict(lambda: defaultdict(list))  # owner -> pos -> bids
    owner_seasons = defaultdict(set)

    for season, sdata in history.get("seasons", {}).items():
        if sdata.get("draft_type") != "auction":
            continue
        teams = sdata.get("teams", {})
        picks = [p for p in sdata.get("picks", []) if (p.get("cost") or 0) > 0]

        # rank within position for this season
        by_pos = defaultdict(list)
        for p in picks:
            pos = (p.get("position") or "").upper()
            if not pos:
                continue
            by_pos[pos].append(p)
        for pos, plist in by_pos.items():
            plist.sort(key=lambda p: p.get("cost") or 0, reverse=True)
            for rank, p in enumerate(plist):
                tier = tier_label(pos, rank)
                cost = p["cost"]
                tier_bids[(pos, tier)].append(cost)
                tier_examples[(pos, tier)].append((cost, p.get("player"), season))

        # owner tendencies
        for p in picks:
            ok = owner_key(p, teams)
            cost = p["cost"]
            owner_spend[ok].append(cost)
            owner_seasons[ok].add(season)
            if cost >= STUD_THRESHOLD:
                owner_studs[ok] += 1
            if cost <= BARGAIN_THRESHOLD:
                owner_bargains[ok] += 1
            pos = (p.get("position") or "").upper()
            if pos:
                owner_pos_spend[ok][pos].append(cost)

    # assemble tier spread output, sorted position then tier
    tiers_out = []
    for (pos, tier), bids in tier_bids.items():
        srt = sorted(tier_examples[(pos, tier)], key=lambda x: x[0])
        low_ex = srt[0]
        high_ex = srt[-1]
        tiers_out.append({
            "position": pos,
            "tier": tier,
            "n": len(bids),
            "high": max(bids),
            "low": min(bids),
            "avg": round(mean(bids), 1),
            "median": round(median(bids), 1),
            "spread": max(bids) - min(bids),
            "high_example": {"player": high_ex[1], "cost": high_ex[0], "season": high_ex[2]},
            "low_example": {"player": low_ex[1], "cost": low_ex[0], "season": low_ex[2]},
        })
    pos_order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5, "D/ST": 5}
    tiers_out.sort(key=lambda t: (pos_order.get(t["position"], 9), t["tier"]))

    # assemble owner output
    owners_out = []
    for owner, spend in owner_spend.items():
        total = sum(spend)
        spend_sorted = sorted(spend, reverse=True)
        top3_share = round(100 * sum(spend_sorted[:3]) / total, 1) if total else 0
        n_seasons = len(owner_seasons[owner])
        # which positions this owner pays up for, on average
        pos_avg = {pos: round(mean(b), 1) for pos, b in owner_pos_spend[owner].items() if b}
        owners_out.append({
            "owner": owner,
            "seasons": n_seasons,
            "avg_studs_per_draft": round(owner_studs[owner] / n_seasons, 1) if n_seasons else 0,
            "avg_bargains_per_draft": round(owner_bargains[owner] / n_seasons, 1) if n_seasons else 0,
            "top3_spend_share_pct": top3_share,  # concentration: stars-and-scrubs vs balanced
            "max_bid_ever": max(spend) if spend else 0,
            "pos_avg_spend": pos_avg,
        })
    owners_out.sort(key=lambda o: o["top3_spend_share_pct"], reverse=True)

    # a few league-wide trend headlines, computed not hand-written
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
        },
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE}: {len(tiers_out)} tier rows, {len(owners_out)} owners, "
          f"{len(out['seasons_analyzed'])} seasons.")


def derive_headlines(tiers, owners):
    """Auto-generate a few observations from the numbers (no hardcoded claims)."""
    h = []
    # biggest bid volatility tier
    if tiers:
        widest = max(tiers, key=lambda t: t["spread"])
        h.append(
            f"Widest bid volatility: {widest['position']} {widest['tier']} ranged "
            f"${widest['low']}-${widest['high']} (spread ${widest['spread']}), meaning "
            f"there's real value to be had if you wait out the top of the market here."
        )
    # elite tier price floor -- what the top tier of each skill position reliably costs
    for pos in ["RB", "WR", "QB", "TE"]:
        t1 = next((t for t in tiers if t["position"] == pos and t["tier"] == f"{pos}1"), None)
        if t1:
            h.append(
                f"{pos}1-tier players have averaged ${t1['avg']} "
                f"(low ${t1['low']}, high ${t1['high']}) -- budget anchor for elite {pos}."
            )
    # most concentrated (stars-and-scrubs) owner
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
    return h


if __name__ == "__main__":
    build()
