"""
zimmer_draft_grades.py
Joins WHAT WAS PAID (zimmer_draft_history.json) against ACTUAL VALUE PRODUCED
(espn_season_stats_<year>.json / espn_weekly_stats_<year>.json) to grade
historical drafting effectiveness -- "who gets more value per dollar than the
market rate," not just "who spent the most on good players."

REQUIRES (run these first, for each year you want graded):
  espn_season_stats_<year>.json   -- via espn_season_stats.py / "Update Season Stats" workflow
  espn_weekly_stats_<year>.json   -- via espn_weekly_stats.py / "Update Weekly Stats" workflow
Years configured below: GRADED_YEARS = [2023, 2024, 2025].

THE JOIN KEY: ESPN player_id is the same identifier across draft history and
season/weekly stats (both come from ESPN's own player IDs), so this is an
exact ID join -- no fuzzy name matching, no ambiguity from suffixes/nicknames.

WHY WAR, NOT RAW POINTS-PER-DOLLAR:
An earlier version of this script graded picks on raw points-per-dollar. That
metric conflates "scores a lot" with "actually matters for winning" -- it rated
Kickers as great value, since they're cheap and score fairly consistently. But
the gap between an elite K and a $1 waiver-replacement K is tiny, so even a
"great value" kicker barely moves the needle on your lineup, unlike a bargain
RB/WR where the gap to replacement is enormous. The fix: grade against WAR
(points above replacement) instead of raw points.

Replacement level per position/season is computed from the FULL player pool
(espn_season_stats_<year>.json includes rostered AND free-agent players) --
the score of the player at REPLACEMENT_RANK (see config below), representing
"what you could get for free off waivers." A position with a small gap between
its top players and its replacement level (K, DEF) will correctly show low WAR
ceiling regardless of price; a position with a huge gap (elite RB/WR) will
correctly show that paying up there has real payoff.

THE CORE METRIC -- WAR-surplus per dollar, not a raw ratio:
For every pick:
    war = player's actual season points - replacement-level points for that position/season
    baseline WAR/$ = league-wide (WAR summed / dollars summed) for that position+season
                     -- a DOLLAR-WEIGHTED aggregate, not a mean of individual ratios,
                     since a naive mean is distorted by $1 picks (any nonzero WAR on a
                     $1 pick produces a huge ratio purely from dividing by 1)
    war_surplus = war - (baseline WAR/$ * cost)  -- also dollar-weighted, so a $1
                  pick can only ever contribute a tiny surplus, while a $60
                  anchor's real over/under-performance carries proportional weight
Averaging war_surplus per dollar spent across an owner's picks grades DRAFTING
SKILL independent of budget size and independent of position-driven raw-point
volume.

VOLATILITY: each pick's stdev_points_per_game (from the weekly file) is
attached too, so "effective" can be split from "consistent" -- an owner might
grade well on WAR-surplus but only by hitting high-variance boom/bust players.

OUTPUT: zimmer_draft_grades.json
{
  "years_graded": [2023, 2024, 2025],
  "owners": [ { owner, picks_graded, war_surplus_per_dollar (PRIMARY ranking
                metric, dollar-weighted), total_war_surplus, hit_rate_pct,
                avg_volatility, total_war_drafted, best_pick, worst_pick,
                concentration_style (merged in from zimmer_analysis.json if present) }, ... ],
  "position_market_efficiency": [ {position, season, baseline_war_per_dollar}, ... ],
  "style_correlation": [ {style, avg_war_surplus_per_dollar, n_owners}, ... ],
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

# How many players deep, per position, before you hit "replacement level" --
# i.e. what a 12-team league could realistically get for free off waivers.
# This is an ASSUMPTION reflecting typical 12-team starting-lineup demand
# (starters + realistic flex/streaming share); tune if Zimmer's actual roster
# construction differs meaningfully. The whole point of grading against WAR
# instead of raw points is that shallow, low-variance positions (K, DEF) have
# almost no gap between "elite" and "replacement" -- so even a cheap, highly-
# ranked K/DEF pick should show minimal WAR, correctly reflecting that it
# barely moved the needle on the lineup, unlike a bargain RB/WR.
REPLACEMENT_RANK = {
    "QB": 15,   # ~12 starters + a few streaming options deep
    "RB": 30,   # ~24 starters (2/team) + partial flex share
    "WR": 36,   # ~24-30 starters (2-3/team) + partial flex share
    "TE": 15,   # ~12 starters + minimal streaming depth
    "K": 12,    # 1/team, minimal bench value league-wide
    "DEF": 12, "D/ST": 12,
}


def load_stats_for_year(year):
    """Returns (season_points_by_id, weekly_info_by_id, replacement_points_by_pos)
    or (None, None, None) if the required files aren't present yet for this year."""
    sfile = f"espn_season_stats_{year}.json"
    wfile = f"espn_weekly_stats_{year}.json"
    if not (os.path.exists(sfile) and os.path.exists(wfile)):
        print(f"  {year}: missing {sfile if not os.path.exists(sfile) else wfile} -- skipping this year.")
        return None, None, None

    with open(sfile) as f:
        sdata = json.load(f)
    season_points = {}
    replacement_points = {}
    for pos, plist in sdata["players_by_position"].items():
        for p in plist:
            season_points[p["player_id"]] = p["total_points"]
        # plist is already sorted descending by total_points (see espn_season_stats.py).
        # Replacement level = the score of the player AT the configured rank, using
        # the FULL pool (rostered + free agent) -- not just drafted players -- since
        # replacement level means "what's on waivers," which by definition includes
        # undrafted players.
        rank = REPLACEMENT_RANK.get(pos, 20)
        idx = min(rank, len(plist)) - 1
        if idx >= 0 and plist:
            replacement_points[pos] = plist[idx]["total_points"]

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
    return season_points, weekly_info, replacement_points


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
        season_points, weekly_info, replacement_points = load_stats_for_year(year)
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
            pos = (p.get("position") or "").upper()
            replacement = replacement_points.get(pos, 0)
            war = pts - replacement  # points above replacement -- the value that actually matters
            wk = weekly_info.get(pid, {})
            owner = owner_key(p, teams)
            graded_picks.append({
                "season": year, "owner": owner, "player": p.get("player"),
                "position": pos,
                "cost": p["cost"], "points": pts, "replacement_points": replacement,
                "war": war,
                "ppd": pts / p["cost"],           # kept for reference/display only
                "war_per_dollar": war / p["cost"],  # this pick's own WAR/$ -- see baseline note below
                "stdev": wk.get("stdev_points_per_game"),
                "avg_ppg": wk.get("avg_points_per_game"),
            })

    print(f"Graded {len(graded_picks)} picks across {GRADED_YEARS} "
          f"({unmatched} picks had no stats match -- likely deep bench/inactive players).")

    # ---- baseline PPD per (season, position) -- the "market rate" ----
    # Computed as SUM(points) / SUM(cost) across all picks at that position/season
    # -- a dollar-weighted aggregate rate, not a mean of individual ratios.
    # A naive mean-of-ratios baseline inherits the same $1-pick distortion as the
    # owner-level metric below (a $1 pick's inflated ratio would drag the "market
    # rate" itself upward, making surplus artificially negative for everyone else).
    #
    # CRITICAL: this is computed on WAR (points above replacement), not raw
    # points. Grading on raw points/$ makes shallow, low-variance positions
    # (K, DEF) look like great "value" simply because they're cheap and score
    # somewhat consistently -- but the gap between an elite K/DEF and a $1
    # waiver-level one is tiny, so that "value" barely helps you win. WAR
    # captures the thing that actually matters: how much better than a free
    # replacement this pick was.
    baseline_group = defaultdict(lambda: {"war": 0.0, "cost": 0})
    for gp in graded_picks:
        if gp["position"]:
            key = (gp["season"], gp["position"])
            baseline_group[key]["war"] += gp["war"]
            baseline_group[key]["cost"] += gp["cost"]
    baseline_war_ppd = {
        k: (v["war"] / v["cost"]) for k, v in baseline_group.items() if v["cost"] > 0
    }

    position_market_efficiency = [
        {"season": season, "position": pos, "baseline_war_per_dollar": round(val, 3)}
        for (season, pos), val in sorted(baseline_war_ppd.items())
    ]

    # attach WAR-efficiency ratio AND dollar-weighted WAR-surplus to every pick.
    # Same $1-pick distortion note as before applies to the ratio -- surplus is
    # the metric owner rankings actually use; ratio is kept for pick-level color
    # commentary (best/worst pick highlights).
    for gp in graded_picks:
        base = baseline_war_ppd.get((gp["season"], gp["position"]))
        gp["war_efficiency_ratio"] = round(gp["war_per_dollar"] / base, 3) if base else None
        gp["war_surplus"] = round(gp["war"] - base * gp["cost"], 1) if base else None

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
        ratios = [p["war_efficiency_ratio"] for p in picks if p["war_efficiency_ratio"] is not None]
        stdevs = [p["stdev"] for p in picks if p["stdev"] is not None]
        hits = sum(1 for r in ratios if r > 1)
        total_spent = sum(p["cost"] for p in picks)
        total_surplus = sum(p["war_surplus"] for p in picks if p["war_surplus"] is not None)
        best = max(picks, key=lambda p: p["war_efficiency_ratio"] or -9e9)
        worst = min(picks, key=lambda p: p["war_efficiency_ratio"] if p["war_efficiency_ratio"] is not None else 9e9)
        owners_out.append({
            "owner": owner,
            "picks_graded": len(picks),
            "war_surplus_per_dollar": round(total_surplus / total_spent, 3) if total_spent else None,
            "total_war_surplus": round(total_surplus, 1),
            "hit_rate_pct": round(100 * hits / len(ratios), 1) if ratios else None,
            "avg_volatility": round(mean(stdevs), 2) if stdevs else None,
            "total_war_drafted": round(sum(p["war"] for p in picks), 1),
            "best_pick": {"player": best["player"], "season": best["season"],
                          "cost": best["cost"], "points": best["points"], "war": best["war"],
                          "war_efficiency_ratio": best["war_efficiency_ratio"]},
            "worst_pick": {"player": worst["player"], "season": worst["season"],
                           "cost": worst["cost"], "points": worst["points"], "war": worst["war"],
                           "war_efficiency_ratio": worst["war_efficiency_ratio"]},
            "concentration_style": style_by_owner.get(owner),
        })
    owners_out.sort(key=lambda o: o["war_surplus_per_dollar"] if o["war_surplus_per_dollar"] is not None else -9e9, reverse=True)

    # ---- does drafting STYLE correlate with effectiveness? ----
    style_groups = defaultdict(list)
    for o in owners_out:
        if o["concentration_style"] and o["war_surplus_per_dollar"] is not None:
            style_groups[o["concentration_style"]].append(o["war_surplus_per_dollar"])
    style_correlation = [
        {"style": style, "avg_war_surplus_per_dollar": round(mean(vals), 3), "n_owners": len(vals)}
        for style, vals in style_groups.items()
    ]
    style_correlation.sort(key=lambda s: s["avg_war_surplus_per_dollar"], reverse=True)

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
            f"averages {top['war_surplus_per_dollar']:+.2f} WAR (points above replacement) per "
            f"dollar spent versus market rate, hitting value on {top['hit_rate_pct']}% of graded picks."
        )
        worst = owners[-1]
        h.append(
            f"Toughest grade: {worst['owner']} at {worst['war_surplus_per_dollar']:+.2f} "
            f"WAR-surplus per dollar ({worst['hit_rate_pct']}% hit rate)."
        )
    if pos_market:
        # average baseline WAR/$ per position across all graded years -- this is
        # the metric that correctly separates "scores a lot" from "actually
        # matters": K/DEF often show a low ceiling here even if their raw
        # points/$ looked good, because there's little gap between an elite
        # and a replacement-level K/DEF -- so overpaying there rarely pays off.
        by_pos = defaultdict(list)
        for row in pos_market:
            by_pos[row["position"]].append(row["baseline_war_per_dollar"])
        pos_avgs = {p: mean(v) for p, v in by_pos.items()}
        if pos_avgs:
            best_pos = max(pos_avgs.items(), key=lambda kv: kv[1])
            worst_pos = min(pos_avgs.items(), key=lambda kv: kv[1])
            h.append(
                f"{best_pos[0]} has delivered the most WAR per dollar historically "
                f"({round(best_pos[1],3)} WAR/$) -- the position where spending actually pays off most."
            )
            h.append(
                f"{worst_pos[0]} has delivered the least WAR per dollar "
                f"({round(worst_pos[1],3)} WAR/$) -- even a 'good value' pick here barely outperforms "
                f"a free replacement, since the position has little top-to-bottom spread."
            )
    if style_corr:
        best_style = style_corr[0]
        h.append(
            f"By drafting style, '{best_style['style']}' owners have graded best on average "
            f"({best_style['avg_war_surplus_per_dollar']:+.2f} WAR-surplus/$ across "
            f"{best_style['n_owners']} owner(s)) -- a data-backed signal for how sharps should "
            f"approach 2026, though the sample size per style is small so treat this as a lean, not a law."
        )
    return h


if __name__ == "__main__":
    build()
