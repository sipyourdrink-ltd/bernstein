"""Structural assertions for cifuzz-weekly's name/trigger agreement.

The filename, the ``name:``, and the concurrency group all said "PR"
while the header explains the per-PR trigger was deliberately dropped in
favor of a weekly batch session, to keep OSSF Scorecard's Fuzzing check
satisfied without a per-push cost (issue #3950). None of the three may
promise a PR-time run this workflow no longer performs: an operator
grepping the workflow directory for the fuzzing lane should not have to
open the file to learn when it fires.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS_DIR / "cifuzz-weekly.yml"


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_the_old_pr_named_filename_is_gone() -> None:
    assert not (WORKFLOWS_DIR / "cifuzz-pr.yml").exists(), (
        "cifuzz-pr.yml must not coexist with cifuzz-weekly.yml - rename in place, don't fork the workflow"
    )


def test_on_block_has_no_pull_request_trigger() -> None:
    """The per-PR trigger was deliberately dropped; the name must agree."""
    doc = _doc()
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    assert "pull_request" not in triggers, (
        "the per-PR trigger was deliberately dropped for a weekly schedule "
        "(see header); reintroducing it without updating name: reopens #3950"
    )


def test_display_name_does_not_claim_a_pr_trigger() -> None:
    name = str(_doc().get("name", ""))
    assert "PR" not in name, f"name: {name!r} still advertises a per-PR run this workflow does not perform"


def test_concurrency_group_matches_the_new_name() -> None:
    group = str(_doc().get("concurrency", {}).get("group", ""))
    assert "cifuzz-pr" not in group, f"concurrency group {group!r} still carries the old cifuzz-pr branding"
