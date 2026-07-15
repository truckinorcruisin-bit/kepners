"""
zimmer_draft_grades.py
Joins WHAT WAS PAID (zimmer_draft_history.json) against WHAT WAS ACTUALLY
SCORED (espn_season_stats_<year>.json / espn_weekly_stats_<year>.json) to grade
historical drafting effectiveness -- "who gets more points per dollar than the
market rate," not just "who spent the most on good players."

REQUIRES (run these first, for each year you want graded):
  espn_season_stats_<year>.json   -- via espn_season_stats.py / "Update Season Stats" workflow
  espn_weekly_stats_<year>.json   -- via espn_weekly_stats.py / "Update Weekly Stats" workflow
Years configured below: GRADED_YEARS = [2023, 2024, 2025].

THE JOIN KEY: ESPN player_id is the same identifier across draft history and
season/weekly stats (both come from ESPN's own player IDs), so this is an
exact ID join -- no fuzzy name matching, no ambiguity from suffixes/nicknames.

THE CORE METRIC -- efficiency ratio, not raw points:
Points-per-dollar (PPD) alone would just reward whoever spent the most on
proven stars. Instead, for every pick we compute:
    PPD = player's actual season points / cost paid
    baseline PPD = the league-wide average PPD for that POSITION, in that
                   SEASON (since position and year both shift the going rate --
                   e.g. elite RBs pay off differently than elite TEs, and the
                   whole market can run cheap or expensive year to year)
    efficiency_ratio = PPD / baseline PPD
A ratio > 1 means that pick beat the market rate for its position that year;
< 1 means it underperformed what the price implied. Averaging this across an
owner's picks grades DRAFTING SKILL independent of budget size.

VOLATILITY: each pick's stdev_points_per_game (from the weekly file) is
attached too, so "effective" can be split from "consistent" -- an owner might
grade well on efficiency but only by hitting high-variance boom/bust players.

OUTPUT: zimmer_draft_grades.json
{
  "years_graded": [2023, 2024, 2025],
  "owners": [ { owner, picks_graded, surplus_per_dollar (PRIMARY ranking metric,
                dollar-weighted), total_surplus_points, avg_efficiency_ratio
                (supplementary, see note in code), hit_rate_pct, avg_volatility,
                total_points_drafted, best_pick, worst_pick,
                concentration_style (merged in from zimmer_analysis.json if present) }, ... ],
  "position_market_efficiency": [ {position, season, baseline_ppd}, ... ],
  "style_correlation": [ {style, avg_surplus_per_dollar, n_owners}, ... ],
  "headlines": [ ... auto-generated strategy takeaways for 2026 ... ]
}
"""
import json
import os
from collections import defaultdict
from statistics import mean

from zimmer_analysis import OWNER_ALIASES, EXCLUDED_OWNERS, normalize_owner  # reuse, don't duplicate

HISTORY_FILE = "zimmer_draft_history.json"
ZANALYSIS_FILE = "zimmer_analysis.json"  # optional, for style-tag cross-reference
OUT_FILE = "zimmer_draft_grades.json"

GRADED_YEARS = [2023, 2024, 2025]


def load_stats_for_year(year):
    """Returns (season_points_by_id, weekly_info_by_id) or (None, None) if the
    required files aren't present yet for this year."""
    sfile = f"espn_season_stats_{year}.json"
    wfile = f"espn_weekly_stats_{year}.json"
    if not (os.path.exists(sfile) and os.path.exists(wfile)):
        print(f"  {year}: missing {sfile if not os.path.exists(sfile) else wfile} -- skipping this year.")
        return None, None

    with open(sfile) as f:
        sdata = json.load(f)
    season_points = {}
    for pos, plist in sdata["players_by_position"].items():
        for p in plist:
            season_points[p["player_id"]] = p["total_points"]

    with open(wfile) as f:
        wdata = json.load(f)
    weekly_info = {
        p["player_id"]: {
            "avg_points_per_game": p.get("avg_points_per_game"),
            "stdev_points_per_game": p.get("stdev_points_per_game"),
            "games_played": p.get("games_played"),
        }
        for p in wdata["players"]
    }
    return season_points, weekly_info


def owner_key(pick, teams):
    tid = str(pick.get("team_id"))
    team = teams.get(tid) or teams.get(pick.get("team_id"))
    if team and team.get("owners"):
        return normalize_owner(team["owners"][0])
    return normalize_owner(pick.get("team_name") or "Unknown")


def build():
    with open(HISTORY_FILE) as f:
        history = json.load(f)

    zanalysis = None
    if os.path.exists(ZANALYSIS_FILE):
        with open(ZANALYSIS_FILE) as f:
            zanalysis = json.load(f)

    graded_picks = []  # every pick we could successfully grade, across all years
    unmatched = 0

    for year in GRADED_YEARS:
        season_points, weekly_info = load_stats_for_year(year)
        if season_points is None:
            continue
        sdata = history["seasons"].get(str(year))
        if not sdata or sdata.get("draft_type") != "auction":
            print(f"  {year}: not an auction season in draft history (or missing) -- skipping.")
            continue
        teams = sdata.get("teams", {})
        for p in sdata.get("picks", []):
            if not p.get("cost") or p["cost"] <= 0:
                continue
            pid = p.get("player_id")
            pts = season_points.get(pid)
            if pts is None:
                unmatched += 1
                continue
            wk = weekly_info.get(pid, {})
            owner = owner_key(p, teams)
            graded_picks.append({
                "season": year, "owner": owner, "player": p.get("player"),
                "position": (p.get("position") or "").upper(),
                "cost": p["cost"], "points": pts,
                "ppd": pts / p["cost"],
                "stdev": wk.get("stdev_points_per_game"),
                "avg_ppg": wk.get("avg_points_per_game"),
            })

    print(f"Graded {len(graded_picks)} picks across {GRADED_YEARS} "
          f"({unmatched} picks had no stats match -- likely deep bench/inactive players).")

    # ---- baseline PPD per (season, position) -- the "market rate" ----
    # Computed as SUM(points) / SUM(cost) across all picks at that position/season
    # -- a dollar-weighted aggregate rate, not a mean of individual PPD ratios.
    # A naive mean-of-ratios baseline inherits the same $1-pick distortion as the
    # owner-level metric above (a $1 pick's inflated ratio would drag the "market
    # rate" itself upward, making surplus artificially negative for everyone else).
    baseline_group = defaultdict(lambda: {"points": 0.0, "cost": 0})
    for gp in graded_picks:
        if gp["position"]:
            key = (gp["season"], gp["position"])
            baseline_group[key]["points"] += gp["points"]
            baseline_group[key]["cost"] += gp["cost"]
    baseline_ppd = {
        k: (v["points"] / v["cost"]) for k, v in baseline_group.items() if v["cost"] > 0
    }

    position_market_efficiency = [
        {"season": season, "position": pos, "baseline_ppd": round(val, 3)}
        for (season, pos), val in sorted(baseline_ppd.items())
    ]

    # attach efficiency ratio AND dollar-weighted surplus to every graded pick.
    # NOTE: efficiency_ratio alone is a poor OWNER-LEVEL ranking metric -- a $1
    # pick scoring even modestly produces a huge ratio purely from dividing by
    # 1, which mechanically flatters stars-and-scrubs rosters (lots of $1
    # picks) over balanced ones, regardless of actual skill. surplus_points
    # (points beyond what the market rate for that price implied) is
    # dollar-weighted instead -- a $1 pick can only ever contribute a tiny
    # surplus, while a $60 anchor's real over/under-performance carries
    # proportional weight. Owner rankings below use surplus-per-dollar-spent
    # as the primary metric for this reason; efficiency_ratio is kept per-pick
    # for the best/worst-pick highlights, where it's fine as color commentary.
    for gp in graded_picks:
        base = baseline_ppd.get((gp["season"], gp["position"]))
        gp["efficiency_ratio"] = round(gp["ppd"] / base, 3) if base else None
        gp["surplus_points"] = round(gp["points"] - base * gp["cost"], 1) if base else None

    # ---- per-owner aggregation (excluded owners dropped, same as zimmer_analysis) ----
    by_owner = defaultdict(list)
    for gp in graded_picks:
        if gp["owner"] in EXCLUDED_OWNERS:
            continue
        by_owner[gp["owner"]].append(gp)

    style_by_owner = {}
    if zanalysis:
        for o in zanalysis.get("owners", []):
            style_by_owner[o["owner"]] = o.get("concentration_style")

    owners_out = []
    for owner, picks in by_owner.items():
        ratios = [p["efficiency_ratio"] for p in picks if p["efficiency_ratio"] is not None]
        stdevs = [p["stdev"] for p in picks if p["stdev"] is not None]
        hits = sum(1 for r in ratios if r > 1)
        total_spent = sum(p["cost"] for p in picks)
        total_surplus = sum(p["surplus_points"] for p in picks if p["surplus_points"] is not None)
        best = max(picks, key=lambda p: p["efficiency_ratio"] or 0)
        worst = min(picks, key=lambda p: p["efficiency_ratio"] or 9e9)
        owners_out.append({
            "owner": owner,
            "picks_graded": len(picks),
            "surplus_per_dollar": round(total_surplus / total_spent, 3) if total_spent else None,
            "total_surplus_points": round(total_surplus, 1),
            "avg_efficiency_ratio": round(mean(ratios), 3) if ratios else None,  # supplementary; see note above
            "hit_rate_pct": round(100 * hits / len(ratios), 1) if ratios else None,
            "avg_volatility": round(mean(stdevs), 2) if stdevs else None,
            "total_points_drafted": round(sum(p["points"] for p in picks), 1),
            "best_pick": {"player": best["player"], "season": best["season"],
                          "cost": best["cost"], "points": best["points"],
                          "efficiency_ratio": best["efficiency_ratio"]},
            "worst_pick": {"player": worst["player"], "season": worst["season"],
                           "cost": worst["cost"], "points": worst["points"],
                           "efficiency_ratio": worst["efficiency_ratio"]},
            "concentration_style": style_by_owner.get(owner),
        })
    owners_out.sort(key=lambda o: o["surplus_per_dollar"] if o["surplus_per_dollar"] is not None else -9e9, reverse=True)

    # ---- does drafting STYLE correlate with effectiveness? ----
    style_groups = defaultdict(list)
    for o in owners_out:
        if o["concentration_style"] and o["surplus_per_dollar"] is not None:
            style_groups[o["concentration_style"]].append(o["surplus_per_dollar"])
    style_correlation = [
        {"style": style, "avg_surplus_per_dollar": round(mean(vals), 3), "n_owners": len(vals)}
        for style, vals in style_groups.items()
    ]
    style_correlation.sort(key=lambda s: s["avg_surplus_per_dollar"], reverse=True)

    headlines = derive_headlines(owners_out, position_market_efficiency, style_correlation, GRADED_YEARS)

    out = {
        "years_graded": [y for y in GRADED_YEARS if any(gp["season"] == y for gp in graded_picks)],
        "owners": owners_out,
        "position_market_efficiency": position_market_efficiency,
        "style_correlation": style_correlation,
        "headlines": headlines,
        "unmatched_picks": unmatched,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {OUT_FILE}: {len(owners_out)} owners graded.")


def derive_headlines(owners, pos_market, style_corr, years):
    h = []
    if owners:
        top = owners[0]
        h.append(
            f"Most effective drafter ({min(years)}-{max(years)}): {top['owner']} -- "
            f"averages {top['surplus_per_dollar']:+.2f} surplus points per dollar spent versus "
            f"market rate, hitting value on {top['hit_rate_pct']}% of graded picks."
        )
        worst = owners[-1]
        h.append(
            f"Toughest grade: {worst['owner']} at {worst['surplus_per_dollar']:+.2f} surplus "
            f"points per dollar ({worst['hit_rate_pct']}% hit rate)."
        )
    if pos_market:
        # average baseline PPD per position across all graded years, to find the
        # position where a dollar has historically bought the fewest/most points
        by_pos = defaultdict(list)
        for row in pos_market:
            by_pos[row["position"]].append(row["baseline_ppd"])
        pos_avgs = {p: mean(v) for p, v in by_pos.items()}
        if pos_avgs:
            cheapest = max(pos_avgs.items(), key=lambda kv: kv[1])  # most points per $ = "cheap" position
            priciest = min(pos_avgs.items(), key=lambda kv: kv[1])
            h.append(
                f"{cheapest[0]} has historically delivered the most points per dollar "
                f"({round(cheapest[1],2)} pts/$) -- the efficient place to spend late/opportunistically."
            )
            h.append(
                f"{priciest[0]} has delivered the fewest points per dollar "
                f"({round(priciest[1],2)} pts/$) -- premium {priciest[0]}s command a real price "
                f"premium beyond their raw output; only pay it for the true elite tier."
            )
    if style_corr:
        best_style = style_corr[0]
        h.append(
            f"By drafting style, '{best_style['style']}' owners have graded best on average "
            f"({best_style['avg_surplus_per_dollar']:+.2f} surplus pts/$ across {best_style['n_owners']} "
            f"owner(s)) -- a data-backed signal for how sharps should approach 2026, though the "
            f"sample size per style is small so treat this as a lean, not a law."
        )
    return h


if __name__ == "__main__":
    build()
