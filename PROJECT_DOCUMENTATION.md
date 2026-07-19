# Nawy Epic Budget Monitor — Project Documentation

Last updated: July 19, 2026
Status: **Live and working** (end-to-end verified, including back-to-back manual refreshes)

---

## 1. What this is

A fully automated dashboard that tracks **how many net business days and how much money (EGP) every Jira Epic has consumed**, across all of Nawy's engineering projects — with zero manual time logging. Effort is inferred entirely from Jira status-transition timestamps.

**Live dashboard:** https://ahmedmatbouly-spec.github.io/nawy-epic-budget/

---

## 2. Architecture

```
┌──────────────┐   daily cron /    ┌────────────────────┐   commits    ┌──────────────┐
│GitHub Actions│◄─ manual button ──│  Python script      │─ data JSON ─►│ GitHub repo   │
│ (scheduler)  │                   │ jira_epic_budget.py │              │  /data/*.json │
└──────────────┘                   │  (calls Jira API)   │              └──────┬───────┘
                                   └────────────────────┘                      │
        ┌───────────────────────┐                                              │ serves
        │  Cloudflare Worker     │◄── Refresh button + fresh-data reads ──┐    ▼
        │ (proxy, holds GitHub   │                                        │ ┌──────────────┐
        │  token as secret)      │───► triggers GitHub Action             └─│  Dashboard    │
        └───────────────────────┘                                          │ (GitHub Pages)│
                                                                            └──────────────┘
```

### Components & locations

| Component | Where | Notes |
|---|---|---|
| Git repo (everything lives here) | `github.com/ahmedmatbouly-spec/nawy-epic-budget` | **Public** repo |
| Calculation engine | `scripts/jira_epic_budget.py` | Python, stdlib only |
| Tracked projects config | `scripts/epics.json` | `{"projects": [...], "epics": [...]}` |
| Scheduler | `.github/workflows/update-budget.yml` | GitHub Actions |
| Dashboard page | `docs/index.html` | Single-file HTML/JS/CSS, served by GitHub Pages from `/docs` |
| Refresh proxy | `cloudflare-worker/index.js` | Cloudflare Worker |
| Worker URL | `https://nawy-epic-budget.ahmed-matbouly.workers.dev` | Check `/version` to see deployed build |
| Generated data | `data/<EPIC_KEY>.json` + `data/index.json` | Auto-committed by the workflow; never edit by hand |
| Sync watermark | `data/sync_state.json` | Drives incremental sync |
| Debug log | `debug/last_run.log` | stdout of last workflow run (readable even when GitHub's log storage isn't) |

### Credentials

| Secret | Where stored | Purpose |
|---|---|---|
| `JIRA_SITE` = `https://nawy.atlassian.net` | GitHub repo → Settings → Secrets → Actions | Jira REST auth |
| `JIRA_EMAIL` = `ahmed.matbouly@nawy.com` | same | |
| `JIRA_API_TOKEN` | same | Rotate at https://id.atlassian.com/manage-profile/security/api-tokens |
| `GITHUB_TOKEN` | Cloudflare Worker → Settings → **Variables and Secrets** (the *runtime* one near the top, NOT "Build environment variables") | Fine-grained PAT, scoped to this repo only, permission: **Actions: Read and write** only. Lets the Worker trigger workflow runs. |
| Optional var `RATE_PER_DAY_EGP` | GitHub repo → Settings → Variables | Defaults to `3500` if unset |

---

## 3. Calculation methodology (the business logic)

For every ticket under every tracked epic:

- **Start** = first transition into a status matching `in progress` (regex, case-insensitive — catches "In Progress*" etc.)
- **End** = first subsequent transition into a status matching `done | released | closed | resolved | deployed | ready for deployment | ready for production` (catches "Done ✅" etc.)
- **Rework counts**: if a ticket re-enters In Progress after a done-like status, a new span starts; all spans are **summed**.
- **Still-open tickets count**: if a ticket never reached a done-like status, days are counted from Start up to **now** (Cairo wall-clock) — so epic totals reflect budget consumed *so far*, live.
- **Excluded time** (subtracted from spans): any time in statuses matching `on hold | product uat | ready for uat / read for uat | uat | to do | backlog`.
  - "To Do/Backlog" exclusion only matters mid-span (a ticket demoted back to backlog mid-work); time before the first In Progress was never counted anyway.
- **Unit = business days**: weekend is **Friday & Saturday** (Egypt); Egyptian public holidays are excluded via a hardcoded list in the script (`EGYPT_HOLIDAYS`, covers 2024–2026 — **Islamic-calendar dates are approximate and shift yearly; review this list periodically**).
- **Partial days** are prorated against a 9am–5pm (8-hour) workday.
- **Cost** = net days × rate (default **3,500 EGP/day**, editable live in the dashboard, and via `RATE_PER_DAY_EGP`).
- **Status matching is pattern-based, not exact-name** — deliberately, because different Nawy projects spell the same concept differently (e.g. the "Read for UAT" typo in SC).

What this deliberately does NOT do: manual time logging, per-person cost split for parallel assignees, or prorating a ticket's cost across the quarters it was worked (the time filter uses **epic creation date**).

---

## 4. Scope & schedule

- **Projects tracked** (`scripts/epics.json`): `SC, NCRM, PAR, PSQ, IMS, NLP, CM, SP, ER` → ~313 epics auto-discovered (every Epic-type issue in each project). Add a project key to the list to expand; individual extra epics can go in the `"epics"` array.
- **Daily incremental sync**: 06:00 UTC (08:00 Cairo). Only re-fetches tickets Jira reports as updated since last sync → typically finishes in **15–60 seconds**.
- **Weekly full rebuild**: Sundays 03:00 UTC — re-scans everything from scratch (~20–30 minutes). Safety net for anything incremental sync can miss. GitHub often delivers scheduled runs **late** (sometimes hours).
- **Manual refresh**: the dashboard's "↻ Refresh from Jira" button (via the Cloudflare Worker) or the repo's Actions tab → "Run workflow" (which also offers a "Force a full rebuild" checkbox).
- Concurrency: overlapping runs **queue** (never race) — enforced by a `concurrency` group in the workflow.

---

## 5. Dashboard features

- Nawy-branded dark navy→teal glassmorphic theme (matched to the internal Sprint Review dashboard), Inter + JetBrains Mono fonts.
- Project cards with colored dots → epic dropdown (sorted by cost) per project.
- KPI cards per epic: net business days, cost (EGP), completion ratio, avg days/ticket; sortable per-ticket table with Done/In-progress badges.
- **Rate input**: recalculates costs live (display-only; doesn't change stored data).
- **Time filter**: Year + Quarter dropdowns (filters by **epic creation date**); shows a portfolio-total banner for the selected period.
- **Refresh button**: triggers the pipeline, polls to completion, verifies the data timestamp actually advanced before claiming success; explains itself if queued behind a long full rebuild.
- Data loads on every page open (no manual steps); "Live from Jira · updated <time>" shows the real last-sync time in the viewer's local timezone.

---

## 6. Hard-won lessons (read before debugging anything)

These cost real time to discover — don't re-learn them:

1. **`raw.githubusercontent.com` is fronted by Fastly, which caches by URL path ONLY and completely ignores query strings.** Cache-busting `?_=timestamp` there does nothing (`x-cache: HIT` regardless). For guaranteed-fresh reads, go through the Worker's `/data/:file` route, which uses GitHub's **Contents API** (`cache-control: private` → shared CDNs can't serve it stale). The initial page load still uses raw content (eventual consistency is fine there); only the post-refresh verification needs the fresh path.
2. **GitHub auto-revokes its own tokens found in public repos** — separate from push protection, and *after* you approve the push. A hardcoded GitHub PAT in the public dashboard died within minutes. This is why the Cloudflare Worker exists: the token lives only as a Worker secret.
3. **Cloudflare's Git-connected Worker builds can silently deploy stale code** (suspected build cache). Fixed by renaming the entry file (`worker.js` → `index.js`) + updating `wrangler.toml`. **Always verify a Worker deploy via `GET /version`** — bump `WORKER_VERSION` in `cloudflare-worker/index.js` on every change. If a deploy goes stale again: Cloudflare → Settings → Build → **Clear Cache**, or paste the code manually via "Edit code" → Deploy.
4. **Cloudflare has TWO "Variables and secrets" sections** — the runtime one (top of Settings; what the Worker reads as `env.X`) and "Build environment variables" (build-time only, useless for runtime). The `GITHUB_TOKEN` must be in the **runtime** one.
5. **Jira retired `GET /rest/api/3/search`** (returns 410 Gone). Use `POST /rest/api/3/search/jql` with `nextPageToken` pagination.
6. **Timestamps need explicit timezone markers.** `datetime.now().isoformat()` (naive) gets misread by browsers as local time. Display timestamps use `datetime.now(timezone.utc)`; the "now" for open-ticket math uses Cairo wall-clock (`ZoneInfo("Africa/Cairo")`, naive) to match how Jira timestamps are parsed. Internal sync watermarks (`last_sync_at`) are naive by design — don't "fix" them without checking the JQL comparison logic.
7. **Concurrent workflow runs used to race on `git push` and silently drop data** while both reported success. Fixed with the workflow `concurrency` group + retry-with-rebase + loud failure. If data ever looks frozen despite green runs, suspect this class of bug first.
8. **GitHub's Actions API responses carry `cache-control: max-age=60`** — the Worker explicitly disables caching (`cf: { cacheTtl: 0 }`, note: `0`, not `-1`, which is silently invalid) and cache-busts GET calls.
9. **GitHub's API has occasional 503 blips** (minutes-long). The refresh button distinguishes "still running (full rebuild ~20–30 min, will update automatically)" from a genuine failure.
10. Workflow logs live on `productionresultssa*.blob.core.windows.net` (often unreachable from restricted environments) — that's why the workflow commits `debug/last_run.log` into the repo, and emits `::error::` annotations readable via the check-runs API.

---

## 7. Common changes — how to

| Want to… | Do this |
|---|---|
| Add/remove a tracked project | Edit `scripts/epics.json` `"projects"` array, commit. Next run picks it up. |
| Track one extra epic from an untracked project | Add its key to the `"epics"` array. |
| Change the day rate | Repo Settings → Variables → `RATE_PER_DAY_EGP` (pipeline), or just type in the dashboard's rate box (display-only). |
| Change sync schedule | Edit the `cron` lines in `.github/workflows/update-budget.yml`. |
| Update Egypt holidays | Edit `EGYPT_HOLIDAYS` in `scripts/jira_epic_budget.py`. |
| Change status inclusion/exclusion rules | Edit `START_PATTERN` / `DONE_PATTERN` / `EXCLUDED_PATTERN` regexes near the top of the script. |
| Force a full rebuild now | Actions tab → Update Epic Budget Data → Run workflow → tick "Force a full rebuild". |
| Rotate the Jira token | New token at id.atlassian.com → update repo secret `JIRA_API_TOKEN`. |
| Rotate the Worker's GitHub token | New fine-grained PAT (this repo only, Actions: R+W) → Cloudflare Worker → Settings → Variables and Secrets (runtime) → rotate `GITHUB_TOKEN`. |
| Verify what Worker build is live | Open `https://nawy-epic-budget.ahmed-matbouly.workers.dev/version` |
| Change dashboard look/logic | Edit `docs/index.html`, push; GitHub Pages redeploys (allow ~1–10 min CDN). |

## 8. Worker API (for reference)

```
GET  /version           → { version, routes }            (deployment sanity check)
POST /dispatch          → triggers the workflow           (used by Refresh button)
GET  /latest-run        → { id, status, conclusion }      (most recent run)
GET  /run/:id           → { status, conclusion }
GET  /data/:file.json   → fresh file contents via GitHub Contents API (cache-immune)
```

## 9. Data file shape (per epic)

```json
{
  "epic_key": "SC-377",
  "epic_name": "Lead Profile",
  "project_key": "SC",
  "project_name": "Suite Control",
  "epic_created": "2025-02-23T08:05:58.095+0200",
  "generated_at": "<UTC ISO>",
  "rate_per_day_egp": 3500,
  "total_tickets": 51, "complete_tickets": 43, "in_progress_tickets": 8,
  "total_net_days": 237.71, "total_cost_egp": 831985.0,
  "tickets": [ { "key", "summary", "status", "net_days", "gross_days",
                 "excluded_days", "cost_egp", "is_complete", "num_spans" } ]
}
```
`data/index.json` holds per-epic summaries + `updated_at` (what the dashboard's freshness check compares).

## 10. Known limitations / future ideas

- Time filter is by **epic creation date**, not when work happened (prorating cost across quarters would need per-ticket span timestamps stored).
- Cost is per-ticket elapsed effort — doesn't multiply for several people working the same ticket in parallel.
- Egypt holiday list is static; Islamic dates need yearly review.
- Anyone on the internet can view the dashboard/repo (public) and press Refresh (worst case: extra workflow runs). Making it private would require GitHub Pro/Team for Pages, plus an auth story for the data reads.
- Possible next steps discussed: portfolio-overview tab, per-quarter cost proration, budget-vs-plan comparison per epic.
