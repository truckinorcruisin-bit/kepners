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
# Tuned to a 12-team league's rough starting-lineup demand. QB and TE are
# broken into 4-player sub-tiers (QB1-4, QB5-8, QB9-12) because in a 1-QB/1-TE
# league the spend range across the top 12 is too wide to be one meaningful tier.
TIER_SIZES = {
    "QB": [4, 4, 4],               # QB1-4, QB5-8, QB9-12
    "RB": [12, 12, 12, 12],        # RB1..RB4
    "WR": [12, 12, 12, 12],        # WR1..WR4
    "TE": [4, 4, 4],               # TE1-4, TE5-8, TE9-12
    "K":  [12],
    "DEF": [12], "D/ST": [12],
}
STUD_PERCENTILE = 0.07  # top ~7% of a season's bids count as "studs" (relative)
BARGAIN_THRESHOLD = 5   # $ -- a value dart
LEAN_NOTABLE_PCT = 4.0  # percentage-point deviation from league avg to call out a position lean


def normalize_owner(name):
    return OWNER_ALIASES.get(name, name)


def tier_label(position, rank_within_pos):
    """rank_within_pos is 0-indexed (0 = most expensive at that position).
    For small sub-tiers (size < 12) the label is a range like 'QB1-4'; for
    full-position tiers it's the compact form like 'RB1'."""
    sizes = TIER_SIZES.get(position, [12, 12, 12, 12, 12])
    start = 1
    for i, size in enumerate(sizes):
        end = start + size - 1
        if rank_within_pos <= end - 1:
            if size < 12:
                return f"{position}{start}-{end}"
            return f"{position}{i+1}"
        start = end + 1
    return f"{position}{start}+"


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

    # per-season captures for year-over-year trend analysis
    # season -> {pos -> [costs]}, and season -> {tier -> avg} etc.
    season_pos_spend = defaultdict(lambda: defaultdict(list))
    season_tier_avg = defaultdict(dict)   # season -> {(pos,tier): avg}
    season_total = defaultdict(float)

    auction_seasons = [
        s for s, d in history.get("seasons", {}).items()
        if d.get("draft_type") == "auction"
    ]

    for season, sdata in history.get("seasons", {}).items():
        if sdata.get("draft_type") != "auction":
            continue
        teams = sdata.get("teams", {})
        picks = [p for p in sdata.get("picks", []) if (p.get("cost") or 0) > 0]

        # A "stud" is defined RELATIVE to each season's own price distribution:
        # the top ~7% of winning bids that year (roughly the marquee anchors in a
        # 12-team, ~$200 auction). Avoids a hardcoded dollar figure that skews
        # everyone toward one label when the market runs cheap or hot.
        season_costs = sorted((p["cost"] for p in picks), reverse=True)
        stud_cut = season_costs[max(0, int(len(season_costs) * 0.07) - 1)] if season_costs else 9999

        # -- tier spreads: every pick counts, regardless of owner exclusions --
        by_pos = defaultdict(list)
        for p in picks:
            pos = (p.get("position") or "").upper()
            if pos:
                by_pos[pos].append(p)
        per_season_tier = defaultdict(list)
        for pos, plist in by_pos.items():
            plist.sort(key=lambda p: p.get("cost") or 0, reverse=True)
            for rank, p in enumerate(plist):
                tier = tier_label(pos, rank)
                cost = p["cost"]
                tier_bids[(pos, tier)].append(cost)
                tier_examples[(pos, tier)].append((cost, p.get("player"), season))
                per_season_tier[(pos, tier)].append(cost)
            season_pos_spend[season][pos].extend(pp["cost"] for pp in plist)
        for key, costs in per_season_tier.items():
            season_tier_avg[season][key] = round(mean(costs), 1)

        # -- per-owner tendencies: excluded owners skipped entirely --
        for p in picks:
            ok = owner_key(p, teams)
            if ok in EXCLUDED_OWNERS:
                continue
            cost = p["cost"]
            pos = (p.get("position") or "").upper()

            owner_spend[ok].append(cost)
            owner_seasons[ok].add(season)
            if cost >= stud_cut:
                owner_studs[ok] += 1
            if cost <= BARGAIN_THRESHOLD:
                owner_bargains[ok] += 1
            if pos:
                owner_pos_spend[ok][pos].append(cost)
                league_pos_spend[pos].append(cost)
                season_total[season] += cost
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
    def tier_start_num(label):
        # extract the first integer in the tier label ("QB5-8" -> 5, "RB2" -> 2)
        import re
        m = re.search(r"(\d+)", label)
        return int(m.group(1)) if m else 999
    tiers_out.sort(key=lambda t: (pos_order.get(t["position"], 9), tier_start_num(t["tier"])))

    # ---- league-wide baselines for positional lean ----
    league_total = sum(league_all_costs) or 1
    league_pos_share = {
        pos: 100 * sum(bids) / league_total for pos, bids in league_pos_spend.items()
    }
    league_avg_pick_cost = mean(league_all_costs) if league_all_costs else 0

    # ---- per-owner output ----
    # First pass: compute raw stats for every owner.
    raw = []
    for owner, spend in owner_spend.items():
        total = sum(spend)
        n_seasons = len(owner_seasons[owner])
        spend_sorted = sorted(spend, reverse=True)
        top3_share = round(100 * sum(spend_sorted[:3]) / total, 1) if total else 0

        pos_avg = {pos: round(mean(b), 1) for pos, b in owner_pos_spend[owner].items() if b}
        pos_share = {pos: 100 * sum(b) / total for pos, b in owner_pos_spend[owner].items() if total}
        pos_deviation = {
            pos: round(share - league_pos_share.get(pos, 0), 1)
            for pos, share in pos_share.items()
        }
        priority_pos = max(pos_deviation.items(), key=lambda kv: kv[1]) if pos_deviation else None
        punt_pos = min(pos_deviation.items(), key=lambda kv: kv[1]) if pos_deviation else None

        nom_costs = owner_nom_costs.get(owner, [])
        avg_nom_cost = round(mean(nom_costs), 1) if nom_costs else None
        nom_deviation = round((avg_nom_cost - league_avg_pick_cost), 1) if avg_nom_cost is not None else None

        raw.append({
            "owner": owner, "spend": spend, "total": total, "n_seasons": n_seasons,
            "top3_share": top3_share, "pos_avg": pos_avg, "pos_deviation": pos_deviation,
            "priority_pos": priority_pos, "punt_pos": punt_pos,
            "avg_nom_cost": avg_nom_cost, "nom_deviation": nom_deviation,
        })

    # Concentration style is RELATIVE: rank owners by top-3 share, split into
    # thirds. Top third = Stars & Scrubs, bottom third = Value Hunter, middle =
    # Balanced. This guarantees the labels actually separate the field instead
    # of collapsing everyone into one bucket against an arbitrary dollar line.
    by_conc = sorted(raw, key=lambda r: r["top3_share"], reverse=True)
    n = len(by_conc)
    third = max(1, round(n / 3))
    style_by_owner = {}
    for idx, r in enumerate(by_conc):
        if idx < third:
            style_by_owner[r["owner"]] = "stars"
        elif idx >= n - third:
            style_by_owner[r["owner"]] = "value"
        else:
            style_by_owner[r["owner"]] = "balanced"

    owners_out = []
    for r in raw:
        owner = r["owner"]
        priority_pos, punt_pos = r["priority_pos"], r["punt_pos"]
        nom_deviation = r["nom_deviation"]

        concentration_style = style_by_owner[owner]
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
            "seasons": r["n_seasons"],
            "concentration_style": concentration_style,
            "avg_studs_per_draft": round(owner_studs[owner] / r["n_seasons"], 1) if r["n_seasons"] else 0,
            "avg_bargains_per_draft": round(owner_bargains[owner] / r["n_seasons"], 1) if r["n_seasons"] else 0,
            "top3_spend_share_pct": r["top3_share"],
            "max_bid_ever": max(r["spend"]) if r["spend"] else 0,
            "pos_avg_spend": r["pos_avg"],
            "priority_position": priority_pos[0] if priority_pos and priority_pos[1] >= LEAN_NOTABLE_PCT else None,
            "priority_position_deviation_pct": priority_pos[1] if priority_pos and priority_pos[1] >= LEAN_NOTABLE_PCT else None,
            "punt_position": punt_pos[0] if punt_pos and punt_pos[1] <= -LEAN_NOTABLE_PCT else None,
            "punt_position_deviation_pct": punt_pos[1] if punt_pos and punt_pos[1] <= -LEAN_NOTABLE_PCT else None,
            "avg_nomination_result_cost": r["avg_nom_cost"],
            "nomination_style": nomination_style,
            "tags": tags,
        })
    owners_out.sort(key=lambda o: o["top3_spend_share_pct"], reverse=True)

    for o in owners_out:
        o["strategy_note"] = strategy_note(o, league_avg_pick_cost)

    # ---- enhancement 2: 2025-specific (latest season) league trends ----
    latest_trends = compute_latest_season_trends(
        auction_seasons, season_pos_spend, season_total, season_tier_avg
    )

    headlines = derive_headlines(tiers_out, owners_out)

    out = {
        "league_id": history.get("league_id"),
        "seasons_analyzed": sorted(
            s for s, d in history.get("seasons", {}).items()
            if d.get("draft_type") == "auction"
        ),
        "tier_spreads": tiers_out,
        "owners": owners_out,
        "latest_season_trends": latest_trends,
        "headlines": headlines,
        "config": {
            "tier_sizes": TIER_SIZES,
            "stud_definition": "top ~7% of winning bids per season (relative)",
            "concentration_tiers": "relative terciles across owners",
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
    style = o["concentration_style"]  # relative tercile: stars / balanced / value

    if style == "stars":
        base = (f"One of the most top-heavy spenders in the league "
                f"({o['top3_spend_share_pct']}% of budget on their top 3) -- "
                f"let bidding wars run long on their early targets")
        if o["priority_position"]:
            base += f", especially at {o['priority_position']}"
        base += ", then attack the middle rounds once they're down to scraps"
        if o["punt_position"]:
            base += f" (they routinely go cheap at {o['punt_position']}, so contest that position early instead)"
        parts.append(base + ".")
    elif style == "value":
        base = (f"Among the most spread-out spenders (only {o['top3_spend_share_pct']}% on their top 3) -- "
                f"unlikely to be your ceiling competition on marquee names")
        if o["avg_bargains_per_draft"]:
            base += f", but hits ~{o['avg_bargains_per_draft']} bargains a draft, so expect stiff competition on the $1-5 tier late"
        parts.append(base + ".")
    else:
        base = "A middle-of-the-pack, balanced spender -- expect steady competitive bids across the board rather than one clear window to exploit"
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


def compute_latest_season_trends(auction_seasons, season_pos_spend, season_total, season_tier_avg):
    """Enhancement 2: how did the most recent season differ from prior years?
    Compares the latest season's share-of-budget by position (and elite-tier
    prices) against the average of all earlier auction seasons."""
    if len(auction_seasons) < 2:
        return None
    seasons_sorted = sorted(auction_seasons)
    latest = seasons_sorted[-1]
    prior = seasons_sorted[:-1]

    def pos_share_for(season):
        tot = sum(sum(v) for v in season_pos_spend[season].values()) or 1
        return {pos: 100 * sum(v) / tot for pos, v in season_pos_spend[season].items()}

    latest_share = pos_share_for(latest)
    # average prior-year share per position
    prior_shares = defaultdict(list)
    for s in prior:
        for pos, share in pos_share_for(s).items():
            prior_shares[pos].append(share)
    prior_avg = {pos: mean(v) for pos, v in prior_shares.items()}

    pos_moves = []
    for pos in ["QB", "RB", "WR", "TE"]:
        if pos in latest_share and pos in prior_avg:
            delta = latest_share[pos] - prior_avg[pos]
            pos_moves.append({
                "position": pos,
                "latest_share_pct": round(latest_share[pos], 1),
                "prior_avg_share_pct": round(prior_avg[pos], 1),
                "delta_pct": round(delta, 1),
            })
    pos_moves.sort(key=lambda m: abs(m["delta_pct"]), reverse=True)

    # elite-tier price movement (top sub-tier of each position)
    elite_moves = []
    elite_tiers = {"QB": "QB1-4", "RB": "RB1", "WR": "WR1", "TE": "TE1-4"}
    for pos, tier in elite_tiers.items():
        latest_avg = season_tier_avg.get(latest, {}).get((pos, tier))
        prior_vals = [season_tier_avg[s][(pos, tier)] for s in prior if (pos, tier) in season_tier_avg.get(s, {})]
        if latest_avg is not None and prior_vals:
            pa = mean(prior_vals)
            elite_moves.append({
                "position": pos, "tier": tier,
                "latest_avg": round(latest_avg, 1),
                "prior_avg": round(pa, 1),
                "delta": round(latest_avg - pa, 1),
            })
    elite_moves.sort(key=lambda m: abs(m["delta"]), reverse=True)

    # narrative headlines for the latest season
    notes = []
    for m in pos_moves:
        if abs(m["delta_pct"]) >= 2:
            direction = "more" if m["delta_pct"] > 0 else "less"
            notes.append(
                f"{m['position']}s took {abs(m['delta_pct'])} pts {direction} of total budget in {latest} "
                f"({m['latest_share_pct']}% vs {m['prior_avg_share_pct']}% prior-year avg)."
            )
    for m in elite_moves:
        if abs(m["delta"]) >= 3:
            direction = "up" if m["delta"] > 0 else "down"
            notes.append(
                f"Elite {m['position']} ({m['tier']}) went {direction} ${abs(m['delta'])} in {latest} "
                f"(${m['latest_avg']} vs ${m['prior_avg']} prior-year avg)."
            )

    return {
        "season": latest,
        "prior_seasons": prior,
        "position_share_moves": pos_moves,
        "elite_tier_moves": elite_moves,
        "notes": notes,
    }


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
