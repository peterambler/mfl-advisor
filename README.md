# MFL Advisor — setup

Pulls your MyFantasyLeague team's state on a schedule and commits it as JSON,
so Claude can read it without needing your computer.

Roughly 20 minutes, once.

---

## Before you start: the privacy tradeoff

**The repo needs to be public** for Claude's cloud session to read the data
files. That means your roster, transactions, and league standings are visible
to anyone who finds the repo. For a 12-team home league this is usually a
non-issue — your leaguemates already see all of it, and nobody is going to
find the repo — but it is worth deciding on purpose rather than by accident.

Your **API key never goes in the repo**. It lives in GitHub Secrets, which
are encrypted and are not visible in logs or to anyone browsing the code.

If public bothers you, say so and we'll do a private repo instead — that
route needs a personal access token handed over at the start of each session,
which is more friction but keeps everything closed.

---

## 1. Get your league ID

Log into MFL and look at the URL of your league home page:

```
https://www45.myfantasyleague.com/2026/home/57505
                 ^^                        ^^^^^
                 host                      league ID
```

The number at the end is your **league ID**. Note the host number too
(`www45` above) — you probably won't need it, but it's useful if the default
API host gives you trouble.

## 2. Get your franchise ID

On your league's home page, click your own team. The URL will contain
`F=0003` or similar. That four-digit string is your **franchise ID**. It's
how I know which roster is yours.

## 3. Get an API key

In MFL, go to **Setup → Developer API Key** (it's under your league's setup
menu; on some leagues it's under your account settings instead). Request a
key for the current season. It's free and usually issued immediately.

This is a read key — it grants access to league data, not the ability to
change anything. Even so, treat it like a password.

*If your league's data is fully public you can skip this and leave the secret
blank; the script works without it, just with less detail.*

## 4. Create the repo

On GitHub, create a new **public** repository — `mfl-advisor` is a fine name.
Upload these three files, preserving the folder structure:

```
mfl_fetch.py
test_offline.py
.github/workflows/fetch.yml
```

The web uploader handles the nested path fine: drag them in and GitHub creates
`.github/workflows/` for you, or use "Add file → Create new file" and type
`.github/workflows/fetch.yml` as the filename.

## 5. Add your settings

In the repo, go to **Settings → Secrets and variables → Actions**.

Under the **Secrets** tab, add:

| Name | Value |
|---|---|
| `MFL_LEAGUE_ID` | your league ID from step 1 |
| `MFL_API_KEY` | your API key from step 3 (skip if you don't have one) |

Under the **Variables** tab, add:

| Name | Value |
|---|---|
| `MFL_FRANCHISE` | your franchise ID from step 2 |
| `MFL_YEAR` | `2026` |
| `MFL_CONTACT` | your email — MFL likes API clients to be identifiable |

## 6. Run it once by hand

Go to the **Actions** tab. If GitHub asks you to enable workflows, do that.
Pick **Fetch MFL league state** in the sidebar, then **Run workflow**.

Give it a minute. When it's green, a `data/` folder should appear in your
repo with about fifteen JSON files in it.

If it's red, open the run and read the log — the script names the endpoint
that failed and why. Send me the log and I'll sort it out.

## 7. Hand me the URL

Tell me your GitHub username and repo name, or just paste a link to one of
the files in `data/`. From then on I can read your league state whenever we
talk, and the scheduled runs keep it current:

- **Tuesday 6am PT** — waiver window
- **Wednesday 6am PT** — after claims process
- **Thursday 2pm PT** — before Thursday Night Football
- **Sunday 6am PT** — before the early lineup lock

---

## What gets collected

| File | What's in it |
|---|---|
| `league.json` | settings, scoring, roster requirements, franchise names |
| `rosters.json` | every team's roster |
| `standings.json` | records, points for/against |
| `schedule.json` | this week's matchups |
| `live_scoring.json` | in-progress scores |
| `projected_scores.json` | MFL's projections for the week |
| `player_scores.json` | season-to-date scoring |
| `free_agents.json` | the waiver wire |
| `injuries.json` | injury designations |
| `transactions.json` | recent adds, drops, trades across the league |
| `trade_bait.json` | what other owners are shopping |
| `pending_trades.json` | offers on the table |
| `adp.json` | average draft position, for value context |
| `players.json` | the player dictionary (refreshed weekly — it's large) |
| `manifest.json` | index: when it ran, what week, what failed |

## Running it locally instead

```bash
export MFL_LEAGUE_ID=57505
export MFL_API_KEY=your-key-here
export MFL_FRANCHISE=0003
python3 mfl_fetch.py
```

## Checking the script without hitting MFL

```bash
python3 test_offline.py
```

Runs the fetcher against mocked responses — verifies week detection, error
handling, and output shape. Useful if you change something.

## Notes

- Cron times are UTC and set for Pacific Daylight Time. After the November
  time change each run happens an hour earlier in local time; bump the hours
  in `fetch.yml` by 1 if that matters.
- The week in play is detected from the NFL schedule's kickoff times, so
  there's no season-start date to keep updated. Override with `MFL_WEEK` if
  you ever need to.
- A single failed endpoint doesn't kill the run — you get a partial snapshot
  and `manifest.json` records what was missing.
- GitHub disables scheduled workflows in repos with no activity for 60 days.
  In-season that won't come up; it might over the summer.
