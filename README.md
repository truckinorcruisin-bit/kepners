# Draft Control — Manager Cockpit

A personal fantasy football command center, in a Mission Control look (amber and cyan on navy, IBM Plex Mono). It manages **four leagues across two platforms**: lineup and waiver intelligence in season, plus the draft, keeper and history tools used before the season.

**Live site:** https://truckinorcruisin-bit.github.io/kepners/ (GitHub Pages, served from this repo's root; `index.html` is the entry point)

*Last updated: October 1, 2026 (NFL Week 4).*

---

## Contents

1. [The leagues](#the-leagues)
2. [What's in the site](#whats-in-the-site)
3. [The numbers behind it](#the-numbers-behind-it)
4. [How the data gets here](#how-the-data-gets-here)
5. [**Operations and Support**](#operations-and-support)
   - [Weekly routine](#weekly-routine)
   - [Refresh the Yahoo cookie](#refresh-the-yahoo-cookie)
   - [Other credentials that expire](#other-credentials-that-expire)
   - [The YahooScraper Shortcut, explained](#the-yahooscraper-shortcut-explained)
   - [Troubleshooting](#troubleshooting)
   - [Health check](#health-check-60-seconds)
   - [Common changes](#common-changes)
6. [Workflow reference](#workflow-reference)
7. [Repo layout](#repo-layout)
8. [Design notes and lessons learned](#design-notes-and-lessons-learned)
9. [Known limitations](#known-limitations)
10. [Security notes](#security-notes)

---

## The leagues

| League | Platform | Format | Teams | Keepers | My team |
|---|---|---|---|---|---|
| **Kepners Keepers** | Yahoo (league 102398) | Snake | 12 | 2, cost = drafted round − 2 | The Pickups (team 12) |
| **Miami Domers** | Yahoo (league 391024) | Snake | 8 | 2, cost = drafted round − 3 | Hannah Lee's Revenge (team 4) |
| **Zimmer** | ESPN | **Snake in 2026** (auction through 2025) | 12 | None | Elements of Intrigue |
| **Inspire11** | ESPN | — | 12 | — | 1st and 11 (team 10) |

- All leagues use **standard (non-PPR) scoring**. ESPN is the single projection engine for every league, since Yahoo exposes no equivalent weekly projections.
- **Miami UDFA rule (new for 2026):** a team may keep one undrafted free agent at a round-13 cost. It counts as one of the two keeper slots, not a bonus third.
- **Source of truth for rules:** [`league_rules.json`](league_rules.json), hand-maintained. `convert_bigboard.py` merges it into `bigboard.json`. Edit it directly when rules change.
- Inspire11 appears only in the in-season Manager view. The draft, prep and history tools cover the other three.

---

## What's in the site

Three top-level views: **Manager** (the default, built for a phone), **Draft**, and **Players**.

### Manager view (in season)

**Home screen**
- **One card per league** showing your team, projected points per week, and your weakest starting slots.
- **"This week, across all your teams"**: a combined action list with `INJURY` and `WEAK` badges, so one glance shows what needs attention in every league. Tap a row to jump to that league.
- **Cross-league player search** ("is he still available, and if not, who has him?"). Type two or more characters and see that player's status in all four leagues: yours, a rival's, or a free agent.

**League detail page** (tap a card)

| Section | What it tells you |
|---|---|
| **Starting lineup vs. the rest of the league** | Each slot's rest-of-season points per week, your rank among the league's teams, and the gap to the league median. Shows where you're strong and where you're exposed. |
| **Bench depth** | Your best flex-eligible bench players and how that depth ranks. |
| **League power rankings** | Two boards: **Starters** (total surplus over the league median at every slot) and **Bench depth** (top three flex-eligible bench players). Two boards because a single blended score would hide which one a team is actually winning on. |
| **Power rankings over time** | A weekly bump chart of every team's rank, with a Starters / Bench toggle. Your line is amber. Tap a team to highlight it. The table shows rank, change vs. last week, and the value. *One point per NFL week, never daily.* History began in Week 4 and can't be backfilled. |
| **Waiver targets** | Free agents who would genuinely upgrade your lineup. Each is scored against the weakest starter he could actually replace, not against raw points. Flex-eligible positions can bump any flex slot. K is skipped in leagues with no kicker slot. |
| **FAAB assistant** | A recommended winning bid per target, with an expandable per-player breakdown. Price is driven by *competition*, so the model estimates how many rivals will bid from: who needs him (whose lineup he'd improve), who actually bids (each manager's claim history), and heat (Sleeper's trending-adds rank). The league's own contested auctions then recalibrate the curve. Includes an "aggressive" bid and a search box for any free agent. Available for leagues with ESPN claim history. |
| **FAAB tendencies** | Each rival's historical claim activity and bids. |
| **Trade targets** | Packages from 1-for-1 up to 2-for-2. Every suggestion is judged by **starting-lineup impact on both sides** and is only offered if it also helps the other team on at least one horizon (rest of season, this week, or the next three weeks). That horizon is the pitch ("helps them this week +6.1" means bye or injury cover). Pin a player on either side with **Trade away / Trade for / Deal size**. K and DEF are excluded. |

**Notes on the Yahoo leagues.** Kepners and Miami rosters come from the phone bridge (see [How the data gets here](#how-the-data-gets-here)). Because Yahoo's API is unavailable, their **free-agent pool is derived**: ESPN's projected player pool minus everyone rostered in that league (top 200). Weekly projections are borrowed from the ESPN pulls, which is valid because every league uses the same scoring. Record and standing show "—" for them, and FAAB bid numbers need ESPN claim history.

### Draft view (all three drafting leagues)

A three-panel, resizable cockpit: **My Team** | **Player Board** | **Player Deep Dive**, with a Live Feed ticker above.

- **Player Board:** tiered or flat toggle. League-aware columns show Yahoo/ESPN rank and target rank as `rank (round.pick)`, plus availability dots.
- **Player Deep Dive:** availability at your next pick, roster fit, tier position (top of a tier is a mild reach, bottom is the last chance at that quality), bye-week overlaps, team stacking, and market drift.
- **Snake Draft Log:** manual pick entry with autocomplete and auto-advancing snake order, plus Undo and Clear. It feeds the same state that greys out taken players, fills **My Team**, and updates roster fit. Names that don't match a player are flagged, not silently dropped. *Session-only: a reload clears it.*
- **Keeper-aware draft schedule:** a keeper costs its owner that round's pick, so the real order isn't a clean snake. One shared schedule drives the log, **My Team**, Deep Dive and plan, so they can't drift apart.
- **Next Best Pick engine:** what to take now, with a hard gate that keeps DEF and K out of recommendations until the roster needs them (the "round-5 defense" fix), and a marginal-value discount once the gate opens.
- **TruRank Draft Plan:** which position to take in each of *your* rounds, based on who will realistically be on the board and what their slots' TruRank says.
- **WAR chart and Owner Profiles:** a clustered WAR bar chart, and drafting tendencies with ADP and reach analysis.
- **Auction cockpit (dormant):** the full Zimmer auction experience (budget, max bid, nominations, opponent intel) is preserved. It reactivates by changing `"format": "auction"` in `league_rules.json`, with no code change. Never hardcode "Zimmer means auction."
- **Update Draft Results (Zimmer):** a button that fires the *Update Live Draft* workflow from the page, waits for it to finish, then reloads the results.

Draft sub-modes: **Draft**, **Prep** (Kepners and Miami), **History** and **Replay** (Zimmer).

### Prep (Kepners and Miami)

- **Draft Order & Keepers:** green means confirmed (Google Sheet or an owner-confirmed entry), amber italic with `*` means predicted, and a **red ⚠** means the name doesn't match any Big Board player (a typo to fix).
- **Owner Drafting Profiles.**
- **2026 Keeper Value Grades:** forward-looking, using projections.
- **2025 Keeper Value Grades:** backward-looking, using actuals.

### History and Replay (Zimmer)

Multi-year ESPN auction history: owner tendencies, tier spreads, bid calibration, draft grading, the optimal-roster result, and a Replay mode that drives the same cockpit components with real historical draft data. These are league-based, not format-based, so they stay valid even though Zimmer drafts snake now.

### Players tab

A full searchable, sortable table with a collapsible **"Where does this data come from?"** legend. Positions a league can't roster are hidden (no kickers for Miami or Zimmer).

---

## The numbers behind it

**WAR (value over replacement)** is league-specific, stored per player as `warByLeague`. Replacement ranks are derived from team count and roster shape (starters plus depth, with streaming buffers), reverse-engineered so Kepners reproduces its original hand-tuned numbers exactly. The same player is worth less in a shallower league: Ja'Marr Chase is +95.9 in Kepners but +74.9 in Miami.

**Keeper Value** (used identically for 2025 actuals and 2026 projections):

```
Keeper Value = Surplus × (WAR ÷ 100)
Surplus      = player WAR − average WAR of same-position players near the keeper-cost round
```

- The WAR multiplier stops a big discount on a mediocre player from outranking a real difference-maker.
- The 100 is a flat constant across positions, so an elite TE can't win just for being near his own shallow ceiling.
- **Value floor:** if WAR ≤ 0, Keeper Value isn't computed, because the formula would flip sign and rank a bench player above starters.
- The peer window tries the exact round first and widens only until the minimum sample is met.
- Tier cutoffs are derived per league (95/82/60/35/12 percentiles), not hardcoded.

**TruRank** compares *positional slots* year over year (what did WR4 cost last year versus this year?) instead of tracking individual players, so rookies and new teams cost nothing.

```
TruRank = priceDrift + 0.5 × talentDrift      (positive = a better value than that slot offered last year)
```

Talent drift separates "the market is mispricing this tier" from "the tier is simply worse this year." It uses WAR *rank* within each year's own pool, so it isn't distorted by projections being compressed and actuals being dispersed. League-specific ADP is used: Yahoo ADP for Kepners and Miami, ESPN ADP for Zimmer.

**Power rankings** (Starters and Bench) are computed in the browser from the same slot data as the lineup comparison. `power_history.py` is a line-for-line port that snapshots them weekly, so a point on the over-time chart is exactly what the board read that week.

---

## How the data gets here

```
                ┌─────────────── ESPN (Zimmer, Inspire11) ───────────────┐
                │   GitHub Action, Tuesdays 13:00 UTC (+ manual runs)    │
                │   rosters · free agents · ROS projections ·            │
                │   waiver/FAAB history · Sleeper trending adds          │
                └──────────────────────────┬─────────────────────────────┘
                                           ▼
iPhone ── "YahooScraper" Shortcut          build_inseason.py ──► inseason_2026.json ──► site
  (Kepners / Miami, one run each)              ▲   ▲                    │
     │ fetch roster pages with your            │   │                    └─► power_history.py
     │ Yahoo cookie (phone's own IP)           │   │                         ► power_rankings_history.json
     ▼                                         │   │
 push raw HTML via GitHub Contents API ─► yahoo_roster_raw/<league>/team_N.html
                                               │   │
                          Parse Yahoo Rosters workflow (auto on push)
                          yahoo_parse_local.py ─► <league>_live_rosters.json
```

**Why a phone bridge?** Yahoo's official API approval stalled (every authenticated call returns `403 "This application is not authorized to perform this action"`, a Yahoo-side provisioning issue). Yahoo also binds session cookies to the originating IP and fingerprint, and GitHub's runners are datacenter IPs that Yahoo flags. So the **phone fetches the page** from its own trusted connection, and **GitHub Actions only parses** what lands in the repo, with no cookie or network access needed. If the API is ever approved, the builder automatically prefers it (see [Known limitations](#known-limitations)).

**Safe by design:**
- Each league parses to a temp file and replaces the real one **only if at least one team parsed**. A bad scrape (expired cookie, wrong URL) can never wipe the last good rosters, and one league failing can't block the other. The run still goes red so it isn't missed.
- The parser classifies every file by positive evidence (roster / login wall / wrong page / unknown) and reports exactly which it was.
- A failed cockpit rebuild never blocks committing parsed rosters.
- All workflows commit with rebase-and-retry, so parallel runs can't fail on a rejected push.

---

# Operations and Support

## Weekly routine

| When | What | Who/what |
|---|---|---|
| **Tuesday, 13:00 UTC** (9am ET in summer, 8am in winter) | `Update In-Season Data` runs automatically: ESPN rosters, free agents, projections, waiver history, Sleeper trends. Rebuilds the cockpit data and takes the weekly power-rankings snapshot. | Scheduled |
| **After waivers clear** | Run the **YahooScraper Shortcut** for Kepners and for Miami (all teams). | You, on the phone |
| *Optional* | Re-run the Shortcuts before Sunday kickoff for fresher injuries and lineups. | You |

After a Shortcut run, the **Parse Yahoo Rosters** workflow starts by itself and **rebuilds the cockpit data**. Give it a minute or two.

**Quick verification:**
1. GitHub → **Actions** → the latest **Parse Yahoo Rosters** run is green.
2. Hard-refresh the site and open the league's card.
3. Each Shortcut team run shows a notification that starts `{"content":{"_links"…` when GitHub accepted the file.

---

## Refresh the Yahoo cookie

**When you need this:** the Yahoo cookie lasts anywhere from days to a few weeks. When it expires, Yahoo returns a sign-in page instead of the roster, and the Shortcut keeps "succeeding" because it faithfully commits whatever Yahoo returned.

**How you'll know it expired:**
- The **Parse Yahoo Rosters** run goes **red** with `LOGIN WALL in team_N.html: sign-in form present -- phone cookie expired at fetch time`, and an error annotation saying the league had no usable teams.
- Your last good data is **kept**: the site doesn't break, it just stops updating.
- The build log may warn `rosters are N days old` if rosters go stale.
- In the Shortcut itself, the Quick Look of the Yahoo fetch shows a sign-in page instead of roster HTML.

> Not the same problem: if the files are a **"Managers"** page rather than a login page, the log says `WRONG PAGE ... URL ends in /teams`. That's a URL typo in the Shortcut (see [Troubleshooting](#troubleshooting)), not an expired cookie.

### Steps (about 5 minutes; needs a desktop browser)

You need a computer with Chrome, Edge or Firefox. The phone can't easily show request headers.

1. **Sign in to Yahoo Fantasy** on the computer, with the same Yahoo account that's in both leagues. Tick **Stay signed in** if offered, and complete any verification prompt Yahoo shows.
2. **Open a roster page**, for example `https://football.fantasysports.yahoo.com/f1/102398/12/team`. Confirm you see your actual roster.
3. **Open developer tools:** press `F12` (Windows) or `Cmd+Option+I` (Mac), then click the **Network** tab.
4. **Reload the page** (`Ctrl/Cmd+R`) so requests appear. Click the **first request in the list**, the page itself (named `team`, since that's the last part of the URL). The status should be `200`. If the list is crowded, filter by **Doc**.
5. In the right-hand pane, open **Headers** and scroll down to **Request Headers** (not *Response Headers*). Find the row named **`Cookie`**.
   - ⚠️ `Set-Cookie` under *Response Headers* is the wrong thing: that's Yahoo setting a cookie, not the one you send.
6. **Copy the full value:** right-click the `Cookie` value and choose **Copy value**.
   - ⚠️ Do **not** select and copy from the screen. The display is visually truncated, and a partial cookie looks like it works but fails on private pages.
   - *Alternative:* right-click the request, then **Copy → Copy as cURL**. Take everything inside the quotes after `-b '` or `-H 'Cookie: `, up to the closing quote.
7. **Sanity-check it:** paste it into a plain text editor. It should be **one very long line** (thousands of characters). A short string made only of tracking cookies such as `tbla_id=…`, `_ga=…` and `cids=…` is the wrong request or an incomplete copy. A good cookie typically also contains short-named cookies such as `A1=`, `A3=`, `T=` or `Y=`.
8. **Get it onto your iPhone unchanged.** Best options:
   - **Universal Clipboard:** copy on the Mac, then paste directly on the iPhone.
   - AirDrop a `.txt` file, or put it in an iCloud Note.
   - Avoid email and Messages, which can wrap or alter long lines.
9. **Paste it into the Shortcut:** Shortcuts → **YahooScraper** → find the **Text** action that holds the cookie (the one whose result is saved as the variable **`CookieString`**, near the top). Tap the text, **Select All**, and **paste**.
   - Make sure it's **a single line with nothing after the last character**. Tap at the very end: if the cursor sits on a new empty line, press backspace once. A stray trailing return or space is the same class of bug that once broke the upload.
10. **Do the same in the Miami copy of the Shortcut.** Cloned Shortcuts each hold their own copy of the cookie.
11. **Test with one team.** Run the Shortcut and enter just `12` (Kepners) or `4` (Miami). Success means:
    - the Shortcut's notification begins `{"content":{"_links"…`, and
    - **Actions → Parse Yahoo Rosters** turns green and the log shows `Parsed team N (Team Name): X players`.
12. Run the full list once the test passes.
13. **Don't sign out of Yahoo in that browser.** Signing out invalidates the session you just copied. Just close the tab.

### If a fresh cookie still shows a login wall

- Confirm you copied from the **page request** (a `200` document on `football.fantasysports.yahoo.com`), not an image or tracker request.
- In Safari on the phone, load the roster URL and confirm you're signed in there. If Yahoo is asking for verification, finish it first.
- Redo the copy after a clean sign-out and sign-in on the computer, then **don't** open other Yahoo sessions before running the Shortcut.
- Yahoo ties cookies to a browser fingerprint and IP. If a desktop-copied cookie keeps getting rejected, try copying it from a browser on the **same Wi-Fi network** as the phone.
- In the Shortcut, add a temporary **Quick Look** after the Yahoo fetch to see whether the response is roster HTML or a sign-in page.

---

## Other credentials that expire

| Credential | Lives in | Symptom when it lapses | How to renew |
|---|---|---|---|
| **GitHub token** (`GHToken`) | A Text action inside **each** Shortcut | Shortcut notification shows `401 "Bad credentials"` (or `404 "Not Found"` if it lost repo access) | GitHub → profile **Settings → Developer settings → Fine-grained tokens**. Create a new token: *Only select repositories → `kepners`*, **Contents: Read and write**, nothing else. Paste it into the `GHToken` Text action in both Shortcuts. Paste only the token. The Shortcut adds the `Bearer ` prefix, and there must be no trailing newline. Fine-grained tokens usually carry an expiry date chosen at creation: put it in your calendar. |
| **`GH_SECRETS_PAT`** | Repo secret | `Run Full Pipeline` or the OAuth/in-season workflows fail with `HTTP 403 "Must have admin rights to Repository"` | A **classic** PAT with **both** `repo` **and** `workflow` scopes (separate checkboxes; `repo` alone isn't enough). Update under **Settings → Secrets and variables → Actions**. |
| **ESPN cookies** (`ESPN_S2`, `ESPN_SWID`) | Repo secrets | ESPN pull steps fail with auth errors, or Zimmer/Inspire11 data stops refreshing | While signed in to ESPN Fantasy in a desktop browser: DevTools → **Application** (Chrome) or **Storage** (Firefox) → **Cookies**, then copy the values of `espn_s2` and `SWID` (SWID includes the curly braces). Update both secrets. |
| **Yahoo API** (`YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_REFRESH_TOKEN`) | Repo secrets | Not in use: the API returns 403 for every call, and the `YAHOO_REFRESH_TOKEN` secret may not exist yet | See [Known limitations](#known-limitations). |

---

## The YahooScraper Shortcut, explained

One Shortcut per Yahoo league (Kepners, Miami). Each does the same thing for the list of team numbers you give it. If you ever need to rebuild one, here's the structure:

1. **Ask for the teams:** a comma-separated list (e.g. `1,2,3,4,5,6,7,8`), no spaces. **Split Text** on a comma, then **Repeat with each item**.
2. **Variables:** the cookie text saved as `CookieString`, and the GitHub token saved as `GHToken`.
3. **Inside the loop**, for each team (`Repeat Item`):
   1. Reset `FileSHA` to empty.
   2. **Get contents of URL** (GET): `https://football.fantasysports.yahoo.com/f1/<LEAGUE_ID>/<team>/team`, with headers `Cookie: CookieString` and an iPhone `User-Agent`. **Note `/team` (singular).** `/teams` returns the league Managers page instead of a roster.
   3. **Base64 Encode** the page, with **Line Breaks set to None**.
   4. **Sha lookup:** GET `https://api.github.com/repos/truckinorcruisin-bit/kepners/contents/yahoo_roster_raw/<league>/team_<team>.html` with `Accept: application/vnd.github.object+json` (needed because the file is over 1 MB), then **Get Dictionary Value** `sha` and save it as `FileSHA`.
   5. **Text action:** the JSON body `{"message":"…","content":"<base64>","sha":"<FileSHA>"}` on a **single line**. A stray return or space inside it breaks the upload.
   6. **Get contents of URL** (PUT) to the same GitHub path, with `Authorization: Bearer GHToken`, request body type **File**, the Text as the file, and header `Content-Type: application/json`.
   7. **Show notification** with GitHub's response, then **Wait** a few seconds.

| League | Yahoo league ID | GitHub folder | Teams |
|---|---|---|---|
| Kepners | `102398` | `yahoo_roster_raw/kepners/` | 1–12 |
| Miami | `391024` | `yahoo_roster_raw/miami/` | 1–8 |

The sha lookup and the PUT **must point at the exact same path**, or GitHub returns a 409.

---

## Troubleshooting

### Parse workflow / site problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Parse run red: `LOGIN WALL in team_N.html` | Yahoo cookie expired | [Refresh the Yahoo cookie](#refresh-the-yahoo-cookie) |
| Parse run red: `WRONG PAGE … URL ends in /teams` | The Shortcut fetches `/teams` (Managers page), not `/team` | Fix the Yahoo URL in the Shortcut's fetch action so it ends in `/team` |
| `PARSE_FAILED=miami` (or kepners) annotation | That league had no usable teams in this scrape | The other league and the last good data are unaffected. See the log lines above it |
| Log: `rosters are N days old` | The Shortcut hasn't run recently | Run the Shortcut |
| Log: `::warning::<league>: couldn't find my team` | `team_id` for that league is wrong or missing | Set `team_id` in `LIVE_LEAGUES` in `build_inseason.py`. The warning lists every team and ID it found |
| A team is missing from a league (e.g. 11 of 12) | That team's file failed to upload, so its players show as "free agents" | Run the Shortcut for just that team number and read the error notification |
| League card says it can't identify your team | Same as `couldn't find my team` | As above |
| Site shows old data | Cache or Pages deploy delay | Hard-refresh, then check Actions for a green **pages build and deployment** |
| League missing from the Manager view | No live-roster file yet, or no ROS projections | Check that `<league>_live_rosters.json` exists, and run **Parse Yahoo Rosters** manually |
| Power-rankings chart says "No weekly history" | No snapshot taken yet | It's written whenever the cockpit data rebuilds. Run **Parse Yahoo Rosters** or **Update In-Season Data** |
| Parse step fails on `git push` | Parallel runs racing | Already retried automatically (5 attempts). Re-run the workflow |

### Shortcut notification errors (GitHub's response)

| Response | Meaning | Fix |
|---|---|---|
| `"content":{"_links"…` | **Success** | None |
| `400 "Problems parsing JSON"` | The JSON body is malformed: stray return, space or curly quote in the Text action | Retype the Text action on one line; check the base64 chip sits between the quotes |
| `422 "content is not valid Base64"` | Line breaks or stray characters in the encoded text | Base64 Encode → **Line Breaks: None**. Check the Text action for an extra return |
| `409 "…does not match <sha>"` | The sha sent isn't the file's current sha | Make the sha-lookup URL identical to the PUT URL (including the folder), keep the `Accept: application/vnd.github.object+json` header, and check for a stray space in the Text action's sha |
| `422 "sha wasn't supplied"` | The file exists but the sha came through empty | Check `Get Dictionary Value sha` feeds `FileSHA` *before* the Text action |
| `401 "Bad credentials"` | GitHub token expired or invalid | [Renew the token](#other-credentials-that-expire) |
| `404 "Not Found"` | Wrong repo or path, or the token can't see the repo | Check the URL and the token's repository access |

---

## Health check (60 seconds)

- **Actions tab:** recent runs of *Parse Yahoo Rosters* and *Update In-Season Data* are green.
- **`inseason_2026.json` → `generated`:** timestamp is recent.
- **`kepners_live_rosters.json` / `miami_live_rosters.json` → `fetched_at`:** shows when the phone last scraped each league (this is the newest scrape commit time, not the parse time).
- **Manager view:** all four league cards appear and each shows your team.
- **`yahoo_roster_raw/<league>/`:** one `team_N.html` per team, each roughly 0.8–1 MB. A much smaller file means a login page was committed.

---

## Common changes

**Add or clone a Shortcut for another Yahoo league.** In the copy, change: the league ID in the Yahoo URL, the folder in **both** GitHub URLs, the commit message, and the team list. Then add the league to the `for lg in kepners miami` loop **and** the commit step's file list in `yahoo-parse-rosters.yml`, and to `LIVE_LEAGUES` in `build_inseason.py` (with your `team_id`).

**Change which team is yours** (new team number or renamed team): edit `team_id` and `team_name` for that league in `LIVE_LEAGUES` in `build_inseason.py`. The ID is the primary match; the name is only a fallback.

**Start a new season:** update `league_rules.json`; upload the new Big Board to `data/`; run **Run Full Pipeline** (or the individual preseason workflows in order); change the default year (`'2026'`) in `update-inseason.yml` (and in the `build_inseason.py 2026` call in `yahoo-parse-rosters.yml`); refresh the roster snapshots used for keeper prediction.

**Show manager names for a Yahoo league:** add a `{Yahoo team label: {manager}}` file (see `kepners_team_aliases.json`). It's optional.

---

## Workflow reference

All run from **Actions → (workflow) → Run workflow** unless noted.

| Workflow | Trigger | What it does |
|---|---|---|
| **Update In-Season Data** | **Tuesdays 13:00 UTC** + manual | ESPN league state, ROS projections, waiver history, Sleeper trends → rebuilds `inseason_2026.json` and the weekly power snapshot |
| **Parse Yahoo Rosters** | **Auto on push** to `yahoo_roster_raw/**` + manual | Parses each league's HTML into `<league>_live_rosters.json`, rebuilds the cockpit data, commits |
| **Run Full Pipeline** | Manual | Preseason orchestrator: triggers the workflows below in dependency order (needs `GH_SECRETS_PAT`) |
| Update Big Board Data | Manual | Excel → `bigboard.json` |
| Update Player Values | Manual | ESPN projections, auction values, byes |
| Update Season Stats / Weekly Stats | Manual | Replacement-level and variance data |
| Update Kepners Draft Order | Manual | Google Sheet → draft order and keepers |
| Update Kepners Draft History | Manual | Yahoo API history (needs the Yahoo API; expected to fail until it's approved) |
| Update Kepners Analysis / Draft Grades | Manual | Owner profiles and keeper grading |
| Update Zimmer Draft History / Draft Grades | Manual | ESPN auction history and grading |
| Update Bid Calibration | Manual | WAR → $ auction curve |
| Run Roster Optimizer | Manual | Optimal Zimmer roster under budget |
| Update Live Draft | Manual | ESPN live draft pull (Zimmer button) |
| Parse Kepners Draft Results | Manual | `.txt` export → draft history. (Miami's export is parsed by running `parse_miami_draft_txt.py` on `Miami_Draft_Results.txt`; it has no workflow.) |
| Yahoo OAuth Token Exchange | Manual | One-time OAuth handshake, writes the refresh token to a secret |
| `yahoo-cookie-test`, `yahoo-direct-league-test`, `yahoo-identity-check` | Manual | **Diagnostics only**; safe to ignore |

**Preseason order that matters** (this is what *Run Full Pipeline* encodes, in parallel waves): *Update Player Values* → *Update Big Board Data* → *Bid Calibration / Zimmer Draft Grades* → *Run Roster Optimizer*. Player Values must run first because `warByLeague` has to exist before the Big Board build can compute TruRank's talent component, and the optimizer needs the WAR and bid fields.

---

## Repo layout

```
index.html                       The entire site (inline CSS/JS)
README.md                        This file
league_rules.json                Hand-maintained league rules (source of truth)
bigboard.json                    Generated player + league data the draft tools load
data/FY26_BigBoard.xlsx          Big Board source workbook

── In-season (Manager view) ──
inseason_2026.json               Merged rosters, free agents, projections, positional strength
build_inseason.py                Builds the above; LIVE_LEAGUES config lives here
espn_inseason_pull.py            ESPN league state (Zimmer + Inspire11)
espn_ros_projections.py          Rest-of-season projections (the global pool)
espn_waiver_history.py           ESPN claims, budgets → FAAB assistant
sleeper_trending.py              Sleeper trending adds → FAAB "heat"
player_ros_2026.json, espn_inseason_2026.json, waiver_history_*.json, sleeper_trending.json
power_history.py                 Weekly power-ranking snapshots
power_rankings_history.json      One entry per league per NFL week

── Yahoo phone bridge ──
yahoo_roster_raw/kepners/        Raw roster HTML pushed by the Shortcut (team_N.html)
yahoo_roster_raw/miami/
yahoo_parse_local.py             HTML → <league>_live_rosters.json
kepners_live_rosters.json, miami_live_rosters.json
kepners_team_aliases.json        Yahoo team label → manager (optional)
yahoo_scrape.py                  Legacy cookie scraper (blocked from GitHub's IPs; reference only)
yahoo_inseason_pull.py, yahoo_setup.py, yahoo_kepners_history.py   Yahoo API path (not active)

── Draft, keepers, history ──
convert_bigboard.py              Excel → bigboard.json (also home of normalize_name, bye map, replacement ranks)
market_drift.py                  Slot-based market drift + TruRank
kepners_*.py / kepners_*.json    Draft order sync, analysis, grades, history, keeper prediction
parse_kepners_draft_txt.py, parse_miami_draft_txt.py, predict_*_keepers.py, miami_*.json
Miami_Draft_Results.txt          Raw Miami draft export (input to the parser)
espn_zimmer_history.py, zimmer_*.py/json, bid_calibration.py, roster_optimizer.py
espn_player_values.py, espn_season_stats.py, espn_weekly_stats.py, espn_live_draft_pull.py

.github/workflows/               All automation (see Workflow reference)
.github/scripts/trigger-and-wait.sh   Used by Run Full Pipeline (invoked with `bash`; web-uploaded files can't be made executable)
```

---

## Design notes and lessons learned

**Data and metrics**
- **Floor before multiplying.** Multiplying by a signed quantity can invert rankings. The keeper formula flips sign on negative WAR, so floor first.
- **Try the exact bucket before widening** peer windows, or edge cases get diluted by much weaker neighbors.
- **A floor must not collapse distinct entries to one sort key.** Use a tiered `(bucket, magnitude)` key.
- **Verify implausible metrics against real data**, and equally don't assume an odd number is fine. Both directions need checking.
- **Excel is messy.** Duplicate rows and trailing scratch lists exist; use data-driven boundaries, never row numbers. Position codes are non-canonical (`DE`, `K-`), so normalize them. Names need suffix handling ("Kenneth Walker III" vs "Kenneth Walker", "Texans D/ST").
- **`isRookie()` only tags 2026 rookies.** A bare "Rookie…" note means 2026; "2025 Rookie…" is the prior class.
- **`resolvedKeepers(t)`** is the single helper for keepers (live-synced first, otherwise `confirmed:true` suggestions). Route everything through it.
- **Isolate draft state per league**, and route `ptDraftedMap()` through `resolvedKeepers()`.

**UI**
- **CSS grid tracks must equal the DOM child count.** A resizer is itself a grid child. Put new panels outside the grid.
- **`var(--x)fr` is invalid CSS.** Use static `fr` values and resize by rewriting `grid-template-columns` from JS.
- **Automated tests validate syntax, not layout.** jsdom and linters can't see a visually broken page. Always eyeball changes in a real browser.

**Pipeline**
- **Lazy-import heavy dependencies** (e.g. `openpyxl`) inside the function that needs them.
- **`>>` redirects swallow errors.** Use `tee` and preserve exit codes with `PIPESTATUS`.
- **Parallel workflows pushing at once collide** on the branch tip even when touching different files. All workflows retry with rebase.
- **A `.gitignore` must have the leading dot**: a file named `gitignore` is inert.
- **GitHub's web editor can't set the executable bit**, and typing a path into "Create new file" inside a subfolder nests it there.
- **Verify edits landed.** After publishing, re-fetch `index.html` from `raw.githubusercontent.com` before running workflows. Parallel sessions have silently overwritten edits before.

**iOS Shortcuts**
- The JSON-body field silently truncates very large base64 values. Build the body as a raw **Text** action and send it as a **File** body with an explicit `Content-Type: application/json`.
- Base64 line breaks, and any stray return or space inside the Text action, break the upload.
- Yahoo session cookies are IP- and fingerprint-bound, which is why the fetch happens on the phone.

---

## Known limitations

- **Yahoo API is still blocked.** Every authenticated call returns 403; a documented bug report to Yahoo support was in progress. If it's ever provisioned: run the **Yahoo OAuth Token Exchange** workflow (or `yahoo_setup.py`) to create `YAHOO_REFRESH_TOKEN`, and the builder will automatically prefer API data over the phone bridge for any league the API covers. Until then, `Update Kepners Draft History` is expected to fail.
- **Yahoo leagues have derived free-agent pools**, no record or standings, and no FAAB claim history (so no FAAB bid numbers).
- **Snake Draft Log is session-only.** A reload mid-draft loses it.
- **Power-rankings history can't be backfilled.** It started in Week 4.
- **Keepers:** `kepners_draft_grades.py` covers one season (2025), so it's directional. One TE (Trey McBride) is ungradeable at a round-1 cost because no peer pool exists; judge him manually. Four players with `avgRank` of `#N/A` are silently dropped from peer pools. The *Peer Avg WAR* column compares against the **keeper-cost** round, not the player's merit round, which has caused confusion.
- **Big Board has 234 players**, not ~300: 66 names exist only as blank scratch entries (believed intentional).
- **ESPN has no projection for kickers**, so K rows show as unvalued.

---

## Security notes

- The repo and site are **public**. Never commit tokens, cookies, or `yahoo_token.json` (it's in `.gitignore`).
- The Yahoo cookie and the GitHub token are stored **in plain text inside the Shortcuts** on your phone. Treat the phone and any Shortcut share link accordingly; never share a Shortcut that contains them.
- Use the narrowest GitHub token possible: fine-grained, `kepners` only, Contents read/write.
- Raw roster HTML in `yahoo_roster_raw/` is committed to a public repo. An earlier Shortcut mistakenly fetched the league **Managers** page, which includes member email addresses. Those commits remain in git history even though the folder now holds roster pages. If that matters, make the repo private or purge that history.
- Revoke and replace any token you suspect has leaked, then update it everywhere it's used (both Shortcuts and any repo secret).
