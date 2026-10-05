#!/usr/bin/env python3
"""Turn data/health.json into a GitHub Issue (opened, updated, or closed).

Run from GitHub Actions with GITHUB_TOKEN and GITHUB_REPOSITORY set:
    python3 scripts/health_alert.py                 # after a scan
    python3 scripts/health_alert.py --stale-hours 7 # watchdog: scans stopped?

One open issue (label "health-alert") at a time. Failing checks open it or
update it; when everything is green again it is commented on and closed.
Warnings only show on the dashboard, they don't open issues.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_FILE = os.path.join(ROOT, "data", "health.json")
LABEL = "health-alert"
API = "https://api.github.com"


def gh(method, path, **kw):
    r = requests.request(method, API + path, timeout=20, headers={
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
    }, **kw)
    if r.status_code >= 400:
        print("GitHub API %s %s -> %s %s" % (method, path, r.status_code, r.text[:200]))
        return None
    return r.json() if r.text else {}


def failing_from_report(report):
    return [c for c in report.get("checks", []) if c.get("status") == "fail"]


def body_for(fails, report):
    lines = ["Automated health check found problems in the latest scan.", "",
             "| Check | Problem |", "|---|---|"]
    for c in fails:
        lines.append("| %s | %s |" % (c.get("name"), str(c.get("detail", "")).replace("|", "/")))
    lines += ["", "Scan: %s · code `%s`" % (report.get("generated_at", "?"), report.get("code_version") or "?"),
              "", "This issue closes itself when the checks pass again.",
              "<!-- fails: %s -->" % ",".join(sorted(c["id"] for c in fails))]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-hours", type=float, default=0,
                    help="watchdog mode: alert if the last health report is older than this")
    args = ap.parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or not os.environ.get("GITHUB_TOKEN"):
        print("GITHUB_TOKEN / GITHUB_REPOSITORY not set; skipping alert")
        return 0

    try:
        with open(HEALTH_FILE) as f:
            report = json.load(f)
    except (OSError, ValueError):
        report = {"checks": []}

    if args.stale_hours:
        ts = report.get("generated_at")
        age = None
        if ts:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
        if age is not None and age <= args.stale_hours:
            print("last scan %.1fh ago - fine" % age)
            return 0
        fails = [{"id": "scan_stale", "name": "Scans running",
                  "detail": "no scan has completed for %s hours - check Actions > Scheduled scan"
                            % ("unknown" if age is None else "%.0f" % age)}]
    else:
        fails = failing_from_report(report)
        outcome = os.environ.get("SCAN_OUTCOME")
        if outcome and outcome != "success":
            fails.insert(0, {"id": "scan_crashed", "name": "Scan completed",
                             "detail": "the pipeline step ended with '%s' - open the run log in "
                                       "Actions > Scheduled scan" % outcome})

    open_issues = gh("GET", "/repos/%s/issues" % repo,
                     params={"labels": LABEL, "state": "open", "per_page": 5}) or []
    issue = open_issues[0] if open_issues else None

    if not fails:
        if issue and not args.stale_hours:
            gh("POST", "/repos/%s/issues/%d/comments" % (repo, issue["number"]),
               json={"body": "✅ All health checks pass again (scan %s). Closing." % report.get("generated_at")})
            gh("PATCH", "/repos/%s/issues/%d" % (repo, issue["number"]), json={"state": "closed"})
            print("closed health issue #%d" % issue["number"])
        else:
            print("healthy")
        return 0

    body = body_for(fails, report)
    title = "🩺 Health check failing: " + ", ".join(c["name"] for c in fails)
    if issue:
        marker = "<!-- fails: %s -->" % ",".join(sorted(c["id"] for c in fails))
        if marker not in (issue.get("body") or ""):
            gh("PATCH", "/repos/%s/issues/%d" % (repo, issue["number"]), json={"title": title, "body": body})
            gh("POST", "/repos/%s/issues/%d/comments" % (repo, issue["number"]),
               json={"body": "Failing checks changed:\n\n" + body})
            print("updated health issue #%d" % issue["number"])
        else:
            print("health issue #%d already open for the same checks" % issue["number"])
    else:
        gh("POST", "/repos/%s/labels" % repo,
           json={"name": LABEL, "color": "d73a4a", "description": "Automated health check"})
        created = gh("POST", "/repos/%s/issues" % repo,
                     json={"title": title, "body": body, "labels": [LABEL]})
        print("opened health issue #%s" % (created or {}).get("number"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
