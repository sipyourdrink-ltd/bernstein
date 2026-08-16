"""Unit tests for ``scripts/check_branch_ruleset_audit.py``.

Classic branch protection has no field for `merge_queue` or
`bypass_actors`, so an audit built on it cannot see a bypass actor added to
the ruleset or the merge-queue rule dropped - it stays green while the
regression that matters most goes uncaught. These tests pin the ruleset-
native replacement against the real shape of the three endpoints it reads,
and prove each audited invariant actually fails when it should.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_branch_ruleset_audit import (
    Violation,
    evaluate,
    main,
    read_required_contexts,
)

REQUIRED_CONTEXTS = ["CI gate"]

# `gh api repos/sipyourdrink-ltd/bernstein/rules/branches/main`, captured
# 2026-08-16 and trimmed to the fields the audit reads. One ruleset backs
# every rule; `pull_request` is present live but not audited here - see
# the script docstring for which invariants are in scope.
LIVE_RULES_2026_08_16: list[dict[str, Any]] = [
    {
        "type": "merge_queue",
        "parameters": {
            "merge_method": "SQUASH",
            "max_entries_to_build": 5,
            "min_entries_to_merge": 1,
            "max_entries_to_merge": 1,
            "min_entries_to_merge_wait_minutes": 0,
            "grouping_strategy": "ALLGREEN",
            "check_response_timeout_minutes": 240,
        },
        "ruleset_id": 16719298,
    },
    {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": True,
            "required_status_checks": [{"context": "CI gate", "integration_id": 15368}],
        },
        "ruleset_id": 16719298,
    },
    {
        "type": "pull_request",
        "parameters": {"required_approving_review_count": 0},
        "ruleset_id": 16719298,
    },
    {"type": "non_fast_forward", "ruleset_id": 16719298},
    {"type": "deletion", "ruleset_id": 16719298},
]

# `gh api repos/sipyourdrink-ltd/bernstein/rulesets`, same capture.
LIVE_RULESETS_2026_08_16: list[dict[str, Any]] = [
    {"id": 16719298, "name": "main-merge-queue", "target": "branch", "enforcement": "active"},
]

# `gh api repos/sipyourdrink-ltd/bernstein/rulesets/16719298`, same
# capture, trimmed to the fields the audit reads.
LIVE_RULESET_DETAIL_2026_08_16: dict[str, Any] = {
    "id": 16719298,
    "name": "main-merge-queue",
    "enforcement": "active",
    "bypass_actors": [],
}


def _details() -> dict[int, dict[str, Any]]:
    return {16719298: json.loads(json.dumps(LIVE_RULESET_DETAIL_2026_08_16))}


def _rules() -> list[dict[str, Any]]:
    return json.loads(json.dumps(LIVE_RULES_2026_08_16))


def _rulesets() -> list[dict[str, Any]]:
    return json.loads(json.dumps(LIVE_RULESETS_2026_08_16))


# --------------------------------------------------------------------------
# The real captured shape passes clean
# --------------------------------------------------------------------------


def test_audit_accepts_the_live_ruleset() -> None:
    assert evaluate(_rules(), _rulesets(), _details(), REQUIRED_CONTEXTS) == []


# --------------------------------------------------------------------------
# Each audited invariant - proof the check can fail
# --------------------------------------------------------------------------


def test_audit_catches_a_missing_merge_queue_rule() -> None:
    rules = [r for r in _rules() if r["type"] != "merge_queue"]
    violations = evaluate(rules, _rulesets(), _details(), REQUIRED_CONTEXTS)
    assert Violation("merge_queue", "no 'merge_queue' rule applies to this branch") in violations


def test_audit_catches_a_missing_non_fast_forward_rule() -> None:
    rules = [r for r in _rules() if r["type"] != "non_fast_forward"]
    violations = evaluate(rules, _rulesets(), _details(), REQUIRED_CONTEXTS)
    assert Violation("non_fast_forward", "no 'non_fast_forward' rule applies to this branch") in violations


def test_audit_catches_a_missing_deletion_rule() -> None:
    rules = [r for r in _rules() if r["type"] != "deletion"]
    violations = evaluate(rules, _rulesets(), _details(), REQUIRED_CONTEXTS)
    assert Violation("deletion", "no 'deletion' rule applies to this branch") in violations


def test_audit_catches_a_missing_required_status_checks_rule() -> None:
    rules = [r for r in _rules() if r["type"] != "required_status_checks"]
    violations = evaluate(rules, _rulesets(), _details(), REQUIRED_CONTEXTS)
    assert violations == [
        Violation("required_status_checks", "no 'required_status_checks' rule applies to this branch")
    ]


def test_audit_catches_a_dropped_required_context() -> None:
    """The concrete scenario the issue names: the wrong context recorded locally."""
    rules = _rules()
    for rule in rules:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = [{"context": "some-other-check", "integration_id": 1}]
    violations = evaluate(rules, _rulesets(), _details(), REQUIRED_CONTEXTS)
    fields = {v.rule for v in violations}
    assert fields == {"required_status_checks"}
    details = " ".join(v.detail for v in violations)
    assert "CI gate" in details
    assert "some-other-check" in details


def test_audit_catches_an_extra_required_context() -> None:
    rules = _rules()
    for rule in rules:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"].append({"context": "extra-check", "integration_id": 2})
    violations = evaluate(rules, _rulesets(), _details(), REQUIRED_CONTEXTS)
    assert violations == [Violation("required_status_checks", "unexpected required context(s): ['extra-check']")]


def test_audit_catches_an_added_bypass_actor() -> None:
    """The other concrete regression the issue names: a bypass actor added."""
    details = _details()
    details[16719298]["bypass_actors"] = [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}]
    violations = evaluate(_rules(), _rulesets(), details, REQUIRED_CONTEXTS)
    assert len(violations) == 1
    assert violations[0].rule == "bypass_actors"
    assert "OrganizationAdmin" in violations[0].detail


def test_a_missing_bypass_actors_key_is_a_violation_not_an_empty_list() -> None:
    """Absent and empty are different answers on the wire.

    `rulesets/{id}` omits the `bypass_actors` key entirely for a caller
    without Administration: read, and returns `[]` only when an
    admin-scoped caller sees a genuinely empty list. An under-scoped
    BRANCH_PROTECTION_AUDIT_TOKEN therefore produces a 200 whose shape
    silently skips the one assertion this audit cannot afford to skip --
    the other two calls need only repo read and keep passing. The fixture
    mirrors the recorded non-admin response: same ruleset, no key.
    """
    details = _details()
    del details[16719298]["bypass_actors"]
    violations = evaluate(_rules(), _rulesets(), details, REQUIRED_CONTEXTS)
    assert len(violations) == 1
    assert violations[0].rule == "bypass_actors"
    assert "unproven" in violations[0].detail
    assert "Administration" in violations[0].detail


def test_audit_catches_a_ruleset_flipped_to_evaluate() -> None:
    """A ruleset in `evaluate` mode still contributes rules to the effective
    set, so presence alone does not prove enforcement."""
    rulesets = _rulesets()
    rulesets[0]["enforcement"] = "evaluate"
    violations = evaluate(_rules(), rulesets, _details(), REQUIRED_CONTEXTS)
    assert violations == [Violation("enforcement", "ruleset 16719298 (main-merge-queue) is 'evaluate', not 'active'")]


def test_audit_catches_a_ruleset_missing_from_the_listing() -> None:
    violations = evaluate(_rules(), [], _details(), REQUIRED_CONTEXTS)
    assert any(v.rule == "rulesets" for v in violations)


def test_audit_catches_a_ruleset_detail_that_was_never_read() -> None:
    violations = evaluate(_rules(), _rulesets(), {}, REQUIRED_CONTEXTS)
    assert violations == [
        Violation("ruleset_detail", "ruleset 16719298 detail was not read; cannot verify bypass actors")
    ]


def test_audit_reports_no_rules_as_a_violation() -> None:
    violations = evaluate([], _rulesets(), _details(), REQUIRED_CONTEXTS)
    assert violations == [Violation("rules", "no rules apply to this branch - protection is effectively absent")]


def test_audit_ignores_pull_request_rule_changes() -> None:
    """`pull_request` review policy is not an invariant this audit owns."""
    rules = _rules()
    for rule in rules:
        if rule["type"] == "pull_request":
            rule["parameters"]["required_approving_review_count"] = 2
    assert evaluate(rules, _rulesets(), _details(), REQUIRED_CONTEXTS) == []


# --------------------------------------------------------------------------
# Required contexts come from the canary, not a restated constant
# --------------------------------------------------------------------------


def test_required_contexts_come_from_the_canary() -> None:
    assert read_required_contexts() == REQUIRED_CONTEXTS


# --------------------------------------------------------------------------
# CLI wiring, offline against files (fixture-driven, no network)
# --------------------------------------------------------------------------


@pytest.fixture
def fixture_files(tmp_path: Path) -> dict[str, Path]:
    rules_path = tmp_path / "rules.json"
    rulesets_path = tmp_path / "rulesets.json"
    details_path = tmp_path / "details.json"
    rules_path.write_text(json.dumps(_rules()), encoding="utf-8")
    rulesets_path.write_text(json.dumps(_rulesets()), encoding="utf-8")
    details_path.write_text(json.dumps({"16719298": LIVE_RULESET_DETAIL_2026_08_16}), encoding="utf-8")
    return {"rules": rules_path, "rulesets": rulesets_path, "details": details_path}


def _argv(fixture_files: dict[str, Path]) -> list[str]:
    return [
        "--repo",
        "sipyourdrink-ltd/bernstein",
        "--rules-file",
        str(fixture_files["rules"]),
        "--rulesets-file",
        str(fixture_files["rulesets"]),
        "--ruleset-details-file",
        str(fixture_files["details"]),
    ]


def test_main_exits_zero_when_the_ruleset_matches(
    fixture_files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(fixture_files)) == 0
    assert "satisfies every audited invariant" in capsys.readouterr().out


def test_main_exits_one_when_a_bypass_actor_is_added(
    fixture_files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    details = json.loads(fixture_files["details"].read_text(encoding="utf-8"))
    details["16719298"]["bypass_actors"] = [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}]
    fixture_files["details"].write_text(json.dumps(details), encoding="utf-8")

    assert main(_argv(fixture_files)) == 1
    out = capsys.readouterr().out
    assert "::error::1 branch ruleset invariant(s) violated" in out
    assert "bypass_actors" in out


def test_main_exits_two_without_a_repo(monkeypatch: pytest.MonkeyPatch, fixture_files: dict[str, Path]) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    argv = [a for a in _argv(fixture_files) if a not in ("--repo", "sipyourdrink-ltd/bernstein")]
    assert main(argv) == 2
