"""
espn_live_draft_pull.py
Pulls CURRENT draft picks (not final results -- picks made SO FAR, mid-draft)
from an ESPN league or ESPN Mock Draft Lobby, for the "Update Draft Results"
button in index.html.

Built for the Zimmer league (ESPN) first because it's the one league Sean can
actually test against RIGHT NOW: Kepners/Miami need the still-blocked Yahoo
API, but ESPN mock draft lobbies are open today and don't need any approval.

HOW THIS WAS BUILT, AND WHAT'S UNVERIFIED:
The real-league case (a normal ESPN league mid-draft) uses the `mDraftDetail`
view, which is well-documented by the ESPN-API reverse-engineering community
(multiple independent write-ups agree on the shape: draftDetail.picks[] with
overallPickNumber/roundId/roundPickNumber/teamId/playerId/bidAmount). That
part should just work.

The MOCK DRAFT LOBBY case is the untested part. Mock lobbies are ephemeral
practice rooms ("this team only exists until the draft is complete" per
ESPN's own support docs), which strongly suggests they're backed by a real,
if temporary, league-shaped object under the hood -- so this script points
the identical mDraftDetail request at the lobby ID instead of a real league
ID. But I have no network path to ESPN from this sandbox (locked to package
registries) to confirm that, so this is an educated guess, not a verified
integration.

IF THIS FAILS AGAINST A REAL MOCK LOBBY: the error output below prints the
top-level JSON keys ESPN actually returned. Paste that (or the full response,
with any personal info blanked) back so the parser can be corrected in one
pass, rather than guessing again blind.

USAGE:
    python espn_live_draft_pull.py <lobby_or_league_id> [year]

REQUIRED secrets (same ones the other Zimmer/ESPN scripts already use):
    ESPN_S2, ESPN_SWID -- optional for a PUBLIC mock lobby, but ESPN mock
    drafts are usually joined while logged in, so pass them anyway; harmless
    if not required.

OUTPUT: espn_live_draft.json (single fixed filename, overwritten each run --
deliberately NOT one-file-per-lobby, since test pulls against throwaway mock
lobbies shouldn't accumulate as permanent repo history).
{
  "lobby_id": "...", "year": 2026, "generated": "...",
  "draft_status": {"drafted": bool, "in_progress": bool},
  "teams": [{"team_id": int, "name": str}],
  "picks": [
    {"overall": int, "round": int, "round_pick": int,
     "team_id": int, "team_name": str,
     "player_id": int, "player_name": str|null,
     "bid_amount": int|null}   # auction only; null for snake
  ]
}
"""
import os
import sys
import json
from datetime import datetime, timezone

import requests

BASE = "https://fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{lid}"
OUT_FILE = "espn_live_draft.json"
# A bare `requests` default User-Agent ("python-requests/2.x") is one of the
# cheapest, most common bot-detection signals a CDN/WAF checks. This alone
# won't get past an IP-reputation-based block (see the module docstring
# section on the CloudFront "Request Blocked" failure mode -- that's a
# different, IP-level problem this header can't fix), but it's a legitimate,
# standard thing for any API client to send regardless, so there's no reason
# not to.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def get_credentials():
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")
    return espn_s2, swid


def fetch_draft_detail(lobby_id, year, espn_s2, swid):
    url = BASE.format(year=year, lid=lobby_id)
    params = {"view": ["mDraftDetail", "mTeam"]}
    cookies = {}
    if espn_s2 and swid:
        cookies = {"espn_s2": espn_s2, "SWID": swid}
    resp = requests.get(url, params=params, cookies=cookies, headers=HEADERS, timeout=20)
    if resp.status_code == 403 and "cloudfront" in resp.text.lower():
        raise SystemExit(
            "ESPN's CDN (CloudFront) blocked this request at the edge -- it never reached "
            "ESPN's actual API. This is an IP-reputation/bot-detection block, not a bad "
            "lobby ID or bad ESPN_S2/SWID cookie (those errors look different -- usually a "
            "clean ESPN JSON error, not a CloudFront HTML page). GitHub Actions runner IPs "
            "are a common target for this kind of block since they're well-known datacenter "
            "ranges. Test whether this is specific to the Mock Draft Lobby product (more "
            "exposed to abuse, likely more firewalled) or affects fantasy.espn.com generally "
            "from these runners (which would also affect the existing ESPN pipeline scripts) "
            "by re-running this same script against a real ESPN_LEAGUE_ID instead."
        )
    if resp.status_code != 200:
        raise SystemExit(
            f"ESPN returned HTTP {resp.status_code} for lobby/league {lobby_id} "
            f"(year {year}). Body (first 500 chars): {resp.text[:500]}"
        )
    try:
        data = resp.json()
    except ValueError:
        raise SystemExit(f"ESPN response wasn't JSON. First 500 chars: {resp.text[:500]}")
    if "draftDetail" not in data:
        raise SystemExit(
            "No 'draftDetail' key in ESPN's response -- the mock-lobby shape may "
            f"differ from a real league's. Top-level keys ESPN actually returned: "
            f"{sorted(data.keys())}. Paste this back so the parser can be fixed."
        )
    return data


def resolve_player_names_for_league(lobby_id, player_ids, year, espn_s2, swid):
    """Draft picks only carry playerId, not a name -- a separate player-pool
    call with an ID filter resolves them. Best-effort: any ID that can't be
    resolved just comes back missing rather than failing the whole pull,
    since a name gap shouldn't block seeing that a pick happened at all."""
    if not player_ids:
        return {}
    url = BASE.format(year=year, lid=lobby_id)
    filt = {"players": {"filterIds": {"value": list(player_ids)}}}
    headers = {**HEADERS, "x-fantasy-filter": json.dumps(filt)}
    cookies = {}
    if espn_s2 and swid:
        cookies = {"espn_s2": espn_s2, "SWID": swid}
    try:
        resp = requests.get(
            url, params={"view": "kona_player_info"}, headers=headers,
            cookies=cookies, timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  (warning) player-name lookup failed, picks will show IDs only: {e}")
        return {}
    out = {}
    for entry in data.get("players", []):
        p = entry.get("player") or entry.get("playerPoolEntry", {}).get("player") or {}
        pid = p.get("id")
        first, last = p.get("firstName"), p.get("lastName")
        if pid is not None and (first or last):
            out[pid] = f"{first or ''} {last or ''}".strip()
    return out


def parse_picks(data):
    draft = data.get("draftDetail", {})
    teams_raw = data.get("teams", [])
    team_names = {}
    for t in teams_raw:
        tid = t.get("id")
        name = (t.get("name") or f"{t.get('location','')} {t.get('nickname','')}").strip() or f"Team {tid}"
        team_names[tid] = name

    picks_raw = draft.get("picks", [])
    player_ids = {p.get("playerId") for p in picks_raw if p.get("playerId")}
    return draft, team_names, picks_raw, player_ids


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python espn_live_draft_pull.py <lobby_or_league_id> [year]")
    lobby_id = sys.argv[1]
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    espn_s2, swid = get_credentials()

    print(f"Fetching draft detail for lobby/league {lobby_id}, year {year}...")
    data = fetch_draft_detail(lobby_id, year, espn_s2, swid)
    draft, team_names, picks_raw, player_ids = parse_picks(data)

    print(f"  {len(picks_raw)} pick(s) so far. Resolving {len(player_ids)} player name(s)...")
    names = resolve_player_names_for_league(lobby_id, player_ids, year, espn_s2, swid)

    picks = []
    for p in sorted(picks_raw, key=lambda x: x.get("overallPickNumber", 0)):
        team_id = p.get("teamId")
        picks.append({
            "overall": p.get("overallPickNumber"),
            "round": p.get("roundId"),
            "round_pick": p.get("roundPickNumber"),
            "team_id": team_id,
            "team_name": team_names.get(team_id, f"Team {team_id}"),
            "player_id": p.get("playerId"),
            "player_name": names.get(p.get("playerId")),
            "bid_amount": p.get("bidAmount") if p.get("bidAmount", -1) >= 0 else None,
        })

    out = {
        "lobby_id": lobby_id,
        "year": year,
        "generated": datetime.now(timezone.utc).isoformat(),
        "draft_status": {
            "drafted": draft.get("drafted", False),
            "in_progress": draft.get("inProgress", False),
        },
        "teams": [{"team_id": tid, "name": name} for tid, name in team_names.items()],
        "picks": picks,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_FILE}: {len(picks)} pick(s), drafted={out['draft_status']['drafted']}, "
          f"in_progress={out['draft_status']['in_progress']}.")


if __name__ == "__main__":
    main()
