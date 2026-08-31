# Deploying Signal Ledger

Three accounts needed, all free: **GitHub** (source), **Render** (hosting),
and **cron-job.org** (daily trigger) -- plus your existing **email
account** for the SMTP digest. Everything (strategy parameters, cash,
positions, CSV import) is now editable from the dashboard itself; Render
only needs touching to ship a code change.

## 0. Before you push: build the frontend

Render's build step only runs `pip install` -- it does not run Node. So
the frontend has to be built locally and its output committed:

```
cd live_app/frontend
npm install
npm run build
```

This writes into `live_app/static/`, which is what Flask actually serves.
Commit that folder along with your other changes. See
`live_app/frontend/README.md` for more on the frontend project. If you
only changed Python, you can skip this step.

## 1. Push this repo to GitHub

Repo: **https://github.com/juice3000app/Investment-Tracker**. Push
everything in this folder there (private is fine).

```
git add -A
git commit -m "Deploy Signal Ledger"
git push origin main
```

## 2. Create the Render web service

1. Sign up at render.com (free).
2. New -> Blueprint -> point it at the GitHub repo above. Render reads
   `render.yaml` and creates a free web service named `signal-ledger`.
3. Wait for the first deploy to finish -- you'll get a URL like
   `https://signal-ledger.onrender.com`.

**Free-tier behavior worth knowing:**
- The service sleeps after ~15 minutes of no traffic and takes 30-60
  seconds to wake on the next request (checking the dashboard from your
  phone after a while means one slow load, then it's fast).
- **Every sleep/wake cycle wipes the on-disk SQLite state file, not just a
  redeploy** -- Render's free tier has no persistent disk, and waking up
  from idle boots a genuinely fresh container. For a dashboard you check a
  few times a day, this happens constantly, not just when you ship a code
  change. Set up **GitHub auto-backup** (step 3a below) so this heals
  itself automatically instead of silently losing data between checks.

## 3. Set environment variables (in Render -> your service -> Environment)

These are the only settings still controlled outside the dashboard --
everything strategy-related (parameters, which strategies are enabled,
cash, positions) is edited through the app itself now, under the
Strategies tab.

| Variable | Value | Notes |
|---|---|---|
| `DASHBOARD_USER` | pick a username | protects the whole app -- it shows your real positions |
| `DASHBOARD_PASSWORD` | pick a password | same |
| `CRON_SECRET` | a long random string | protects `/run-daily-job` from being triggered by strangers |
| `SMTP_HOST` | e.g. `smtp.gmail.com` | see step 4 |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | your email address | the sending mailbox |
| `SMTP_APP_PASSWORD` | an app password, NOT your real password | see step 4 |
| `DIGEST_TO_EMAIL` | where the digest should land | can be the same address as `SMTP_USER` |
| `LIVE_UNIVERSE_MODE` | `index` (default) or `auto_sweep` | which universe the forward scanner sweeps |
| `GITHUB_BACKUP_TOKEN` | a GitHub fine-grained personal access token | see step 3a -- optional, but strongly recommended on the free plan |
| `GITHUB_BACKUP_REPO` | `juice3000app/Investment-Tracker` | the repo the backup file gets written to |
| `GITHUB_BACKUP_PATH` | `backups/state-backup.json` | any path in that repo -- created automatically on first backup |
| `GITHUB_BACKUP_BRANCH` | `main` | optional, defaults to `main` if unset |

Redeploy after setting these (Render does this automatically on save).

## 3a. Set up automatic GitHub backup (recommended)

This is what closes the "every sleep/wake cycle wipes the database" gap
above, for free, with no action needed from you day to day: after every
daily job run (and every manual "Refresh now"), the app pushes a full
state backup to a JSON file in your own GitHub repo. If the server ever
wakes up into a genuinely empty database, it automatically pulls that
backup back down before serving any requests -- so a cold start after an
idle period restores your positions, cash, settings, and history instead
of showing zeros.

1. On GitHub, go to **Settings -> Developer settings -> Personal access
   tokens -> Fine-grained tokens -> Generate new token**.
2. Give it a name (e.g. "signal-ledger-backup"), set **Resource owner** to
   your account, and under **Repository access** pick **Only select
   repositories** -> `Investment-Tracker`.
3. Under **Permissions -> Repository permissions**, set **Contents** to
   **Read and write**. Leave everything else at "No access".
4. Generate the token and copy it -- GitHub only shows it once.
5. In Render, set the four `GITHUB_BACKUP_*` variables from the table
   above (`GITHUB_BACKUP_TOKEN` is the token you just copied).
6. Redeploy (or wait for Render to auto-redeploy after saving env vars).

That's it -- no dashboard button to click, nothing to remember before a
redeploy. The manual export/import flow in step 6 still exists as a
belt-and-suspenders option (e.g. to keep an offline copy, or to migrate
data by hand), but with this set up you shouldn't need it for routine use.

## 4. Get an SMTP app password (Gmail example)

Any provider works; Gmail is the common case:

1. Turn on 2-Step Verification on the Google account, if it isn't already:
   myaccount.google.com/security
2. Go to myaccount.google.com/apppasswords, create an app password (name
   it anything, e.g. "signal ledger digest").
3. Use the 16-character password it gives you as `SMTP_APP_PASSWORD`
   (spaces don't matter). `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`.

Outlook/Office365: `SMTP_HOST=smtp.office365.com`, `SMTP_PORT=587`, similar
app-password flow under account security settings.

## 5. Set up the daily trigger (cron-job.org)

Render's free tier doesn't include a reliable free background scheduler, so
an external free "hit this URL once a day" service drives the daily job --
it calls the SAME running web service, so it doesn't need its own copy of
the state file.

1. Sign up at cron-job.org (free).
2. Create a new cron job:
   - URL: `https://<your-render-url>/run-daily-job?token=<your CRON_SECRET>`
   - Schedule: **daily, 5:00 PM**, timezone **America/Halifax** (cron-job.org
     supports picking a real IANA timezone directly, so this stays correct
     across daylight-saving changes -- no manual UTC-offset math needed).
3. Save. The job will start firing after the TSX's 4:00pm ET close.

You can trigger it manually any time from cron-job.org's dashboard, or by
opening that URL directly, to test before waiting for the schedule.

## 6. Backup: manual export/import (optional if you set up step 3a)

If you set up GitHub auto-backup in step 3a, this manual flow is a
belt-and-suspenders option, not a required routine -- the app backs
itself up and restores itself automatically. Keep reading if you want an
offline copy, want to migrate data by hand, or skipped step 3a. From the
dashboard's **Strategies** tab, under **Backup**:

- **Before you push a code change:** click **Export backup**. It downloads
  one JSON file containing every position, the cash ledger, strategy
  settings, the idle-sweep state, snapshots, and the decision log.
- **After the redeploy finishes:** open the (now-empty) dashboard and click
  **Restore backup**, pick the file you just exported. This restores
  everything in one shot.

Treat this like a save file: keep the last few exports somewhere durable
(not just your Downloads folder) in case a redeploy happens before you
remember to export. Skipping this step before a redeploy means losing
whatever's live -- positions, cash, and any settings tweaks you'd made.

## 7. Verify

- Open your dashboard URL, log in with `DASHBOARD_USER`/`DASHBOARD_PASSWORD`.
- Add a test position through "Record a purchase".
- Click "Refresh now" -- confirms live data fetching + the decision logic
  work end to end (no email sent).
- Trigger `/run-daily-job?token=...` once manually -- confirms the digest
  email arrives.
- Do one export -> (pretend-redeploy by clearing the position you added) ->
  import cycle to confirm the backup actually round-trips before you rely
  on it for real.

## Known limitations, by design

- Free Render disk isn't durable across redeploys, or across any
  sleep/wake cycle -- see step 3a (automatic) and step 6 (manual).
- The forward scanner's universe (`LIVE_UNIVERSE_MODE`) is either the S&P/TSX
  Composite snapshot or the bundled ticker list for auto-sweep -- same
  limitations as the backtester's `data_sources.py`, described there.
- Advisory only: nothing here places a trade. You act on the digest/dashboard
  yourself and record what you did through "Record a purchase", or via CSV
  import (review-and-confirm -- nothing is applied until you tick it and
  confirm).
- Three settings sliders (entry lead time, dip check delay, sell-the-spike
  exit lead hours) are shown but disabled -- the engine's timing for those
  is currently fixed (C-1 entry, C+1 dip trigger, whole-day spike exit) and
  isn't wired to be adjustable yet. Each has a caption explaining why.
