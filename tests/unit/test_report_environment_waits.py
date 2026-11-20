"""Replay public GitHub REST responses recorded from Bernstein on 2026-11-20.

The cassette retains the fields used by the report and GitHub's Link headers.
A two-item page puts the matching SBOM deployment on page two. Vary the clock
to check the threshold without changing the recorded deployment timestamps.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("report_environment_waits", ROOT / "scripts/report_environment_waits.py")
assert SPEC and SPEC.loader
waits = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = waits
SPEC.loader.exec_module(waits)

RUN_ID = 42034413437
RUN_PATH = f"actions/runs/{RUN_ID}"
RUN_LIST = "actions/runs?per_page=2&status=waiting"
GATES = f"{RUN_PATH}/pending_deployments"
DEPLOYMENTS = "deployments?per_page=2&sha=1191c7a20f39033fdfe387cc76297f42c2f8aebc&environment=release-channels"
ISSUES = "issues?per_page=2&state=open&creator=github-actions%5Bbot%5D"
START = datetime(2026, 11, 5, 13, 17, 10, tzinfo=UTC)
NOW = START + timedelta(days=15)


class Replay(waits.GitHub):
    def __init__(self):
        super().__init__("sipyourdrink-ltd/bernstein", "fixture")
        self.responses = json.loads((ROOT / "tests/fixtures/ci/environment_waits.json").read_text(encoding="utf-8"))
        self.reads = []
        self.writes = []

    def request(self, path, *, method="GET", body=None):
        if method != "GET":
            self.writes.append((method, path, body))
            if method == "POST" and path == "issues":
                return {"number": 7, **body}, ""
            return {}, ""
        self.reads.append(path)
        assert path in self.responses, f"Unexpected request: {path}"
        response = self.responses[path]
        if "error" in response:
            raise OSError(response["error"])
        return response["body"], response.get("link", "")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(waits, "PAGE_SIZE", 2)
    return Replay()


def test_no_waiting_runs(api):
    api.responses[RUN_LIST] = api.responses[RUN_LIST + "&created=2026-11-01"]
    report = waits.collect(api, NOW)
    assert not report.waits and not report.errors
    assert "No workflow runs" in waits.report_pages(api, report, NOW)[0]
    assert api.reads == [RUN_LIST]


def test_overdue_reviewer_only_gate_and_paginated_deployments(api):
    report = waits.collect(api, NOW)
    assert not report.errors
    assert report.waits == [waits.Wait(RUN_ID, "SBOM", "release-channels", START)]
    assert any(path.startswith("deployments?") and "page=2" in path.split("&")[1:] for path in api.reads)
    body = waits.report_pages(api, report, NOW)[0]
    for value in ("SBOM", str(RUN_ID), api.run_url(RUN_ID), "release-channels", "15 days"):
        assert value in body
    assert not api.writes


@pytest.mark.parametrize(("seconds", "count"), [(-1, 0), (0, 0), (1, 1)])
def test_strict_gate_age_even_when_the_run_is_older(api, seconds, count):
    report = waits.collect(api, START + timedelta(days=1, seconds=seconds))
    assert GATES in api.reads
    assert not report.errors
    assert len(report.waits) == count


def test_waiting_run_on_a_later_page_and_two_different_environment_ages(api):
    first = api.responses[RUN_LIST]["body"]["workflow_runs"][0]
    second_page = RUN_LIST + "&page=2"
    api.responses[second_page] = api.responses[RUN_LIST]
    api.responses[RUN_LIST] = {
        "body": {"workflow_runs": [dict(first, id=12)]},
        "link": f'<https://api.github.com/repositories/1188762020/{second_page}>; rel="next"',
    }
    api.responses["actions/runs/12/pending_deployments"] = {"body": []}
    api.responses[GATES]["body"] += [
        {"environment": {"name": "new"}, "wait_timer_started_at": NOW.isoformat()},
        {"environment": {"name": "publish"}, "wait_timer_started_at": START.isoformat()},
    ]
    report = waits.collect(api, NOW)
    assert not report.errors
    assert [w.environment for w in report.waits] == ["release-channels", "publish"]


def test_timer_and_reviewer_waits_are_reported_without_claiming_an_approval(api):
    now = START + timedelta(hours=25)
    api.responses[GATES]["body"].append(
        {
            "environment": {"name": "cooldown"},
            "reviewers": [],
            "wait_timer": 48 * 60,
            "wait_timer_started_at": START.isoformat(),
        }
    )
    report = waits.collect(api, now)
    assert not report.errors
    assert [(w.environment, w.timer_active) for w in report.waits] == [("release-channels", False), ("cooldown", True)]
    pages = waits.report_pages(api, report, now)
    api.responses[ISSUES] = {"body": []}
    waits.sync_issue(api, report, pages, api.run_url(123))
    for body in (waits.job_summary(report, pages), api.writes[0][2]["body"]):
        assert "release-channels" in body and "cooldown (wait timer active)" in body
        assert "approval" not in body.lower()
    assert "approval" not in api.writes[0][2]["title"].lower()


def test_unrelated_deployment_from_a_previous_attempt_cannot_date_this_wait(api):
    original = f"{RUN_PATH}/attempts/1/jobs?per_page=2"
    api.responses[RUN_LIST]["body"]["workflow_runs"][0]["run_attempt"] = 2
    api.responses[original.replace("/1/", "/2/")] = {"body": {"jobs": [{"status": "waiting", "html_url": "new-job"}]}}
    report = waits.collect(api, NOW)
    assert not report.waits
    assert "current attempt" in report.errors[0]
    assert "release-channels" in report.errors[0]
    assert "No workflow runs" not in waits.report_pages(api, report, NOW)[0]


def test_filtered_search_cap_includes_ties_and_older_runs_once(api):
    run = api.responses[RUN_LIST]["body"]["workflow_runs"][0]
    same_second = [dict(run, id=i) for i in range(1000)]
    api.responses[RUN_LIST] = {"body": {"workflow_runs": same_second}}
    cutoff = "&created=%3C%3D2026-11-05T13%3A17%3A07Z"
    api.responses[RUN_LIST + cutoff] = api.responses[RUN_LIST]
    api.responses["actions/runs?per_page=2"] = {
        "body": {"workflow_runs": [same_second[-1], dict(run, id=1000), dict(run, id=1001, status="completed")]}
    }
    assert [r["id"] for r in waits.waiting_runs(api)] == list(range(1001))


def test_tracking_issue_follows_confirmed_results_and_preserves_incomplete_scans(api):
    api.responses[ISSUES] = {"body": [{"title": "Release drift", "number": 5}]}
    overdue = waits.collect(api, NOW)
    body = waits.report_pages(api, overdue, NOW)[0]
    url = api.run_url(123)
    waits.sync_issue(api, overdue, [body], url)
    method, path, created = api.writes.pop()
    assert (method, path, created["title"]) == ("POST", "issues", waits.ISSUE_TITLE)
    assert body in created["body"] and url in created["body"]
    api.responses[ISSUES]["body"].append({"number": 7, **created})
    waits.sync_issue(api, overdue, [body], url)
    assert not api.writes
    waits.sync_issue(api, overdue, [body], api.run_url(124))
    assert api.writes.pop()[:2] == ("PATCH", "issues/7")

    api.responses[GATES] = {"error": "HTTP 503"}
    partial = waits.collect(api, NOW)
    assert partial.errors and "incomplete" in waits.report_pages(api, partial, NOW)[0]
    waits.sync_issue(api, partial, ["incomplete"], url)
    assert not api.writes
    waits.sync_issue(api, waits.Report(), ["clear"], url)
    assert api.writes == [("PATCH", "issues/7", {"state": "closed"})]


def test_existing_tracking_issue_is_reused_under_its_previous_title(api):
    api.responses[ISSUES] = {"body": [{"title": waits.PREVIOUS_ISSUE_TITLE, "number": 17, "body": "Earlier report"}]}
    report = waits.collect(api, NOW)
    pages = waits.report_pages(api, report, NOW)
    assert waits.sync_issue(api, report, pages, api.run_url(123)) == 17
    assert [(method, path, body["title"]) for method, path, body in api.writes] == [
        ("PATCH", "issues/17", waits.ISSUE_TITLE)
    ]


def test_cli_writes_the_summary_before_updating_the_issue(api, monkeypatch, tmp_path):
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_REPOSITORY", api.repo)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GH_TOKEN", "fixture")
    monkeypatch.setattr(waits, "GitHub", lambda repo, token: api)
    api.responses[RUN_LIST] = api.responses[RUN_LIST + "&created=2026-11-01"]
    api.responses[ISSUES] = {"body": []}
    assert waits.main() == 0
    assert "No workflow runs" in summary.read_text()
    api.responses[RUN_LIST] = {"error": "HTTP 503"}
    assert waits.main() == 1
    assert "incomplete" in summary.read_text()
    assert not api.writes


def test_optional_api_fields_preserve_other_confirmed_waits(api):
    run = api.responses[RUN_LIST]["body"]["workflow_runs"][0]
    del run["name"], run["run_attempt"]
    latest_jobs = api.responses[f"{RUN_PATH}/attempts/1/jobs?per_page=2"]
    latest_jobs["body"]["jobs"].append({"id": 123, "status": "waiting", "html_url": None})
    api.responses[f"{RUN_PATH}/jobs?per_page=2&filter=latest"] = latest_jobs
    # An earlier deployment in this list belongs to a different workflow.
    api.responses["deployments/7574727843/statuses?per_page=2"]["body"] = [
        {"state": "waiting", "created_at": "2026-11-01T00:00:00Z"}
    ]
    api.responses[GATES]["body"].insert(0, {"environment": {"id": 123}})
    report = waits.collect(api, NOW)
    assert len(report.errors) == 1 and "environment ID 123" in report.errors[0]
    assert [(w.workflow, w.environment, w.since) for w in report.waits] == [(run["path"], "release-channels", START)]


@pytest.mark.parametrize(
    ("page", "failure_on_next"),
    [
        ("deployments/7574727843/statuses?per_page=2", False),  # Before the matching deployment.
        ("deployments/7574621062/statuses?per_page=2", False),  # After the matching deployment.
        (DEPLOYMENTS + "&page=3", False),  # After the deployment list yielded the match on page two.
        ("deployments/7574621553/statuses?per_page=2", True),  # After a matching status on page one.
        (f"{RUN_PATH}/attempts/1/jobs?per_page=2", True),  # After a waiting job on page one.
    ],
)
def test_partial_deployment_reads_preserve_confirmed_waits(api, page, failure_on_next):
    if failure_on_next:
        next_page = page + "&page=2"
        api.responses[page]["link"] = f'<https://api.github.com/repositories/1188762020/{next_page}>; rel="next"'
        page = next_page
    api.responses[page] = {"error": "HTTP 503"}
    report = waits.collect(api, NOW)
    assert report.waits == [waits.Wait(RUN_ID, "SBOM", "release-channels", START)]
    assert len(report.errors) == 1 and "HTTP 503" in report.errors[0]
    pages = waits.report_pages(api, report, NOW)
    api.responses[ISSUES] = {"body": []}
    assert waits.sync_issue(api, report, pages, api.run_url(123)) == 7
    for body in (waits.job_summary(report, pages), api.writes[0][2]["body"]):
        assert "incomplete" in body and "15 days" in body and str(RUN_ID) in body


def test_long_escaped_fields_fit_without_losing_text(api):
    name = "'" * 18_000
    error = '"' * 18_000
    report = waits.Report([waits.Wait(RUN_ID, name, "release-channels", START)], [error])
    pages = waits.report_pages(api, report, NOW)
    assert all(len(page.encode()) <= waits.ISSUE_PAGE_BYTES for page in pages)
    assert "".join(pages).count("&#x27;") == len(name)
    assert "".join(pages).count("&quot;") == len(error)


def test_summary_keeps_distinct_run_ids_when_full_rows_do_not_fit(api, monkeypatch):
    run_ids = list(range(1000, 1040))
    report = waits.Report(
        [waits.Wait(i, "<release>" * 50, env, START) for i in run_ids for env in ("one", "two")], ["HTTP 503"]
    )
    pages = waits.report_pages(api, report, NOW)
    monkeypatch.setattr(waits, "SUMMARY_BYTES", 500)
    summary = waits.job_summary(report, pages)
    assert "40 confirmed overdue runs" in summary and "incomplete" in summary
    assert summary.split("Run IDs: ")[1].strip() == ", ".join(map(str, run_ids))
    monkeypatch.setattr(waits, "SUMMARY_BYTES", 180)
    shortened = waits.job_summary(report, pages)
    assert len(shortened.encode()) <= 180 and shortened.endswith(", …\n")
    shown = list(map(int, shortened.split("Run IDs: ")[1].removesuffix(", …\n").split(", ")))
    assert 0 < len(shown) < len(run_ids) and shown == run_ids[: len(shown)]


def test_sync_parts_recognizes_only_bounded_bot_markers(api, monkeypatch):
    def comment(comment_id, author, part):
        return {"id": comment_id, "user": {"login": author}, "body": f"{waits.PART_PREFIX}{part} -->\nPrevious"}

    bot = "github-actions[bot]"
    matcher = Mock(wraps=waits.PART_COMMENT)
    monkeypatch.setattr(waits, "PART_COMMENT", matcher)
    maintainer = comment(13, "maintainer", "2")
    maintainer["body"] += " from the maintainer"
    api.responses["issues/7/comments?per_page=2"] = {
        "body": [
            comment(10, bot, "2"),
            comment(11, bot, "02"),
            comment(12, bot, "1000000000"),
            maintainer,
        ]
    }
    waits.sync_parts(api, 7, ["first", "second"], "summary")
    assert [(method, path) for method, path, _ in api.writes] == [("PATCH", "issues/comments/10")]
    assert (maintainer["body"],) not in [call.args for call in matcher.match.call_args_list]


@pytest.mark.parametrize("count", [300, 5000])
def test_large_report_keeps_every_run_in_bounded_issue_parts(api, monkeypatch, tmp_path, count):
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_REPOSITORY", api.repo)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GH_TOKEN", "fixture")
    monkeypatch.setattr(waits, "GitHub", lambda repo, token: api)
    report = waits.Report([waits.Wait(i, "Publish Homebrew Formula", "release-channels", START) for i in range(count)])
    monkeypatch.setattr(waits, "collect", lambda api, now: report)

    # Freeze the report age across the idempotency check below.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(waits, "datetime", Clock)
    api.responses[ISSUES] = {"body": []}
    comments_path = "issues/7/comments?per_page=2"
    api.responses[comments_path] = {"body": []}
    assert waits.main() == 0
    issue = {"title": waits.ISSUE_TITLE, **api.writes[-1][2]}
    posted = [b["body"] for method, path, b in api.writes if (method, path) == ("POST", "issues/7/comments")]
    if count == 300:
        assert not posted and [entry[:2] for entry in api.writes] == [("POST", "issues")]
    else:
        assert posted and "Updating report parts" in api.writes[0][2]["body"]
        assert api.writes[-1][:2] == ("PATCH", "issues/7")
    bodies = [issue["body"], *posted]
    assert all(len(body.encode()) < 65_536 for body in bodies)
    linked_ids = re.findall(r'/actions/runs/(\d+)">', "\n".join(bodies))
    assert list(map(int, linked_ids)) == list(range(count))
    summary_body = summary.read_text()
    assert len(summary_body.encode()) < 1024 * 1024
    assert f"https://github.com/{api.repo}/issues/7" in summary_body
    summary_ids = re.findall(r'/actions/runs/(\d+)">', summary_body)
    assert list(map(int, summary_ids)) == list(range(count))

    # Replay GitHub's stored issue and comments. A retry reuses the same parts,
    # and a smaller report retires only the bot's old continuation comments.
    api.responses[ISSUES]["body"] = [{"number": 7, "comments": len(posted) + 1, **issue}]
    comments = [
        {"id": i, "user": {"login": "github-actions[bot]"}, "body": body} for i, body in enumerate(posted, start=10)
    ]
    note = posted[0] if posted else "Maintainer note"
    api.responses[comments_path]["body"] = [*comments, {"id": 1, "user": {"login": "maintainer"}, "body": note}]
    api.writes.clear()
    assert waits.main() == 0
    assert not api.writes
    report.waits = report.waits[:1]
    assert waits.main() == 0
    assert {p for _, p, _ in api.writes} == {"issues/7", *(f"issues/comments/{c['id']}" for c in comments)}
