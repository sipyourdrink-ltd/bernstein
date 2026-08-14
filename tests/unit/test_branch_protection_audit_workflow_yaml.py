"""Structural assertions for the branch protection audit workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TypedDict, cast

import yaml


class WorkflowJob(TypedDict, total=False):
    name: object
    permissions: dict[str, object]
    env: dict[str, object]
    steps: list[object]


class WorkflowFile(TypedDict, total=False):
    on: object
    permissions: object
    jobs: dict[str, WorkflowJob]


WORKFLOW = Path(".github/workflows/branch-protection-audit.yml")
CANARY = Path(".github/workflows/required-check-canary.yml")
REQUIRED_CONTEXTS = {"CI gate"}


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> WorkflowFile:
    return cast("WorkflowFile", yaml.safe_load(_workflow_text()))


def _on(workflow: WorkflowFile) -> dict[str, object]:
    # PyYAML 1.1 parses bare ``on:`` as boolean True; tolerate both.
    raw_workflow = cast("dict[object, object]", workflow)
    value = raw_workflow.get(True, workflow.get("on"))
    assert isinstance(value, dict), "workflow must define triggers"
    return cast("dict[str, object]", value)


def _audit_job(workflow: WorkflowFile) -> WorkflowJob:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict), "workflow must define jobs"
    job = jobs.get("audit")
    assert isinstance(job, dict), "workflow must define an audit job"
    return job


def _canary_expected_contexts() -> set[str]:
    canary = cast("dict[str, object]", yaml.safe_load(CANARY.read_text(encoding="utf-8")))
    jobs = canary.get("jobs")
    assert isinstance(jobs, dict)
    job_map = cast("dict[str, object]", jobs)
    verify = job_map.get("verify")
    assert isinstance(verify, dict)
    verify_map = cast("dict[str, object]", verify)
    steps = verify_map.get("steps")
    assert isinstance(steps, list)
    for step_value in cast("list[object]", steps):
        if not isinstance(step_value, dict):
            continue
        step = cast("dict[str, object]", step_value)
        if step.get("name") != "Verify required-check invariants":
            continue
        env = step.get("env")
        assert isinstance(env, dict)
        env_map = cast("dict[str, object]", env)
        raw = env_map.get("BRANCH_PROTECTION_CONTEXTS_JSON")
        assert isinstance(raw, str)
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
        parsed_items = cast("list[object]", parsed)
        assert all(isinstance(item, str) for item in parsed_items)
        return {cast("str", item) for item in parsed_items}
    raise AssertionError("canary must define BRANCH_PROTECTION_CONTEXTS_JSON")


def test_branch_protection_audit_workflow_exists() -> None:
    assert WORKFLOW.exists(), "branch-protection-audit.yml is missing"


def test_branch_protection_audit_is_scheduled_and_manual_only() -> None:
    triggers = _on(_workflow())
    assert "schedule" in triggers, "audit must run on a schedule"
    assert "workflow_dispatch" in triggers, "audit must be manually runnable"
    assert "pull_request" not in triggers, "live branch protection audit must not run on untrusted PR events"
    assert "push" not in triggers, "live branch protection audit must not run on every push"

    schedule = triggers["schedule"]
    assert isinstance(schedule, list)
    assert schedule, "scheduled audit must include at least one cron entry"


def test_branch_protection_audit_permissions_are_read_only() -> None:
    workflow = _workflow()
    assert workflow.get("permissions") in ({}, "{}"), "workflow-level permissions must be default-deny"
    assert _audit_job(workflow).get("permissions") == {"contents": "read"}


def test_branch_protection_audit_reads_main_protection_without_mutating() -> None:
    workflow_text = _workflow_text()
    assert "gh api" in workflow_text, "audit must read live branch protection through the GitHub API"
    assert "repos/${GITHUB_REPOSITORY}/branches/main/protection" in workflow_text
    assert not re.search(r"\b(?:POST|PUT|PATCH|DELETE)\b", workflow_text), "audit workflow must not mutate settings"
    assert "--method" not in workflow_text
    assert " -X " not in workflow_text


def test_branch_protection_audit_compares_live_contexts_with_canary_expectations() -> None:
    workflow_text = _workflow_text()
    canary_text = CANARY.read_text(encoding="utf-8")

    assert _canary_expected_contexts() == REQUIRED_CONTEXTS
    assert "BRANCH_PROTECTION_CONTEXTS_JSON" in canary_text
    assert "required-check-canary.yml" in workflow_text
    assert "BRANCH_PROTECTION_CONTEXTS_JSON" in workflow_text
    assert "required_status_checks" in workflow_text
    assert "contexts" in workflow_text
    assert "checks" in workflow_text
    assert "missing" in workflow_text
    assert "extra" in workflow_text


def test_branch_protection_audit_fails_when_live_protection_is_unreadable() -> None:
    workflow_text = _workflow_text()
    assert "::error::Unable to read live branch protection for main" in workflow_text
    assert "exit 1" in workflow_text
    assert "continue-on-error: true" not in workflow_text
