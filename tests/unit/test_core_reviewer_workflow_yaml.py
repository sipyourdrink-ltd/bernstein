"""Safety and offline routing regressions for one-core-reviewer requests."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/core-reviewer-review.yml"


def load_workflow() -> dict:
    assert WORKFLOW.is_file(), "the core reviewer router must exist"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def routing_job() -> dict:
    jobs = load_workflow()["jobs"]
    assert len(jobs) == 1
    return next(iter(jobs.values()))


def test_trigger_routes_all_ready_prs_including_forks() -> None:
    workflow = load_workflow()
    on = workflow.get("on", workflow.get(True))
    assert on == {"pull_request_target": {"types": ["opened", "reopened", "ready_for_review"]}}
    assert routing_job()["if"] == "${{ !github.event.pull_request.draft }}"


def test_privileged_job_has_only_review_request_permissions() -> None:
    assert load_workflow()["permissions"] == {}
    assert routing_job()["permissions"] == {"pull-requests": "write"}


def test_privileged_job_never_executes_pr_content() -> None:
    job = routing_job()
    actions = [step["uses"] for step in job["steps"] if "uses" in step]
    assert len(actions) == 1
    assert re.fullmatch(r"step-security/harden-runner@[0-9a-f]{40}", actions[0])
    assert job["steps"][0]["uses"] == actions[0]
    for step in job["steps"]:
        if "run" in step:
            assert "${{" not in step["run"]
    assert job["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert job["env"]["PR_AUTHOR"] == "${{ github.event.pull_request.user.login }}"
    assert job["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert job["env"]["REPO"] == "${{ github.repository }}"


def test_codeowners_has_no_catch_all_but_keeps_protected_paths() -> None:
    lines = (ROOT / ".github/CODEOWNERS").read_text().splitlines()
    assert not any(line.split()[0] == "*" for line in lines if line.strip() and not line.startswith("#"))
    assert "/.github/ @chernistry" in lines


def route(*, roster=None, pages=None, author="contributor", requested=(), draft=False, fail_history=False):
    job = routing_job()
    blocks = [step["run"] for step in job["steps"] if "run" in step]
    assert len(blocks) == 1
    match = re.search(r"python3 - <<'PY'\n(.*?)\nPY\s*$", blocks[0], re.DOTALL)
    assert match, "exercise the actual inline router without network or a checkout"
    posts = []

    def gh_run(args, **kwargs):
        assert args[:2] == ["gh", "api"]
        assert kwargs["check"] and kwargs["capture_output"] and kwargs["text"]
        if "--method" in args:
            assert args[args.index("--method") + 1] == "POST"
            assert "repos/owner/repo/pulls/42/requested_reviewers" in args
            assert args[-2:] == ["--input", "-"]
            posts.append(json.loads(kwargs["input"]))
            result = "{}"
        elif any("/contents/" in arg for arg in args):
            assert "repos/owner/repo/contents/.github/quorum-roster.toml?ref=base-sha" in args
            assert "Accept: application/vnd.github.raw+json" in args
            result = roster if roster is not None else 'core_reviewers = ["Alpha", "Beta", "Gamma"]'
        elif "repos/owner/repo/issues/events?per_page=100" in args:
            assert "--paginate" in args and "--slurp" in args
            if fail_history:
                raise subprocess.CalledProcessError(1, args)
            result = json.dumps(pages or [])
        else:
            assert args == ["gh", "api", "repos/owner/repo/pulls/42"]
            result = json.dumps({"draft": draft, "requested_reviewers": [{"login": login} for login in requested]})
        return subprocess.CompletedProcess(args, 0, stdout=result)

    env = {"REPO": "owner/repo", "PR_NUMBER": "42", "PR_AUTHOR": author, "BASE_SHA": "base-sha"}
    with patch.dict("os.environ", env), patch("subprocess.run", side_effect=gh_run):
        exec(compile(match[1], str(WORKFLOW), "exec"), {})
    return posts


def event(login, when, kind="review_requested"):
    return {"event": kind, "requested_reviewer": {"login": login}, "created_at": when}


def test_requests_exactly_one_using_latest_request_across_all_pages(capsys) -> None:
    pages = [
        [event("Alpha", "2026-03-01T00:00:00Z"), event("Beta", "2026-02-01T00:00:00Z")],
        [event("Alpha", "2026-01-01T00:00:00Z"), event("Gamma", "2026-04-01T00:00:00Z")],
    ]
    assert route(pages=pages) == [{"reviewers": ["Beta"]}]
    log = capsys.readouterr().out
    assert "Beta" in log and "2026-02-01T00:00:00Z" in log


@pytest.mark.parametrize("author", ["Alpha", "aLPHa"])
def test_never_requests_author_even_with_different_login_case(author) -> None:
    assert route(author=author) == [{"reviewers": ["Beta"]}]


def test_never_requested_reviewers_win_with_deterministic_ties() -> None:
    pages = [[event("Alpha", "2026-01-01T00:00:00Z"), event("Beta", "2026-05-01T00:00:00Z", "review_request_removed")]]
    assert route(pages=pages) == [{"reviewers": ["Beta"]}]


def test_roster_is_the_only_source_of_candidates() -> None:
    assert route(roster='core_reviewers = ["NewReviewer"]\ncommitters = ["Alpha"]') == [{"reviewers": ["NewReviewer"]}]


@pytest.mark.parametrize("term", ['expires = "2000-01-01"', 'granted = "2000-01-01"'])
def test_roster_terms_do_not_route_to_expired_core_reviewers(term) -> None:
    roster = 'core_reviewers = [{ login = "Alpha", ' + term + ' }, { login = "Beta", expires = "9999-12-31" }]'
    assert route(roster=roster) == [{"reviewers": ["Beta"]}]


@pytest.mark.parametrize("roster", ["core_reviewers = []", 'core_reviewers = ["contributor"]'])
def test_no_eligible_reviewer_makes_no_request(roster) -> None:
    assert route(roster=roster) == []


def test_reopened_pr_does_not_accumulate_pending_core_requests() -> None:
    assert route(requested=["bETA"]) == []


def test_non_core_request_does_not_replace_core_routing() -> None:
    assert route(requested=["docs-steward"]) == [{"reviewers": ["Alpha"]}]


def test_pr_converted_to_draft_since_event_is_skipped() -> None:
    assert route(draft=True) == []


def test_history_failure_cannot_silently_bias_selection() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        route(fail_history=True)
