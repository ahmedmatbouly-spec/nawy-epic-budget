#!/usr/bin/env python3
"""
Jira Epic Budget Fetcher (automated pipeline version)
========================================================
Runs on a schedule (via GitHub Actions). For every epic key listed in
epics.json, computes net business-days consumed and cost, and writes
results to data/<EPIC_KEY>.json plus an data/index.json manifest.

Auth comes from environment variables (set as GitHub Actions secrets):
    JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN

Epics to track are listed in scripts/epics.json:
    ["SC-377", "NCRM-520"]

See the logic docs in the previous single-epic version for full detail
on start/end/exclusion rules - unchanged here.
"""
import sys
import os
import re
import json
import base64
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

JIRA_SITE = os.environ.get("JIRA_SITE", "")
EMAIL = os.environ.get("JIRA_EMAIL", "")
API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
RATE_PER_DAY = float(os.environ.get("RATE_PER_DAY_EGP", "3500"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
EPICS_FILE = os.path.join(SCRIPT_DIR, "epics.json")
SYNC_STATE_FILE = os.path.join(DATA_DIR, "sync_state.json")

# Safety overlap subtracted from "now" before each sync, to guard against
# clock skew / issues updated mid-run being missed by the next incremental pass.
SYNC_OVERLAP_MINUTES = 5

START_PATTERN = re.compile(r"in\s*progress", re.I)
DONE_PATTERN = re.compile(
    r"\b(done|released|closed|resolved|deployed|ready\s*for\s*deployment|"
    r"ready\s*for\s*production)\b", re.I
)
EXCLUDED_PATTERN = re.compile(
    r"\b(on[\s-]*hold|product\s*uat|read?y?\s*for\s*uat|\buat\b|"
    r"to\s*do|backlog)\b", re.I
)

WEEKEND_DAYS = {4, 5}  # Friday, Saturday

# Used to get an accurate "now" in Cairo wall-clock time (DST-aware via the
# system tz database) - needed because Jira's own timestamps come back in
# Cairo local time with the offset stripped (see parse_jira_ts), so "now" for
# still-open tickets needs to be in the same frame of reference, not the
# GitHub Actions runner's raw UTC clock.
CAIRO_TZ = ZoneInfo("Africa/Cairo")

EGYPT_HOLIDAYS = {
    "2024-01-07", "2024-01-25", "2024-04-10", "2024-04-11", "2024-04-25",
    "2024-05-01", "2024-05-06", "2024-06-16", "2024-06-17", "2024-06-18",
    "2024-07-07", "2024-07-23", "2024-09-15", "2024-10-06",
    "2025-01-07", "2025-01-25", "2025-03-30", "2025-03-31", "2025-04-20",
    "2025-04-25", "2025-05-01", "2025-06-05", "2025-06-06", "2025-06-07",
    "2025-06-26", "2025-07-23", "2025-09-04", "2025-10-06",
    "2026-01-07", "2026-01-25", "2026-03-20", "2026-03-21", "2026-04-25",
    "2026-05-01", "2026-05-27", "2026-05-28", "2026-05-29", "2026-06-16",
    "2026-07-23", "2026-08-24", "2026-10-06",
}
EGYPT_HOLIDAY_DATES = {datetime.strptime(d, "%Y-%m-%d").date() for d in EGYPT_HOLIDAYS}


def _request_with_retry(req, max_retries=5):
    import time
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 5)) if e.headers else 5
                print(f"  Rate limited (429), waiting {wait}s...")
                time.sleep(wait + 1)
                continue
            raise
    raise Exception(f"Gave up after {max_retries} retries (rate limited)")


def jira_get(path, params=None):
    url = f"{JIRA_SITE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{EMAIL}:{API_TOKEN}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}", "Accept": "application/json",
    })
    return _request_with_retry(req)


def jira_post(path, body):
    url = f"{JIRA_SITE}{path}"
    auth = base64.b64encode(f"{EMAIL}:{API_TOKEN}".encode()).decode()
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return _request_with_retry(req)


def get_project_name(project_key):
    try:
        data = jira_get(f"/rest/api/3/project/{project_key}")
        return data.get("name", project_key)
    except Exception:
        return project_key


def get_epics_in_project(project_key):
    """Find every Epic-type issue in a project via JQL."""
    epics = []
    next_token = None
    while True:
        body = {
            "jql": f"project = {project_key} AND issuetype = Epic ORDER BY created ASC",
            "fields": ["summary", "status", "created"],
            "maxResults": 100,
        }
        if next_token:
            body["nextPageToken"] = next_token
        data = jira_post("/rest/api/3/search/jql", body)
        epics.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast", True):
            break
    return [(e["key"], e["fields"]["summary"], e["fields"].get("created")) for e in epics]


def get_epic_name(epic_key):
    """Fetch a single epic's summary/name/created date (used when an epic key
    is listed explicitly in epics.json rather than discovered via a project scan)."""
    data = jira_get(f"/rest/api/3/issue/{epic_key}", {"fields": "summary,created"})
    return data["fields"]["summary"], data["fields"].get("created")


def get_changed_issues_since(project_keys, since_dt):
    """Find every issue (Epic or otherwise) updated since `since_dt` within the
    given projects. Returns raw issue dicts with summary/status/issuetype/
    created/parent fields - used to drive incremental sync."""
    if not project_keys:
        return []
    # JQL date literal format: "yyyy-MM-dd HH:mm"
    since_str = since_dt.strftime("%Y-%m-%d %H:%M")
    project_list = ", ".join(project_keys)
    issues = []
    next_token = None
    while True:
        body = {
            "jql": f'project in ({project_list}) AND updated >= "{since_str}" ORDER BY updated ASC',
            "fields": ["summary", "status", "issuetype", "created", "parent", "project"],
            "maxResults": 100,
        }
        if next_token:
            body["nextPageToken"] = next_token
        data = jira_post("/rest/api/3/search/jql", body)
        issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast", True):
            break
    return issues


def compute_ticket_entry(issue):
    """Fetch changelog + compute the net-days/cost entry for a single ticket issue dict."""
    key = issue["key"]
    summary = issue["fields"]["summary"]
    status_name = issue["fields"]["status"]["name"]
    try:
        histories, cur_status = get_changelog(key)
        transitions = extract_status_transitions(histories)
        result = compute_net_days(transitions, cur_status)
        cost = round(result["net_days"] * RATE_PER_DAY, 2)
        return {
            "key": key, "summary": summary, "status": status_name,
            "net_days": result["net_days"], "gross_days": result["gross_days"],
            "excluded_days": result["excluded_days"], "cost_egp": cost,
            "is_complete": result["is_complete"], "num_spans": result["num_spans"],
        }
    except Exception as e:
        return {"key": key, "summary": summary, "status": status_name,
                "net_days": 0, "cost_egp": 0, "error": str(e)}


def load_epic_file(epic_key):
    path = os.path.join(DATA_DIR, f"{epic_key}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def recompute_epic_totals(output):
    tickets = output["tickets"]
    output["total_net_days"] = round(sum(t.get("net_days", 0) for t in tickets), 2)
    output["total_cost_egp"] = round(sum(t.get("cost_egp", 0) for t in tickets), 2)
    output["total_tickets"] = len(tickets)
    output["complete_tickets"] = sum(1 for t in tickets if t.get("is_complete"))
    output["in_progress_tickets"] = output["total_tickets"] - output["complete_tickets"]
    output["generated_at"] = datetime.now(timezone.utc).isoformat()
    return output


def sync_epic_incremental(epic_key, changed_ticket_keys, epic_meta_update=None):
    """
    Patch a single epic's stored data using only the tickets that actually
    changed, instead of re-walking every child from scratch:
      1. Ask Jira for the epic's CURRENT full child list (one cheap call).
      2. Diff against what's stored: figure out additions, removals, and
         which of the "changed" tickets are still actually children here.
      3. Only fetch changelogs (the expensive part) for tickets that are
         new or were flagged as changed. Untouched tickets keep their
         cached values.
    """
    existing = load_epic_file(epic_key)
    if existing is None:
        return None  # caller should do a full process_epic() instead

    if epic_meta_update:
        existing["epic_name"] = epic_meta_update.get("epic_name", existing.get("epic_name"))
        existing["epic_created"] = epic_meta_update.get("epic_created", existing.get("epic_created"))

    current_children = get_child_issues(epic_key)
    current_keys = {i["key"] for i in current_children}
    current_by_key = {i["key"]: i for i in current_children}

    existing_tickets_by_key = {t["key"]: t for t in existing.get("tickets", [])}
    existing_keys = set(existing_tickets_by_key.keys())

    to_remove = existing_keys - current_keys
    new_keys = current_keys - existing_keys
    to_refetch = (changed_ticket_keys & current_keys) | new_keys

    if to_remove:
        print(f"    {epic_key}: removing {len(to_remove)} ticket(s) no longer children here")
    if to_refetch:
        print(f"    {epic_key}: refetching {len(to_refetch)} changed/new ticket(s)")

    new_tickets = []
    for key in current_keys:
        if key in to_refetch:
            new_tickets.append(compute_ticket_entry(current_by_key[key]))
        else:
            new_tickets.append(existing_tickets_by_key[key])  # unchanged - reuse cached value

    existing["tickets"] = new_tickets
    return recompute_epic_totals(existing)


def get_child_issues(epic_key):
    # NOTE: GET /rest/api/3/search was deprecated/removed by Atlassian
    # (returns 410 Gone). The replacement is POST /rest/api/3/search/jql,
    # which uses nextPageToken-based pagination instead of startAt/total.
    issues = []
    next_token = None
    while True:
        body = {
            "jql": f"parent = {epic_key} ORDER BY created ASC",
            "fields": ["summary", "status", "issuetype"],
            "maxResults": 100,
        }
        if next_token:
            body["nextPageToken"] = next_token
        data = jira_post("/rest/api/3/search/jql", body)
        issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast", True):
            break
    return issues


def get_changelog(issue_key):
    data = jira_get(f"/rest/api/3/issue/{issue_key}", {
        "expand": "changelog", "fields": "status",
    })
    return data["changelog"]["histories"], data["fields"]["status"]["name"]


def is_business_day(d: date) -> bool:
    return d.weekday() not in WEEKEND_DAYS and d not in EGYPT_HOLIDAY_DATES


def business_days_between(start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    WORKDAY_START_HOUR, WORKDAY_END_HOUR = 9, 17
    WORKDAY_HOURS = WORKDAY_END_HOUR - WORKDAY_START_HOUR
    total = 0.0
    cur_date = start.date()
    while cur_date <= end.date():
        if is_business_day(cur_date):
            day_start = datetime.combine(cur_date, datetime.min.time()).replace(hour=WORKDAY_START_HOUR)
            day_end = datetime.combine(cur_date, datetime.min.time()).replace(hour=WORKDAY_END_HOUR)
            seg_start = max(start, day_start) if cur_date == start.date() else day_start
            seg_end = min(end, day_end) if cur_date == end.date() else day_end
            seconds = max(0.0, (seg_end - seg_start).total_seconds())
            total += seconds / (WORKDAY_HOURS * 3600)
        cur_date += timedelta(days=1)
    return total


def parse_jira_ts(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def extract_status_transitions(histories):
    transitions = []
    for h in histories:
        ts = parse_jira_ts(h["created"])
        for item in h["items"]:
            if item["field"] == "status":
                transitions.append((ts, item["fromString"], item["toString"]))
    transitions.sort(key=lambda x: x[0])
    return transitions


def compute_net_days(transitions, current_status_name, now=None):
    now = now or datetime.now(CAIRO_TZ).replace(tzinfo=None)
    spans, current_start = [], None

    for ts, frm, to in transitions:
        if START_PATTERN.search(to or ""):
            if current_start is None:
                current_start = ts
        elif DONE_PATTERN.search(to or "") and current_start is not None:
            spans.append((current_start, ts, True))
            current_start = None

    if current_start is not None:
        spans.append((current_start, now, False))

    if not spans:
        return {
            "net_days": 0.0, "gross_days": 0.0, "excluded_days": 0.0,
            "is_complete": DONE_PATTERN.search(current_status_name or "") is not None,
            "num_spans": 0,
        }

    gross, excluded = 0.0, 0.0
    for span_start, span_end, _ in spans:
        gross += business_days_between(span_start, span_end)
        cur_status, cur_from = None, None
        for ts, frm, to in transitions:
            if span_start <= ts <= span_end:
                if cur_status and EXCLUDED_PATTERN.search(cur_status) and cur_from:
                    excluded += business_days_between(cur_from, ts)
                cur_status, cur_from = to, ts
        if cur_status and EXCLUDED_PATTERN.search(cur_status) and cur_from:
            excluded += business_days_between(cur_from, span_end)

    is_complete = all(s[2] for s in spans)
    return {
        "net_days": round(max(0.0, gross - excluded), 2),
        "gross_days": round(gross, 2),
        "excluded_days": round(excluded, 2),
        "is_complete": is_complete,
        "num_spans": len(spans),
    }


def process_epic(epic_key, epic_name=None, project_key=None, project_name=None, epic_created=None):
    project_key = project_key or epic_key.split("-")[0]
    project_name = project_name or project_key
    print(f"Fetching {epic_key} ({epic_name or 'name unknown'}) ...")
    issues = get_child_issues(epic_key)
    tickets = []
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        status_name = issue["fields"]["status"]["name"]
        try:
            histories, cur_status = get_changelog(key)
            transitions = extract_status_transitions(histories)
            result = compute_net_days(transitions, cur_status)
            cost = round(result["net_days"] * RATE_PER_DAY, 2)
            tickets.append({
                "key": key, "summary": summary, "status": status_name,
                "net_days": result["net_days"], "gross_days": result["gross_days"],
                "excluded_days": result["excluded_days"], "cost_egp": cost,
                "is_complete": result["is_complete"], "num_spans": result["num_spans"],
            })
            print(f"  {key}: {result['net_days']}d")
        except Exception as e:
            print(f"  {key}: ERROR {e}")
            tickets.append({"key": key, "summary": summary, "status": status_name,
                             "net_days": 0, "cost_egp": 0, "error": str(e)})

    total_days = round(sum(t.get("net_days", 0) for t in tickets), 2)
    total_cost = round(sum(t.get("cost_egp", 0) for t in tickets), 2)
    complete_count = sum(1 for t in tickets if t.get("is_complete"))

    output = {
        "epic_key": epic_key,
        "epic_name": epic_name or epic_key,
        "project_key": project_key,
        "project_name": project_name,
        "epic_created": epic_created,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rate_per_day_egp": RATE_PER_DAY,
        "total_tickets": len(tickets),
        "complete_tickets": complete_count,
        "in_progress_tickets": len(tickets) - complete_count,
        "total_net_days": total_days,
        "total_cost_egp": total_cost,
        "tickets": tickets,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, f"{epic_key}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {out_path}: {total_days} days, {total_cost:,.0f} EGP")
    return output


def load_config():
    if not os.path.exists(EPICS_FILE):
        print(f"ERROR: {EPICS_FILE} not found.")
        sys.exit(1)
    with open(EPICS_FILE) as f:
        config = json.load(f)
    if isinstance(config, list):
        config = {"projects": [], "epics": config}
    return config


def discover_all_epics(config):
    """Returns epic_key -> {epic_name, project_key, project_name, epic_created}
    for every epic currently reachable via configured projects/explicit keys."""
    epics_to_process = {}
    project_name_cache = {}

    for project_key in config.get("projects", []):
        print(f"Discovering epics in project {project_key} ...")
        if project_key not in project_name_cache:
            project_name_cache[project_key] = get_project_name(project_key)
        found = get_epics_in_project(project_key)
        print(f"  found {len(found)} epics")
        for key, name, created in found:
            epics_to_process[key] = {
                "epic_name": name,
                "project_key": project_key,
                "project_name": project_name_cache[project_key],
                "epic_created": created,
            }

    for epic_key in config.get("epics", []):
        if epic_key not in epics_to_process:
            proj_key = epic_key.split("-")[0]
            if proj_key not in project_name_cache:
                project_name_cache[proj_key] = get_project_name(proj_key)
            try:
                name, created = get_epic_name(epic_key)
            except Exception as e:
                print(f"  Could not fetch name for {epic_key}: {e}")
                name, created = epic_key, None
            epics_to_process[epic_key] = {
                "epic_name": name,
                "project_key": proj_key,
                "project_name": project_name_cache[proj_key],
                "epic_created": created,
            }

    return epics_to_process


def summary_from_output(result):
    return {
        "epic_key": result["epic_key"],
        "epic_name": result["epic_name"],
        "project_key": result["project_key"],
        "project_name": result["project_name"],
        "epic_created": result["epic_created"],
        "total_net_days": result["total_net_days"],
        "total_cost_egp": result["total_cost_egp"],
        "total_tickets": result["total_tickets"],
        "complete_tickets": result["complete_tickets"],
        "generated_at": result["generated_at"],
    }


def write_index_from_all_files():
    """Rebuild data/index.json by scanning every data/<EPIC>.json on disk -
    used after incremental syncs so epics we didn't touch this run still
    show up correctly."""
    summaries = []
    if os.path.isdir(DATA_DIR):
        for fname in sorted(os.listdir(DATA_DIR)):
            if fname.endswith(".json") and fname not in ("index.json", "sync_state.json"):
                with open(os.path.join(DATA_DIR, fname)) as f:
                    try:
                        data = json.load(f)
                        summaries.append(summary_from_output(data))
                    except Exception:
                        continue
    with open(os.path.join(DATA_DIR, "index.json"), "w") as f:
        json.dump({"epics": summaries, "updated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    return summaries


def full_sync(config):
    print("=== FULL SYNC (rebuilding every tracked epic from scratch) ===")
    epics_to_process = discover_all_epics(config)

    for epic_key, meta in epics_to_process.items():
        process_epic(epic_key, meta["epic_name"], meta["project_key"],
                     meta["project_name"], meta["epic_created"])

    summaries = write_index_from_all_files()
    print(f"\nDone. Processed {len(epics_to_process)} epic(s) (full sync).")
    return summaries


def incremental_sync(config):
    if not os.path.exists(SYNC_STATE_FILE):
        print("No previous sync_state.json found - falling back to full sync.")
        summaries = full_sync(config)
        with open(SYNC_STATE_FILE, "w") as f:
            json.dump({"last_sync_at": datetime.now().isoformat()}, f, indent=2)
        return summaries

    with open(SYNC_STATE_FILE) as f:
        state = json.load(f)
    last_sync_at = datetime.fromisoformat(state["last_sync_at"])
    sync_started_at = datetime.now()

    project_keys = config.get("projects", [])
    print(f"=== INCREMENTAL SYNC since {last_sync_at.isoformat()} ===")

    changed_issues = get_changed_issues_since(project_keys, last_sync_at)
    print(f"Found {len(changed_issues)} issue(s) updated since last sync.")

    changed_epics = {}          # epic_key -> {epic_name, epic_created, project_key, project_name}
    changed_children = {}       # parent_epic_key -> set(ticket_keys)

    for issue in changed_issues:
        fields = issue["fields"]
        issuetype = fields.get("issuetype", {}).get("name", "")
        proj = fields.get("project", {})
        proj_key = proj.get("key", issue["key"].split("-")[0])

        if issuetype == "Epic":
            changed_epics[issue["key"]] = {
                "epic_name": fields.get("summary"),
                "epic_created": fields.get("created"),
                "project_key": proj_key,
                "project_name": proj.get("name", proj_key),
            }
        else:
            parent = fields.get("parent")
            if parent and parent.get("key"):
                changed_children.setdefault(parent["key"], set()).add(issue["key"])

    epics_to_touch = set(changed_epics.keys()) | set(changed_children.keys())
    print(f"Epics needing an update this run: {len(epics_to_touch)}")

    for epic_key in epics_to_touch:
        meta_update = changed_epics.get(epic_key)
        existing = load_epic_file(epic_key)

        if existing is None:
            # Brand new epic (or one we've never generated before) - no
            # baseline to patch, so do a normal full fetch for this one epic.
            if meta_update:
                epic_name = meta_update["epic_name"]
                epic_created = meta_update["epic_created"]
                project_key = meta_update["project_key"]
                project_name = meta_update["project_name"]
            else:
                proj_key = epic_key.split("-")[0]
                try:
                    epic_name, epic_created = get_epic_name(epic_key)
                except Exception as e:
                    print(f"  Could not fetch name for new epic {epic_key}: {e}")
                    epic_name, epic_created = epic_key, None
                project_key = proj_key
                project_name = get_project_name(proj_key)
            print(f"  {epic_key}: new epic, full fetch")
            process_epic(epic_key, epic_name, project_key, project_name, epic_created)
        else:
            updated = sync_epic_incremental(epic_key, changed_children.get(epic_key, set()), meta_update)
            if updated:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(os.path.join(DATA_DIR, f"{epic_key}.json"), "w") as f:
                    json.dump(updated, f, indent=2)
                print(f"  {epic_key}: patched ({updated['total_net_days']}d, {updated['total_cost_egp']:,.0f} EGP)")

    summaries = write_index_from_all_files()

    # Move the sync watermark back by a small overlap to guard clock skew /
    # issues updated mid-run, rather than using "now" exactly.
    new_last_sync = sync_started_at - timedelta(minutes=SYNC_OVERLAP_MINUTES)
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump({"last_sync_at": new_last_sync.isoformat()}, f, indent=2)

    print(f"\nDone. Incremental sync touched {len(epics_to_touch)} epic(s), "
          f"{len(summaries)} epic(s) total tracked.")
    return summaries


def main():
    if not (JIRA_SITE and EMAIL and API_TOKEN):
        print("ERROR: JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN must be set (as env vars / secrets).")
        sys.exit(1)

    force_full = "--full" in sys.argv
    config = load_config()

    if force_full:
        summaries = full_sync(config)
        with open(SYNC_STATE_FILE, "w") as f:
            json.dump({"last_sync_at": datetime.now().isoformat()}, f, indent=2)
    else:
        summaries = incremental_sync(config)

    total_days = sum(s["total_net_days"] for s in summaries)
    total_cost = sum(s["total_cost_egp"] for s in summaries)
    print(f"Portfolio: {total_days:.1f} days, {total_cost:,.0f} EGP across {len(summaries)} epics")


if __name__ == "__main__":
    main()
