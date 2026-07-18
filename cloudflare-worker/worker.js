/**
 * Cloudflare Worker: GitHub Actions trigger proxy for the Nawy Epic Budget dashboard.
 *
 * Purpose: the dashboard's "Refresh from Jira" button needs to call GitHub's
 * workflow_dispatch API, which requires a token. Putting that token directly
 * in the dashboard's HTML doesn't work - it's a public repo, so GitHub's
 * secret scanning auto-revokes any real GitHub token it finds there, even
 * after you approve the push. This Worker holds the token server-side
 * instead (as a Cloudflare "secret", never visible in any browser or repo)
 * and exposes a small public API that the dashboard calls instead.
 *
 * Routes:
 *   POST /dispatch      -> triggers the update-budget.yml workflow
 *   GET  /latest-run    -> returns the most recent run's {id, status, conclusion}
 *   GET  /run/:id       -> returns a specific run's {status, conclusion}
 *
 * Setup (Cloudflare dashboard, no CLI needed):
 *   1. workers.cloudflare.com -> Create Worker -> paste this file's contents
 *   2. Worker -> Settings -> Variables and Secrets -> add secret:
 *        name: GITHUB_TOKEN
 *        value: <a Fine-grained PAT scoped to ONLY this repo, Actions: Read+Write>
 *   3. Deploy. Copy the Worker's URL (looks like
 *        https://nawy-budget-refresh.<your-subdomain>.workers.dev
 *      and paste it into the dashboard's WORKER_URL constant.
 */

const GH_OWNER = 'ahmedmatbouly-spec';
const GH_REPO = 'nawy-epic-budget';
const GH_WORKFLOW = 'update-budget.yml';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Cache-Control': 'no-store',
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...CORS_HEADERS },
  });
}

async function githubFetch(env, path, opts = {}) {
  // Cache-bust GET requests with a unique query param on every call - this
  // guarantees a fresh request regardless of any cache layer's specific TTL
  // semantics, rather than relying on cf.cacheTtl alone (which needs 0, not
  // -1, to actually disable caching - a bug in an earlier version of this
  // file). Left off POST requests (dispatch) to avoid any risk of an
  // unexpected extra query param affecting a write endpoint.
  const method = (opts && opts.method) || 'GET';
  let finalPath = path;
  if (method === 'GET') {
    const sep = path.includes('?') ? '&' : '?';
    finalPath = `${path}${sep}_cb=${Date.now()}`;
  }
  return fetch(`https://api.github.com${finalPath}`, {
    ...opts,
    headers: {
      'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'User-Agent': 'nawy-epic-budget-worker',
      'Cache-Control': 'no-cache',
      ...(opts.headers || {}),
    },
    cf: { cacheTtl: 0, cacheEverything: false },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS_HEADERS });
    }

    if (!env.GITHUB_TOKEN) {
      return json({ error: 'Worker is not configured - GITHUB_TOKEN secret is missing' }, 500);
    }

    try {
      if (url.pathname === '/dispatch' && request.method === 'POST') {
        const resp = await githubFetch(
          env,
          `/repos/${GH_OWNER}/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/dispatches`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ref: 'main' }),
          }
        );
        if (resp.status === 204) return json({ ok: true });
        const body = await resp.text();
        return json({ ok: false, status: resp.status, body }, resp.status);
      }

      if (url.pathname === '/latest-run' && request.method === 'GET') {
        const resp = await githubFetch(
          env,
          `/repos/${GH_OWNER}/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/runs?per_page=1`
        );
        if (!resp.ok) return json({ ok: false, status: resp.status }, resp.status);
        const data = await resp.json();
        const run = (data.workflow_runs || [])[0];
        return json({ ok: true, id: run ? run.id : null, status: run ? run.status : null, conclusion: run ? run.conclusion : null });
      }

      const runMatch = url.pathname.match(/^\/run\/(\d+)$/);
      if (runMatch && request.method === 'GET') {
        const runId = runMatch[1];
        const resp = await githubFetch(env, `/repos/${GH_OWNER}/${GH_REPO}/actions/runs/${runId}`);
        if (!resp.ok) return json({ ok: false, status: resp.status }, resp.status);
        const data = await resp.json();
        return json({ ok: true, status: data.status, conclusion: data.conclusion });
      }

      return json({ error: 'Not found. Routes: POST /dispatch, GET /latest-run, GET /run/:id' }, 404);
    } catch (e) {
      return json({ error: 'Worker error: ' + e.message }, 500);
    }
  },
};
