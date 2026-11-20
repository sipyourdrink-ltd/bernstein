"""Report overdue GitHub Actions environment gates in the release reconciliation workflow."""

from __future__ import annotations

import json
import os
import re
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from http.client import HTTPException, HTTPSConnection
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator

LIMIT = timedelta(hours=24)
PAGE_SIZE = 100
TITLE = "Environment waits over 24 hours"
ISSUE_TITLE = "Release reconciliation: environment waits over 24 hours"
PREVIOUS_ISSUE_TITLE = "Release reconciliation: environment approvals over 24 hours"
# Leave room for links and part markers below GitHub's 65,536-character issue
# limit and 1 MiB per-step summary limit. Count UTF-8 bytes conservatively.
ISSUE_PAGE_BYTES = 60_000
SUMMARY_BYTES = 1_000_000
# Two escaped text cells (at most six bytes per character) still fit in a
# single issue part, even if a workflow has an unusually long display name.
CELL_CHARS = 3000
PART_PREFIX = "<!-- release-environment-waits part "
PART_COMMENT = re.compile(re.escape(PART_PREFIX) + r"([1-9][0-9]{0,8}) -->\n")
API_ERRORS = (OSError, HTTPException, ValueError, KeyError, TypeError)


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "bernstein-reconcile-release",
            "X-GitHub-Api-Version": "2026-03-10",
        }

    def request(self, path: str, *, method: str = "GET", body: dict | None = None) -> tuple[Any, str]:
        with closing(HTTPSConnection("api.github.com", timeout=30)) as conn:
            conn.request(
                method,
                f"/repos/{self.repo}/{path}",
                body=json.dumps(body).encode() if body is not None else None,
                headers={**self.headers, "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            if not 200 <= response.status < 300:
                raise OSError(f"GitHub returned HTTP {response.status} for {method} {path}")
            return json.load(response), response.getheader("Link", "")

    def items(self, path: str, key: str | None = None, **params: str) -> Iterator[dict]:
        query = urlencode({"per_page": PAGE_SIZE, **params})
        while query:
            body, links = self.request(f"{path}?{query}")
            items = body if key is None else body[key]
            if not isinstance(items, list):
                raise ValueError(f"Expected a list from {path}")
            yield from items
            # GitHub can change /repos/owner/name to /repositories/id in Link.
            # Only the pagination query is needed, including opaque cursors.
            link = re.search(r'<([^>]+)>;\s*rel="next"', links)
            query = urlsplit(link[1]).query if link else ""

    def run_url(self, run_id: int) -> str:
        return f"https://github.com/{self.repo}/actions/runs/{run_id}"


def waiting_runs(api: GitHub) -> Iterator[dict]:
    # The filtered endpoint caps each search at 1,000. Continue from the
    # oldest creation second, including ties at the edge of the window.
    filters = {"status": "waiting"}
    seen: set[int] = set()
    while True:
        runs = api.items("actions/runs", "workflow_runs", **filters)
        count = 0
        oldest = None
        for run in islice(runs, 1000) if filters else runs:
            count += 1
            created = run["created_at"]
            oldest = min(oldest, created) if oldest else created
            if run["status"] == "waiting" and run["id"] not in seen:
                seen.add(run["id"])
                yield run
        if count < 1000 or not filters:
            return
        cutoff = f"<={oldest}"
        if filters.get("created") == cutoff:
            # Even one second can exceed the search cap. The unfiltered
            # listing has no search cap and includes older active reruns.
            filters = {}
        else:
            filters["created"] = cutoff


@contextmanager
def record_read_error(errors: list[str], source: str) -> Iterator[None]:
    try:
        yield
    except API_ERRORS as exc:
        errors.append(f"{source}: {exc}")


def waiting_job_urls(api: GitHub, run: dict, errors: list[str]) -> set[str]:
    run_id = int(run["id"])
    if attempt := run.get("run_attempt"):
        jobs = api.items(f"actions/runs/{run_id}/attempts/{int(attempt)}/jobs", "jobs")
    else:
        jobs = api.items(f"actions/runs/{run_id}/jobs", "jobs", filter="latest")
    urls = set()
    with record_read_error(errors, f"Run {run_id}, waiting jobs"):
        for job in jobs:
            if job["status"] == "waiting" and (job.get("html_url") or job.get("id") is not None):
                urls.add(job.get("html_url") or f"{api.run_url(run_id)}/job/{int(job['id'])}")
    return urls


def deployment_wait_start(
    api: GitHub, run: dict, environment: str, waiting_jobs: set[str], errors: list[str]
) -> datetime:
    # Reviewer-only gates usually have a null wait_timer_started_at. A SHA
    # and environment can be shared by several workflows and rerun attempts.
    # Only accept the uninterrupted waiting statuses of a current waiting job.
    starts = []
    label = f"Run {run['id']}, environment {environment}"
    with record_read_error(errors, label):
        for deployment in api.items("deployments", sha=run["head_sha"], environment=environment):
            with record_read_error(errors, f"{label}, deployment {deployment.get('id')}"):
                if deployment["created_at"] < run["created_at"]:
                    continue
                for status in api.items(f"deployments/{int(deployment['id'])}/statuses"):
                    job_url = status.get("log_url") or status.get("target_url")
                    if status["state"] != "waiting" or job_url not in waiting_jobs:
                        break
                    starts.append(datetime.fromisoformat(status["created_at"]))
    if not starts:
        raise ValueError("no waiting deployment status matched a job in the current attempt")
    return min(starts)


@dataclass
class Wait:
    run_id: int
    workflow: str
    environment: str
    since: datetime
    timer_active: bool = False


@dataclass
class Report:
    waits: list[Wait] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def collect(api: GitHub, now: datetime) -> Report:
    report = Report()
    with record_read_error(report.errors, "Waiting run scan"):
        for run in waiting_runs(api):
            run_id = int(run["id"])
            if now - datetime.fromisoformat(run["created_at"]) <= LIMIT:
                continue  # Even the whole run is too young.
            try:
                gates, _ = api.request(f"actions/runs/{run_id}/pending_deployments")
                if not isinstance(gates, list):
                    raise ValueError("expected a list of pending deployments")
            except API_ERRORS as exc:
                report.errors.append(f"Run {run_id}: {exc}")
                continue
            waiting_jobs = None
            for gate in gates:
                label = f"Run {run_id}, pending environment"
                try:
                    environment = gate["environment"]
                    if not isinstance(environment, dict):
                        raise ValueError("missing environment metadata")
                    env = environment.get("name")
                    if not env:
                        raise ValueError(f"environment ID {environment.get('id', 'unknown')} has no name")
                    label = f"Run {run_id}, environment {env}"
                    if timestamp := gate.get("wait_timer_started_at"):
                        since = datetime.fromisoformat(timestamp)
                    else:
                        if waiting_jobs is None:
                            waiting_jobs = waiting_job_urls(api, run, report.errors)
                        since = deployment_wait_start(api, run, env, waiting_jobs, report.errors)
                    age = now - since
                    if age > LIMIT:
                        minutes = gate.get("wait_timer")
                        timer_active = bool(
                            timestamp and isinstance(minutes, int) and age.total_seconds() < 60 * minutes
                        )
                        report.waits.append(Wait(run_id, run.get("name") or run["path"], env, since, timer_active))
                except API_ERRORS as exc:
                    report.errors.append(f"{label}: {exc}")
    return report


def report_pages(api: GitHub, report: Report, now: datetime) -> list[str]:
    title = f"## {TITLE}\n\n"
    if not report.waits and not report.errors:
        return [title + "No workflow runs have waited on an environment for more than 24 hours.\n"]
    if report.errors:
        title += "The scan is incomplete.\n\n"
    header = title + "<table>\n<tr><th>Workflow</th><th>Run</th><th>Environment</th><th>Minimum wait</th></tr>\n"
    footer = "</table>\n"
    rows = []
    for wait in sorted(report.waits, key=lambda w: (w.since, w.run_id, w.environment)):
        cells = (wait.workflow, wait.environment)
        for start in range(0, max(map(len, cells)), CELL_CHARS):
            workflow, environment = (escape(cell[start : start + CELL_CHARS]) for cell in cells)
            if wait.timer_active and start == 0:
                environment += " (wait timer active)"
            rows.append(
                f"<tr><td>{workflow}</td>"
                f'<td><a href="{api.run_url(wait.run_id)}">{wait.run_id}</a></td>'
                f"<td>{environment}</td><td>{now - wait.since}</td></tr>\n"
            )
    for error in report.errors:
        for start in range(0, len(error), CELL_CHARS):
            rows.append(f'<tr><td colspan="4">{escape(error[start : start + CELL_CHARS])}</td></tr>\n')

    pages = []
    current: list[str] = []
    capacity = ISSUE_PAGE_BYTES - len((header + footer).encode())
    remaining = capacity
    for row in rows:
        size = len(row.encode())
        if current and size > remaining:
            pages.append(header + "".join(current) + footer)
            current = []
            remaining = capacity
        current.append(row)
        remaining -= size
    pages.append(header + "".join(current) + footer)
    return pages


def job_summary(report: Report, pages: list[str]) -> str:
    body = "\n".join(pages)
    if len(body.encode()) <= SUMMARY_BYTES:
        return body
    if not report.waits:
        return pages[0] + f"\nShowing the first page of {len(report.errors)} scan errors.\n"

    run_ids = sorted({wait.run_id for wait in report.waits})
    heading = f"## {TITLE}\n\n{len(run_ids)} confirmed overdue runs.\n\n"
    if report.errors:
        heading += f"The scan is incomplete ({len(report.errors)} errors).\n\n"
    heading += "Run IDs: "
    listing = ", ".join(map(str, run_ids))
    room = SUMMARY_BYTES - len((heading + "\n").encode())
    if len(listing) > room:
        listing = listing[: room - len(", …".encode())].rsplit(", ", 1)[0] + ", …"
    return heading + listing + "\n"


def sync_parts(api: GitHub, number: int, pages: list[str], summary_link: str) -> None:
    comments = {}
    for comment in api.items(f"issues/{number}/comments"):
        if (comment.get("user") or {}).get("login") != "github-actions[bot]":
            continue
        match = PART_COMMENT.match(comment.get("body") or "")
        if match:
            part = int(match[1])
            if part >= 2:
                comments[part] = comment
    for part in sorted(set(range(2, len(pages) + 1)) | comments.keys()):
        marker = f"{PART_PREFIX}{part} -->\n"
        if part <= len(pages):
            body = f"{marker}**Part {part} of {len(pages)}**\n\n{pages[part - 1]}\n{summary_link}"
        else:
            body = marker + "This part is no longer in the current report. See the issue body."
        previous = comments.get(part)
        if previous is None:
            api.request(f"issues/{number}/comments", method="POST", body={"body": body})
        elif previous["body"] != body:
            api.request(f"issues/comments/{int(previous['id'])}", method="PATCH", body={"body": body})


def sync_issue(api: GitHub, report: Report, pages: list[str], summary_url: str) -> int | None:
    if report.errors and not report.waits:
        return None  # An incomplete scan cannot clear the previous report.
    # The channel drift job closes issues labelled release-drift when package
    # versions align. The environment report has its own tracking issue.
    issues = api.items("issues", state="open", creator="github-actions[bot]")
    issue = next(
        (i for i in issues if i["title"] in (ISSUE_TITLE, PREVIOUS_ISSUE_TITLE) and "pull_request" not in i), None
    )
    if not report.waits:
        if issue:
            api.request(f"issues/{int(issue['number'])}", method="PATCH", body={"state": "closed"})
        return None

    link = f"[Reconciliation job summary]({summary_url})"
    body = pages[0] + "\n" + link
    if len(pages) > 1:
        body += f"\n\n{PART_PREFIX}1 -->\nPart 1 of {len(pages)}. The numbered comments contain the remaining rows."
    previous = (issue or {}).get("body") or ""
    multipart = len(pages) > 1 or PART_PREFIX in previous
    # A failed POST/PATCH can leave some parts for the next run to finish.
    # Publish the final index only after all numbered comments are in place.
    staging = body
    if multipart:
        staging = pages[0] + f"\n{link}\n\n{PART_PREFIX}1 -->\nUpdating report parts."
    if issue is None:
        issue, _ = api.request("issues", method="POST", body={"title": ISSUE_TITLE, "body": staging})
        previous = staging
    number = int(issue["number"])
    if multipart and previous not in (body, staging):
        api.request(f"issues/{number}", method="PATCH", body={"body": staging})
    if multipart or issue.get("comments", 0):
        sync_parts(api, number, pages, link)
    if body != previous or issue.get("title") == PREVIOUS_ISSUE_TITLE:
        api.request(f"issues/{number}", method="PATCH", body={"title": ISSUE_TITLE, "body": body})
    return number


def main() -> int:
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    now = datetime.now(UTC).replace(microsecond=0)
    report = collect(api, now)
    pages = report_pages(api, report, now)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(job_summary(report, pages))
        summary.flush()
        number = sync_issue(api, report, pages, api.run_url(int(os.environ["GITHUB_RUN_ID"])))
        if number:
            summary.write(f"\n[Full report in issue #{number}](https://github.com/{api.repo}/issues/{number})\n")
    return int(bool(report.errors))


if __name__ == "__main__":
    raise SystemExit(main())
