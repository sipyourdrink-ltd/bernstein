"""Consistency assertions on ``docs/operations/merge-queue.md``.

The runbook is the declared source of truth for the merge-queue ruleset:
the shipped ruleset is reconciled *to* the document, not the other way
round. That only holds if the document cannot contradict itself, so the
two places it states the configuration - the **Tunables** table and the
copy-pasteable ``gh api`` payload in **Enable** - are diffed here.

Also guarded:

* the required-status contexts the runbook tells the operator to put on
  the ruleset match the ones branch protection enforces at queue entry,
  so the two gates cannot silently diverge;
* claims that went stale once ``review-bot-ack.yml`` gained a
  ``merge_group`` trigger, and once ``main-red-guard.yml`` was folded
  into ``pr-policy.yml``, do not creep back in.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

RUNBOOK = Path("docs/operations/merge-queue.md")
MERGE_GATE = Path("docs/operations/merge-gate.md")

# Mirrors repos/sipyourdrink-ltd/bernstein/branches/main/protection
# -> required_status_checks.contexts (app_id 15368 == GitHub Actions).
BRANCH_PROTECTION_CONTEXTS = ("CI gate", "review-bot-ack")
ACTIONS_INTEGRATION_ID = 15368

# Tunable -> the value the Enable payload must carry. Sourced from the
# runbook's own table at test time; this map only fixes the key set so a
# silently dropped row is caught too.
EXPECTED_TUNABLES = (
    "merge_method",
    "grouping_strategy",
    "max_entries_to_build",
    "min_entries_to_merge",
    "max_entries_to_merge",
    "min_entries_to_merge_wait_minutes",
    "check_response_timeout_minutes",
)


@pytest.fixture(scope="module")
def runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def enable_payload(runbook_text: str) -> dict[str, Any]:
    """The JSON heredoc the operator pipes into `gh api -X PUT .../rulesets`."""
    match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", runbook_text, re.DOTALL)
    assert match, (
        "merge-queue.md must keep a copy-pasteable ruleset payload in a "
        "`<<'JSON' ... JSON` heredoc; the operator flip is meant to be "
        "mechanical, not reconstructed from prose."
    )
    return json.loads(match.group(1))


@pytest.fixture(scope="module")
def documented_tunables(runbook_text: str) -> dict[str, str]:
    """The `Correct` column of the Tunables table, keyed by parameter."""
    tunables: dict[str, str] = {}
    for line in runbook_text.splitlines():
        row = re.match(r"^\|\s*`([a-z_]+)`\s*\|([^|]*)\|([^|]*)\|", line)
        if row:
            tunables[row.group(1)] = row.group(3).strip().strip("*").strip("`")
    return tunables


def _rule(payload: dict[str, Any], rule_type: str) -> dict[str, Any]:
    for rule in payload.get("rules", []):
        if rule.get("type") == rule_type:
            params = rule.get("parameters")
            assert isinstance(params, dict)
            return params
    raise AssertionError(f"enable payload is missing a `{rule_type}` rule")


def test_runbook_exists() -> None:
    assert RUNBOOK.exists(), "the merge-queue runbook is referenced from merge-gate.md"


def test_tunables_table_lists_every_merge_queue_parameter(
    documented_tunables: dict[str, str],
) -> None:
    missing = [k for k in EXPECTED_TUNABLES if k not in documented_tunables]
    assert not missing, (
        f"Tunables table is missing rows for {missing}. Every merge_queue rule "
        "parameter must be documented with its rationale, or the shipped ruleset "
        "has nothing to be reconciled against."
    )


@pytest.mark.parametrize("param", EXPECTED_TUNABLES)
def test_enable_payload_matches_tunables_table(
    param: str,
    documented_tunables: dict[str, str],
    enable_payload: dict[str, Any],
) -> None:
    """The prose table and the copy-paste payload must not disagree.

    An operator following the runbook reads one and pastes the other. If they
    drift, the queue ships with tunables nobody reviewed - which is exactly
    the drift this document was written to close.
    """
    documented = documented_tunables[param]
    applied = _rule(enable_payload, "merge_queue")[param]
    assert str(applied) == documented, (
        f"`{param}`: Tunables table says {documented!r} but the Enable payload "
        f"applies {applied!r}. Fix whichever is wrong - the table is the "
        "source of truth for the value, the payload for the syntax."
    )


def test_batch_size_is_pinned_to_one(enable_payload: dict[str, Any]) -> None:
    """`max_entries_to_merge` > 1 breaks the release path.

    With N entries merged per push, the base branch advances N commits in one
    push event that reports only the last SHA. auto-release keys on that SHA,
    so a version bump anywhere but last is skipped with no error.

    The diff planner is no longer part of this: `determine-changes` classifies
    a merge group against `github.event.merge_group.base_sha`, so it already
    sees every entry in a multi-entry group.
    """
    params = _rule(enable_payload, "merge_queue")
    assert params["max_entries_to_merge"] == 1, (
        "max_entries_to_merge must stay 1 until the release gate stops keying "
        "on the push head SHA. See merge-queue.md :: Auto-release through the queue."
    )


def test_enable_payload_requires_every_branch_protection_context(
    enable_payload: dict[str, Any],
) -> None:
    """The queue must enforce the same contexts as queue entry does."""
    checks = _rule(enable_payload, "required_status_checks")["required_status_checks"]
    contexts = {c["context"] for c in checks}
    assert contexts == set(BRANCH_PROTECTION_CONTEXTS), (
        f"ruleset payload requires {sorted(contexts)} but branch protection "
        f"requires {sorted(BRANCH_PROTECTION_CONTEXTS)} at queue entry. A context "
        "required to enter the queue but not to leave it means the queue enforces "
        "less than the PR gate did."
    )
    for check in checks:
        assert check.get("integration_id") == ACTIONS_INTEGRATION_ID, (
            f"{check['context']!r} must be pinned to the GitHub Actions app "
            f"(integration_id {ACTIONS_INTEGRATION_ID}) so an unrelated app "
            "cannot satisfy a required context."
        )


def test_enable_payload_does_not_activate_the_ruleset(
    enable_payload: dict[str, Any],
) -> None:
    """Step 1 configures; Step 3 enables. Keep them separate.

    Enabling is a shared-state change that serialises every open PR. Folding it
    into the rule-writing call means a typo in the rules and the flip land
    together, with no chance to verify the payload first.
    """
    assert enable_payload.get("enforcement") == "disabled", (
        "the Step 1 payload must keep `enforcement: disabled`; the flip is a separate, reviewable call in Step 3."
    )


@pytest.mark.parametrize("doc", [RUNBOOK, MERGE_GATE])
def test_no_stale_review_bot_ack_claim(doc: Path) -> None:
    """`review-bot-ack` does run on merge_group; the old warning must not return."""
    text = doc.read_text(encoding="utf-8")
    stale = re.search(
        r"review-bot-ack[^.\n]{0,80}(triggers only on|runs on `pull_request` only)",
        text,
    )
    assert stale is None, (
        f"{doc} repeats the stale claim that review-bot-ack cannot report on a "
        f"merge_group ref ({stale.group(0)!r} if matched). review-bot-ack.yml "
        "declares `merge_group: {}` and carries a pass-through emitter."
    )


@pytest.mark.parametrize("doc", [RUNBOOK, MERGE_GATE])
def test_no_reference_to_retired_main_red_guard_workflow(doc: Path) -> None:
    """main-red-guard is a step in pr-policy.yml, not a standalone workflow."""
    text = doc.read_text(encoding="utf-8")
    assert "main-red-guard.yml" not in text, (
        f"{doc} refers to `main-red-guard.yml`, which no longer exists - the "
        "advisory was consolidated into `.github/workflows/pr-policy.yml`. "
        "Point operator steps at pr-policy.yml instead."
    )


def test_runbook_documents_the_release_path_through_the_queue(
    runbook_text: str,
) -> None:
    """The highest-risk question must have a written answer, not an assumption."""
    assert "gh-readonly-queue/" in runbook_text, (
        "the runbook must name the queue's ephemeral ref prefix; it is why the "
        "workflow_run listener does not fire on speculative queue builds"
    )
    assert "github-merge-queue[bot]" in runbook_text, (
        "the runbook must record that the post-queue push to main is emitted by "
        "github-merge-queue[bot] and does start push-triggered workflows - that "
        "is the evidence the auto-release chain survives the queue"
    )
