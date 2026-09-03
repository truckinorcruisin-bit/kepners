"""
espn_player_values.py
Pulls two things ESPN tracks for the CURRENT/upcoming season's full player pool
that the draft-day Player Card needs:
  1. projected_total_points -- ESPN's preseason point projection (feeds our WAR calc)
  2. auctionValueAverage -- ESPN's own crowd-sourced "recommended bid" (the number
     ESPN's own live auction UI shows as a suggested bid)

Neither of these comes from the season/weekly stats scripts -- those pull ACTUAL
results for completed seasons. This script pulls PROJECTIONS for a season that
hasn't happened yet (or is in progress), which is a different ESPN data source
(statSourceId=1 instead of 0 in the same underlying stats blob).

IMPORTANT CAVEAT: ESPN doesn't populate meaningful preseason projections and
auction values until sometime before the season -- exactly when varies by year.
If you run this too early, most players will show projected_points=0 and/or
auctionValueAverage=0/missing. That's ESPN not having published the data yet,
not a bug here. Re-run closer to draft day if values look empty.

REQUIRED environment variables (same as the other ESPN scripts):
    ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID

OUTPUT: espn_player_values_<year>.json
{
  "year": 2026, "generated": "...",
  "players": [
    { "player_id":, "name":, "position":, "pro_team":,
      "projected_total_points":, "auction_value_avg": },
    ...
  ]
}

This merges into bigboard.json by NAME (normalized: lowercase, strip Jr./Sr./
suffixes and punctuation) via convert_bigboard.py, since the Big Board comes
from your own Excel and has no ESPN player_id to join on directly. Name
matching is best-effort -- convert_bigboard.py logs any Big Board players it
couldn't match, so you can spot-check for suffix/nickname mismatches.
"""
import os
import sys
import json
import re
from datetime import datetime, timezone
from espn_api.football import League
from espn_api.football.player import Player

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
OUT_FILE = f"espn_player_values_{YEAR}.json"
POOL_SIZE = 3000


def get_credentials():
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")
    if not league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID (env var or GitHub Secret) first.")
    return int(league_id), espn_s2, swid


def extract_auction_value(entry):
    """auctionValueAverage lives in the raw ownership block, which the espn_api
    Player class doesn't expose as a named attribute -- read it directly from
    the raw entry, handling both the 'playerPoolEntry' and bare 'player' shapes
    the same way the library's own Player.__init__ does."""
    raw_player = entry.get("playerPoolEntry", {}).get("player") if "playerPoolEntry" in entry else entry.get("player", entry)
    ownership = (raw_player or {}).get("ownership", {})
    return ownership.get("auctionValueAverage")


def extract_bye_week(entry):
    """Bye week is exposed directly on the raw player object as 'byeWeek'
    (0 or missing if unknown -- e.g. before schedules are finalized)."""
    raw_player = entry.get("playerPoolEntry", {}).get("player") if "playerPoolEntry" in entry else entry.get("player", entry)
    bye = (raw_player or {}).get("byeWeek")
    return bye if bye else None


def fetch_full_player_pool(league, year, size=POOL_SIZE):
    params = {"view": "kona_player_info", "scoringPeriodId": 0}
    filters = {
        "players": {
            "limit": size,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    headers = {"x-fantasy-filter": json.dumps(filters)}
    data = league.espn_request.league_get(params=params, headers=headers)
    raw_players = data.get("players", [])

    out = []
    dropped = 0
    for entry in raw_players:
        try:
            p = Player(entry, year)
        except Exception:
            dropped += 1
            continue
        out.append({
            "player_id": p.playerId,
            "name": p.name,
            "position": p.position,
            "pro_team": p.proTeam,
            "bye_week": extract_bye_week(entry),
            "projected_total_points": p.projected_total_points,
            "auction_value_avg": extract_auction_value(entry),
        })
    if dropped:
        # Was previously a silent `continue` with no trace at all -- if K (or
        # any position) is missing from the output, "0 dropped" now rules out
        # "parsed then discarded" as the cause, pointing straight at the fetch
        # itself instead of sending someone hunting through Player.__init__.
        print(f"  ({dropped} raw entries failed to parse and were dropped)")
    return out


# BUG FOUND: kickers never appear in fetch_full_player_pool's output, at ANY
# pool size -- verified directly against a real run: requesting up to 3000
# players sorted by ownership% returned 0 kickers, while D/ST (also a
# low-ownership position) came through fine (32 of 32). That's not a
# convert_bigboard.py matching problem downstream -- every Kicker's
# projectedWar was null because there was never any ESPN entry to match
# against in the first place.
#
# This looks like a real quirk of ESPN's ownership-sorted kona_player_info
# query specifically, not something POOL_SIZE can fix. The workaround: a
# SEPARATE request explicitly filtered to the Kicker slot (ESPN slot id 17,
# confirmed against espn_api's own POSITION_MAP), which sidesteps the
# ownership sort entirely and asks for kickers directly. UNVERIFIED against a
# live league -- I have no network path to ESPN to confirm this filter shape
# works. If it still returns 0, the printed count below will say so plainly;
# paste that back and the ESPN response shape (add a raw-response dump if
# needed) rather than guessing at a different filter blind.
K_SLOT_ID = 17


def fetch_position_pool(league, year, slot_id, size=100):
    params = {"view": "kona_player_info", "scoringPeriodId": 0}
    filters = {
        "players": {
            "limit": size,
            "filterSlotIds": {"value": [slot_id]},
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    headers = {"x-fantasy-filter": json.dumps(filters)}
    data = league.espn_request.league_get(params=params, headers=headers)
    raw_players = data.get("players", [])
    out = []
    for entry in raw_players:
        try:
            p = Player(entry, year)
        except Exception:
            continue
        out.append({
            "player_id": p.playerId,
            "name": p.name,
            "position": p.position,
            "pro_team": p.proTeam,
            "bye_week": extract_bye_week(entry),
            "projected_total_points": p.projected_total_points,
            "auction_value_avg": extract_auction_value(entry),
        })
    return out


def main():
    league_id, espn_s2, swid = get_credentials()
    print(f"Loading league {league_id}, {YEAR} season...")
    league = League(league_id=league_id, year=YEAR, espn_s2=espn_s2, swid=swid)

    print("Fetching full player pool (projections + auction values)...")
    players = fetch_full_player_pool(league, YEAR)

    kicker_count = sum(1 for p in players if p["position"] == "K")
    if kicker_count == 0:
        print("  0 kickers in the main pool (known ESPN quirk) -- trying an explicit K-slot fetch...")
        kickers = fetch_position_pool(league, YEAR, K_SLOT_ID)
        print(f"  Explicit K-slot fetch returned {len(kickers)} kicker(s).")
        if kickers:
            seen_ids = {p["player_id"] for p in players}
            players.extend(k for k in kickers if k["player_id"] not in seen_ids)
        else:
            print("  Still 0 -- this filter shape didn't work either. Kickers will show '—' "
                  "on the board until this is resolved differently.")

    have_proj = sum(1 for p in players if p["projected_total_points"])
    have_auction = sum(1 for p in players if p["auction_value_avg"])
    print(f"Fetched {len(players)} players: {have_proj} have nonzero projections, "
          f"{have_auction} have an auction value.")
    if have_proj < len(players) * 0.1 or have_auction < len(players) * 0.1:
        print("WARNING: most players show no projection/auction value -- ESPN "
              "likely hasn't published preseason data yet. Re-run closer to draft day.")

    out = {"year": YEAR, "generated": datetime.now(timezone.utc).isoformat(), "players": players}
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE}.")


if __name__ == "__main__":
    main()
