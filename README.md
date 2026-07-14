# Draft Control

Personal fantasy football draft management site — Mission Control aesthetic —
covering three leagues: Kepners Keepers, Miami Domers (both Yahoo), and Zimmer
(ESPN, auction).

## Live site
Published via GitHub Pages from this repo's root — `index.html` is the entry point.

## League formats & keeper rules
**Source of truth: [`league_rules.json`](league_rules.json).** Team count, draft
format (snake/auction), scoring, and keeper cost rules for all three leagues
live there, hand-maintained. `convert_bigboard.py` merges it into
`bigboard.json` at build time (`leagues.<league>.rules`) so the site itself can
reference it. Edit `league_rules.json` directly when rules change — nothing
else needs to change to pick it up.

Quick summary (see the JSON for exact wording):
- **Kepners** — 12 teams, snake, 2 keepers, keeper cost = drafted round − 2. Undrafted players can't be kept.
- **Miami** — 8 teams, snake, 2 keepers, keeper cost = drafted round − 3. Same eligibility rule.
- **Zimmer** — 12 teams, auction, no keepers.

## File layout
```
index.html                  Main site (Draft view + Zimmer History view)
bigboard.json                Generated player/league data the site loads
league_rules.json             Hand-maintained league format/keeper rules (see above)
convert_bigboard.py          Excel -> bigboard.json converter (merges league_rules.json in)

yahoo_setup.py                Yahoo OAuth handshake (one-time, local)
yahoo_kepners_history.py      Pulls Kepners historical drafts from Yahoo

espn_zimmer_history.py        Pulls Zimmer historical auction drafts from ESPN
zimmer_analysis.py            Aggregates Zimmer history -> tier bid spreads, owner tendencies

.github/workflows/
  update-bigboard.yml          Regenerates bigboard.json from the Excel (manual trigger)
  update-zimmer-history.yml    Pulls ESPN history + runs zimmer_analysis.py (manual trigger)
```

## Updating data before draft day
1. Upload the new season's Big Board Excel to `data/FY26_BigBoard.xlsx`
2. Actions tab → **Update Big Board Data** → Run workflow
3. (Zimmer only) Actions tab → **Update Zimmer Draft History** → Run workflow

## Secrets required (repo Settings → Secrets and variables → Actions)
- `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_REFRESH_TOKEN` — Yahoo API (Kepners/Miami)
- `ESPN_LEAGUE_ID`, `ESPN_S2`, `ESPN_SWID` — ESPN API (Zimmer)

See each script's docstring for how to obtain these.
