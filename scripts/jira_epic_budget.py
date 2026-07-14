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
from datetime import datetime, date, timedelta

JIRA_SITE = os.environ.get("JIRA_SITE", "")
EMAIL = os.environ.get("JIRA_EMAIL", "")
API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
RATE_PER_DAY = float(os.environ.get("RATE_PER_DAY_EGP", "3500"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
EPICS_FILE = os.path.join(SCRIPT_DIR, "epics.json")

START_PATTERN = re.compile(r"in\s*progress", re.I)
DONE_PATTERN = re.compile(
    r"\b(done|released|closed|resolved|deployed|ready\s*for\s*deployment|"
    r"ready\s*for\s*production)\b", re.I
)
EXCLUDED_PATTERN = re.compile(
    r"\b(on[\s-]*hold|product\s*uat|read?y?\s*for\s*uat|\buat\b)\b", re.I
)

WEEKEND_DAYS = {4, 5}  # Friday, Saturday

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


def jira_get(path, params=None):
    url = f"{JIRA_SITE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{EMAIL}:{API_TOKEN}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}", "Accept": "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def get_child_issues(epic_key):
    issues, start_at = [], 0
    while True:
        data = jira_get("/rest/api/3/search", {
            "jql": f"parent = {epic_key} ORDER BY created ASC",
            "fields": "summary,status,issuetype",
            "startAt": start_at, "maxResults": 100,
        })
        issues.extend(data["issues"])
        start_at += len(data["issues"])
        if start_at >= data["total"] or not data["issues"]:
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
    now = now or datetime.now()
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


def process_epic(epic_key):
    print(f"Fetching {epic_key} ...")
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
        "generated_at": datetime.now().isoformat(),
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


def main():
    if not (JIRA_SITE and EMAIL and API_TOKEN):
        print("ERROR: JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN must be set (as env vars / secrets).")
        sys.exit(1)

    if not os.path.exists(EPICS_FILE):
        print(f"ERROR: {EPICS_FILE} not found. Create it with a JSON array of epic keys.")
        sys.exit(1)

    with open(EPICS_FILE) as f:
        epic_keys = json.load(f)

    summaries = []
    for epic_key in epic_keys:
        result = process_epic(epic_key)
        summaries.append({
            "epic_key": result["epic_key"],
            "total_net_days": result["total_net_days"],
            "total_cost_egp": result["total_cost_egp"],
            "total_tickets": result["total_tickets"],
            "complete_tickets": result["complete_tickets"],
            "generated_at": result["generated_at"],
        })

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "index.json"), "w") as f:
        json.dump({"epics": summaries, "updated_at": datetime.now().isoformat()}, f, indent=2)

    print(f"\nDone. Processed {len(epic_keys)} epic(s).")


if __name__ == "__main__":
    main()
