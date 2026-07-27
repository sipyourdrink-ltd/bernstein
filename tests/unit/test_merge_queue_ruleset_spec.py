"""Guards on the intended merge-queue ruleset and the drift verifier.

`docs/operations/merge-queue.md` has been the only statement of what the
`main-merge-queue` ruleset should contain, and prose does not compare
itself to anything. The shipped ruleset drifted from it on two axes that
each break the queue in a different direction, and both survived because
nothing read the live settings back:

    * `required_status_checks` carries only `CI gate`. Branch protection
      requires `CI gate` *and* `review-bot-ack` to enter the queue, so a
      queue configured this way merges without the gate it enforced at
      entry.
    * `check_response_timeout_minutes` is 30. Every measured CI run on
      `main` takes longer, so every queue entry is ejected as timed out.

`docs/operations/merge-queue-ruleset.json` is now the machine-readable
source of truth - it is the exact body the runbook's Step 1 PUTs - and
`scripts/verify_merge_queue_ruleset.py` diffs the live ruleset against
it. These tests pin the invariants that make the flip safe and prove the
verifier reports the real shipped ruleset as drifted.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from verify_merge_queue_ruleset import (
    diff_ruleset,
    load_intended,
    merge_queue_parameters,
    required_contexts,
)

RUNBOOK = Path("docs/operations/merge-queue.md")
SPEC = Path("docs/operations/merge-queue-ruleset.json")

# Mirrors repos/sipyourdrink-ltd/bernstein/branches/main/protection
# -> required_status_checks.contexts. A context required to ENTER the
# queue but not to MERGE from it is a gate the queue silently drops.
BRANCH_PROTECTION_CONTEXTS = ("CI gate", "review-bot-ack")

# Floor for `check_response_timeout_minutes`, in minutes.
#
# Measured 2026-07-27 over the last 40 concluded `CI` runs on `main`
# (`created_at` -> `updated_at`, which is what the queue's own timeout
# measures because it includes runner-pool wait): p50 110, p90 224,
# max 243, and 0 of 40 finished inside 30 minutes. A queue entry runs the
# same full-matrix shape a push to `main` does, so this distribution is
# the one the timeout has to cover.
MEASURED_TIMEOUT_FLOOR_MINUTES = 240

# The ruleset as `gh api repos/sipyourdrink-ltd/bernstein/rulesets/16719298`
# returned it on 2026-07-27, trimmed to the fields the verifier reads.
# Captured verbatim so the drift the verifier must catch is the real one
# and not a hypothetical.
LIVE_RULESET_2026_07_27: dict[str, Any] = {
    "id": 16719298,
    "name": "main-merge-queue",
    "target": "branch",
    "enforcement": "disabled",
    "conditions": {"ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}},
    "rules": [
        {
            "type": "merge_queue",
            "parameters": {
                "merge_method": "SQUASH",
                "max_entries_to_build": 1,
                "min_entries_to_merge": 1,
                "max_entries_to_merge": 1,
                "min_entries_to_merge_wait_minutes": 0,
                "grouping_strategy": "ALLGREEN",
                "check_response_timeout_minutes": 30,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "do_not_enforce_on_create": True,
                "required_status_checks": [{"context": "CI gate", "integration_id": 15368}],
            },
        },
    ],
}


@pytest.fixture(scope="module")
def intended() -> dict[str, Any]:
    return load_intended(SPEC)


def _mutate(base: dict[str, Any], rule_type: str, key: str, value: Any) -> dict[str, Any]:
    """Deep-copy `base` and overwrite one merge-queue parameter."""
    clone = json.loads(json.dumps(base))
    for rule in clone["rules"]:
        if rule["type"] == rule_type:
            rule["parameters"][key] = value
    return clone


# --------------------------------------------------------------------------
# The spec file itself
# --------------------------------------------------------------------------


def test_spec_is_the_runbook_payload_verbatim(intended: dict[str, Any]) -> None:
    """The JSON file and the runbook heredoc may not become two truths.

    `test_merge_queue_runbook_docs.py` already ties the Tunables table to
    the heredoc. This link extends that chain to the file the verifier
    reads, so all three move together or the suite goes red.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", text, re.DOTALL)
    assert match, "merge-queue.md must keep the copy-pasteable ruleset payload"
    assert json.loads(match.group(1)) == intended, (
        "docs/operations/merge-queue-ruleset.json and the runbook's Step 1 "
        "payload disagree; the ruleset would be reconciled to whichever the "
        "operator happened to read"
    )


def test_spec_requires_every_branch_protection_context(intended: dict[str, Any]) -> None:
    """A context required at queue entry must also be required at merge."""
    assert required_contexts(intended) == sorted(BRANCH_PROTECTION_CONTEXTS)


def test_spec_pins_single_entry_merges(intended: dict[str, Any]) -> None:
    """`max_entries_to_merge` > 1 silently skips releases.

    auto-release keys on the push head SHA, so a version bump that is not
    the last entry in a merged batch produces green CI, no tag, no
    publish and no error.
    """
    assert merge_queue_parameters(intended)["max_entries_to_merge"] == 1


def test_spec_timeout_covers_the_measured_ci_distribution(intended: dict[str, Any]) -> None:
    """Below the real CI wall time the queue ejects entries instead of merging."""
    timeout = merge_queue_parameters(intended)["check_response_timeout_minutes"]
    assert timeout >= MEASURED_TIMEOUT_FLOOR_MINUTES


def test_spec_keeps_enforcement_disabled(intended: dict[str, Any]) -> None:
    """Step 1 corrects the rules; Step 3 flips enforcement separately.

    A spec carrying `enforcement: active` would turn the corrective PUT
    into the flip itself, which is the one thing the runbook orders apart.
    """
    assert intended["enforcement"] == "disabled"


# --------------------------------------------------------------------------
# The verifier
# --------------------------------------------------------------------------


def test_verifier_accepts_a_live_ruleset_matching_the_spec(intended: dict[str, Any]) -> None:
    live = json.loads(json.dumps(intended))
    live["id"] = 16719298
    assert diff_ruleset(live, intended) == []


def test_verifier_reports_the_shipped_ruleset_as_drifted(intended: dict[str, Any]) -> None:
    """The real 2026-07-27 ruleset must be rejected, naming both faults."""
    drifts = diff_ruleset(LIVE_RULESET_2026_07_27, intended)
    fields = {d.field for d in drifts}
    assert "required_status_checks" in fields
    assert "check_response_timeout_minutes" in fields


def test_verifier_catches_a_dropped_required_context(intended: dict[str, Any]) -> None:
    live = json.loads(json.dumps(intended))
    for rule in live["rules"]:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = [{"context": "CI gate", "integration_id": 15368}]
    drifts = diff_ruleset(live, intended)
    assert [d.field for d in drifts] == ["required_status_checks"]
    assert "review-bot-ack" in drifts[0].detail


def test_verifier_catches_a_timeout_below_the_spec(intended: dict[str, Any]) -> None:
    live = _mutate(intended, "merge_queue", "check_response_timeout_minutes", 30)
    drifts = diff_ruleset(live, intended)
    assert [d.field for d in drifts] == ["check_response_timeout_minutes"]


def test_verifier_catches_a_multi_entry_merge(intended: dict[str, Any]) -> None:
    live = _mutate(intended, "merge_queue", "max_entries_to_merge", 5)
    drifts = diff_ruleset(live, intended)
    assert [d.field for d in drifts] == ["max_entries_to_merge"]


def test_verifier_catches_a_changed_merge_method(intended: dict[str, Any]) -> None:
    live = _mutate(intended, "merge_queue", "merge_method", "MERGE")
    drifts = diff_ruleset(live, intended)
    assert [d.field for d in drifts] == ["merge_method"]


def test_verifier_catches_a_missing_merge_queue_rule(intended: dict[str, Any]) -> None:
    live = json.loads(json.dumps(intended))
    live["rules"] = [r for r in live["rules"] if r["type"] != "merge_queue"]
    drifts = diff_ruleset(live, intended)
    assert [d.field for d in drifts] == ["merge_queue"]


def test_verifier_catches_a_missing_required_status_checks_rule(
    intended: dict[str, Any],
) -> None:
    live = json.loads(json.dumps(intended))
    live["rules"] = [r for r in live["rules"] if r["type"] != "required_status_checks"]
    drifts = diff_ruleset(live, intended)
    assert [d.field for d in drifts] == ["required_status_checks"]


def test_drift_carries_machine_readable_expected_and_actual(intended: dict[str, Any]) -> None:
    """`detail` is prose for humans; `expected`/`actual` are for callers.

    The report prints both, and a caller generating a corrective payload
    needs the values rather than the sentence about them.
    """
    live = _mutate(intended, "merge_queue", "check_response_timeout_minutes", 30)
    (drift,) = diff_ruleset(live, intended)
    assert drift.actual == 30
    assert drift.expected == 240

    live = json.loads(json.dumps(intended))
    for rule in live["rules"]:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = [{"context": "CI gate", "integration_id": 15368}]
    (drift,) = diff_ruleset(live, intended)
    assert drift.actual == ["CI gate"]
    assert drift.expected == ["CI gate", "review-bot-ack"]


def test_verifier_ignores_context_ordering(intended: dict[str, Any]) -> None:
    """GitHub does not promise an order; a reorder is not drift."""
    live = json.loads(json.dumps(intended))
    for rule in live["rules"]:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"].reverse()
    assert diff_ruleset(live, intended) == []
