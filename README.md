# Nawy Epic Budget Monitor — automated pipeline

Fully automated: a GitHub Action pulls fresh data from Jira on a schedule,
commits it into this repo, and a GitHub Pages dashboard displays it. Once
set up, nobody needs to touch this again — it just stays current.

## One-time setup (~10 minutes)

### 1. Create the repo
Push this folder to a new GitHub repo, e.g. `nawy/epic-budget-monitor`.

### 2. Add secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `JIRA_SITE` | `https://nawy.atlassian.net` |
| `JIRA_EMAIL` | the email tied to your Jira API token |
| `JIRA_API_TOKEN` | generate one at https://id.atlassian.com/manage-profile/security/api-tokens |

Optional — in **Settings → Secrets and variables → Actions → Variables**:

| Name | Value |
|---|---|
| `RATE_PER_DAY_EGP` | `3500` (defaults to this if not set) |

### 3. Enable GitHub Pages
**Settings → Pages** → Source: `Deploy from a branch` → Branch: `main`, folder: `/docs`.
Your dashboard will be live at `https://<org>.github.io/<repo>/`.

### 4. Choose which epics to track
Edit `scripts/epics.json` — it's a plain JSON array of epic keys:
```json
["SC-377"]
```
Add more epic keys here anytime to track additional epics. No code changes needed.

### 5. Run it once manually
**Actions tab → "Update Epic Budget Data" → Run workflow** (the `workflow_dispatch`
trigger lets you do this on demand, instead of waiting for the daily 06:00 UTC cron).

That's it — check the dashboard URL. It should show live data within a minute
of the workflow finishing.

## How it stays "zero effort" going forward
- The workflow re-runs automatically every day (edit the cron schedule in
  `.github/workflows/update-budget.yml` if you want a different cadence).
- Each run overwrites `data/<EPIC>.json` and `data/index.json` and commits
  the change — the dashboard picks it up on next page load, no rebuild step.
- Adding a new epic to track = one line in `scripts/epics.json`, commit, done.
- Sync is **incremental**: only tickets Jira reports as changed since the last
  run get re-fetched (fast — usually under a minute). A full rebuild still
  runs automatically every Sunday as a safety net, and can be forced anytime
  via the Actions tab ("Run workflow" → check "Force a full rebuild").

## "Refresh from Jira" button (on-demand refresh)
The dashboard has a Refresh button that triggers the same GitHub Action
on demand, instead of waiting for the daily schedule.

**No token is stored in this repo** — GitHub's push protection actively
blocks committing one (public repo), and that's the correct behavior. Instead,
the first time anyone clicks Refresh, the page asks them to paste a token —
it's saved only in *their own browser's* localStorage and sent directly from
their browser to GitHub's API. It never touches this repo or any server.

To use it, each person needs their own **Fine-grained personal access token**:
1. https://github.com/settings/personal-access-tokens/new
2. Repository access → **Only select repositories** → this repo
3. Permissions → Repository permissions → **Actions: Read and write**
4. Leave every other permission as **No access**
5. Generate, copy, paste it into the browser prompt when clicking Refresh

That scope means the token can only start/cancel workflow runs on this one
repo — it cannot read code, secrets, or anything else, even if it leaked.

## "Refresh from Jira" button (on-demand refresh)
The dashboard has a Refresh button that triggers the same GitHub Action
on demand, instead of waiting for the daily schedule — works instantly for
anyone who opens the page, no login or setup required on their end.

**Why this needs a Cloudflare Worker:** the button needs a GitHub token to
trigger a workflow run. Putting that token directly in the dashboard's HTML
doesn't work — this is a public repo, and GitHub's secret scanning **automatically
revokes any real GitHub token it detects there**, even if you manually approve
the push past push-protection. It keeps working right up until the commit
lands in the public repo, then dies. (Learned this the hard way — see git
history for the two dead-end attempts.)

The fix: a tiny serverless proxy (Cloudflare Workers, free tier) holds the
token server-side, where GitHub's public-repo scanning can't see it at all.
The dashboard calls the Worker; the Worker calls GitHub.

### One-time setup (~5 minutes, no CLI needed)
1. Go to https://workers.cloudflare.com and sign up / log in (free tier is enough)
2. **Create Worker** → give it any name (e.g. `nawy-budget-refresh`)
3. Open the online code editor, delete the default code, paste in the
   contents of `cloudflare-worker/worker.js` from this repo
4. **Deploy**
5. Worker → **Settings → Variables and Secrets** → **Add** →
   - Type: **Secret**
   - Name: `GITHUB_TOKEN`
   - Value: a Fine-grained PAT — https://github.com/settings/personal-access-tokens/new
     → Repository access: only this repo → Permissions → Actions: **Read and write**
     → generate, paste the value here
   - Save (this deploys the secret — the token is never visible again in the UI or anywhere else)
6. Copy the Worker's URL (shown on its overview page, looks like
   `https://nawy-budget-refresh.<your-subdomain>.workers.dev`)
7. Edit `docs/index.html`, find the line:
   ```js
   const WORKER_URL = 'https://YOUR-WORKER-SUBDOMAIN.workers.dev';
   ```
   replace with your actual Worker URL, commit, push.

That's it — the Refresh button will work for anyone, and the only place the
real token exists is that one Cloudflare secret field, which nobody (including
this repo, GitHub, or any browser) can read back out.

## Files
- `scripts/jira_epic_budget.py` — the fetch + calculation engine
- `scripts/epics.json` — list of epic keys to track
- `.github/workflows/update-budget.yml` — the scheduler
- `docs/index.html` — the dashboard (served via GitHub Pages)
- `data/` — auto-generated, do not edit by hand

## Methodology (for reference)
- Cycle time = first "In Progress"-like status → first "Done/Released/Closed/
  Ready for Production"-like status (fuzzy-matched, so it survives different
  project workflows)
- Rework: if a ticket re-enters "In Progress" after being marked done, that
  time is added to the total (summed across all spans)
- Excluded from the count: time in "On Hold", "Product UAT", "Ready for UAT"
  (however that status happens to be spelled in a given project)
- Unit: business days — Friday & Saturday weekend (Egypt), plus a
  configurable public-holiday list inside the script
- In-progress tickets count partial days consumed up to "now", not just
  finished tickets — so the epic total reflects budget spent *so far*, live
