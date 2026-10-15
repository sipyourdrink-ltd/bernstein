"""Mutation-kill tests for ``core/security/compliance_policies.py`` (#6471).

``scripts/mutmut_critical.py`` scored the ``compliance_policies`` target at
3/80: its one test file checks the catalogue's shape (counts, ids, severities)
and two hand-picked inputs, so flipping a field's least-secure default, an
``and`` into an ``or`` inside a check, or a comparison in the retention
observer survived. Each test below pins one of those decisions to an exact
value, following the other ``*_mutation_kill.py`` files.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.compliance_policies import (
    ALL_POLICIES,
    POLICY_INPUT_EVIDENCE_REFS,
    POLICY_INPUT_KINDS,
    CompliancePolicy,
    PolicyInput,
    PolicyInputKind,
    _asserted,
    _observed,
    classify_policy_input_fields,
    observe_audit_retention_days,
    policy_input_fields,
)

_CATALOGUE = Path(__file__).parent / "fixtures" / "compliance_policy_catalogue.json"

#: Every field's least-secure default. Omitting a field from a snapshot must
#: produce a finding, so each default is the value that fails its policies.
_LEAST_SECURE = {
    "audit_logging": False,
    "audit_hmac_chain": False,
    "audit_retention_days": 0,
    "sandbox_enabled": False,
    "seccomp_enabled": False,
    "network_isolation": False,
    "read_only_rootfs": False,
    "tls_enforced": False,
    "secrets_rotation_days": 999,
    "mfa_enabled": False,
    "rbac_enabled": False,
    "least_privilege_caps": False,
    "vulnerability_scanning": False,
    "sbom_enabled": False,
    "change_approval_gates": False,
    "incident_response_plan": False,
    "data_classification": False,
    "encrypt_at_rest": False,
    "encrypt_in_transit": False,
    "log_integrity": False,
    "access_review_days": 999,
    "password_min_length": 0,
    "session_timeout_minutes": 9999,
    "agent_token_expiry_hours": 9999,
    "rate_limiting_enabled": False,
    "waf_enabled": False,
    "backup_enabled": False,
    "backup_encryption": False,
    "dr_rto_hours": 9999,
    "code_signing": False,
    "dependency_pinning": False,
    "sast_in_ci": False,
    "phi_detection": False,
    "data_residency_enforced": False,
    "custom": {},
}

#: A value for every field that satisfies every policy reading it.
_SECURE: dict[str, Any] = {
    name: (True if isinstance(value, bool) else value) for name, value in _LEAST_SECURE.items() if name != "custom"
} | {
    "audit_retention_days": 365,
    "secrets_rotation_days": 90,
    "access_review_days": 90,
    "password_min_length": 12,
    "session_timeout_minutes": 60,
    "agent_token_expiry_hours": 24,
    "dr_rto_hours": 4,
}

#: ``(field, value at the threshold, value one step past it)`` for every
#: numeric policy input. The threshold value passes, the next one fails.
_THRESHOLDS = [
    ("audit_retention_days", 365, 364),
    ("secrets_rotation_days", 90, 91),
    ("access_review_days", 90, 91),
    ("password_min_length", 12, 11),
    ("session_timeout_minutes", 60, 61),
    ("agent_token_expiry_hours", 24, 25),
    ("dr_rto_hours", 4, 5),
]


def _reads(policy: CompliancePolicy) -> tuple[str, ...]:
    """The ``PolicyInput`` fields a policy's Rego rule reads."""
    return policy_input_fields(policy.policy_id)


# ---------------------------------------------------------------------------
# PolicyInput: least-secure defaults and declared kinds
# ---------------------------------------------------------------------------


def test_every_default_is_the_least_secure_value() -> None:
    assert asdict(PolicyInput()) == _LEAST_SECURE


def test_only_retention_is_observed_and_it_names_its_evidence() -> None:
    assert {n for n, k in POLICY_INPUT_KINDS.items() if k is PolicyInputKind.OBSERVED} == {"audit_retention_days"}
    assert set(POLICY_INPUT_KINDS) == set(_LEAST_SECURE)
    assert POLICY_INPUT_EVIDENCE_REFS == {"audit_retention_days": "audit-chain:oldest-retained-segment"}


# ---------------------------------------------------------------------------
# classify_policy_input_fields: refuses to guess a field's kind
# ---------------------------------------------------------------------------


def test_a_field_with_no_kind_is_refused() -> None:
    @dataclass
    class Unclassified:
        x: int = 0

    with pytest.raises(ValueError) as exc:
        classify_policy_input_fields(Unclassified)
    assert str(exc.value) == (
        "policy input field 'x' declares no PolicyInputKind; use _asserted(...) or _observed(..., evidence_ref=...)"
    )


def test_a_kind_that_is_not_a_policy_input_kind_is_refused() -> None:
    @dataclass
    class WrongKind:
        x: int = field(default=0, metadata={"kind": "observed"})

    with pytest.raises(ValueError, match="declares no PolicyInputKind"):
        classify_policy_input_fields(WrongKind)


def test_an_observed_field_without_evidence_is_refused() -> None:
    @dataclass
    class NoEvidence:
        x: int = field(default=0, metadata={"kind": PolicyInputKind.OBSERVED})

    with pytest.raises(ValueError, match=r"observed policy input field 'x' declares no evidence_ref"):
        classify_policy_input_fields(NoEvidence)


def test_asserted_and_evidenced_observed_fields_classify() -> None:
    @dataclass
    class Mixed:
        a: bool = _asserted(False)
        o: int = _observed(0, evidence_ref="somewhere")

    assert classify_policy_input_fields(Mixed) == {"a": PolicyInputKind.ASSERTED, "o": PolicyInputKind.OBSERVED}


# ---------------------------------------------------------------------------
# observe_audit_retention_days
# ---------------------------------------------------------------------------

#: Far from the real date on purpose: a reference that equals today would let
#: ``today or now()`` and ``today and now()`` agree.
_TODAY = date(2031, 3, 20)


def _day(days_ago: int) -> str:
    return (_TODAY - timedelta(days=days_ago)).isoformat()


def _segment(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_a_missing_audit_dir_evidences_nothing(tmp_path: Path) -> None:
    assert observe_audit_retention_days(tmp_path / "absent", today=_TODAY) == 0


def test_an_empty_audit_dir_evidences_nothing(tmp_path: Path) -> None:
    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 0


def test_the_oldest_live_segment_sets_the_span(tmp_path: Path) -> None:
    _segment(tmp_path / f"{_day(14)}.jsonl")
    _segment(tmp_path / f"{_day(40)}.jsonl")
    _segment(tmp_path / f"{_day(1)}.jsonl")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 40


def test_an_archived_segment_counts_and_can_be_the_oldest(tmp_path: Path) -> None:
    _segment(tmp_path / f"{_day(14)}.jsonl")
    _segment(tmp_path / "archive" / f"{_day(90)}.jsonl.gz")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 90


def test_archives_count_with_no_live_segment(tmp_path: Path) -> None:
    _segment(tmp_path / "archive" / f"{_day(10)}.jsonl.gz")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 10


def test_an_undated_file_evidences_no_day(tmp_path: Path) -> None:
    _segment(tmp_path / "current.jsonl")
    _segment(tmp_path / "archive" / "renamed.jsonl.gz")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 0


def test_an_undated_file_does_not_hide_a_dated_one(tmp_path: Path) -> None:
    _segment(tmp_path / "current.jsonl")
    _segment(tmp_path / f"{_day(2)}.jsonl")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 2


def test_a_segment_dated_after_today_never_goes_negative(tmp_path: Path) -> None:
    _segment(tmp_path / f"{_day(-5)}.jsonl")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 0


def test_a_segment_from_today_is_zero_days(tmp_path: Path) -> None:
    _segment(tmp_path / f"{_day(0)}.jsonl")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 0


def test_a_segment_from_yesterday_is_one_day(tmp_path: Path) -> None:
    _segment(tmp_path / f"{_day(1)}.jsonl")

    assert observe_audit_retention_days(tmp_path, today=_TODAY) == 1


# ---------------------------------------------------------------------------
# Policy checks
# ---------------------------------------------------------------------------

_POLICIES = ALL_POLICIES


@pytest.mark.parametrize("policy", _POLICIES, ids=[p.policy_id for p in _POLICIES])
def test_every_policy_fails_on_the_least_secure_input(policy: CompliancePolicy) -> None:
    assert policy.check(PolicyInput()) is False


@pytest.mark.parametrize("policy", _POLICIES, ids=[p.policy_id for p in _POLICIES])
def test_every_policy_passes_when_every_input_it_reads_is_secure(policy: CompliancePolicy) -> None:
    assert bool(policy.check(PolicyInput(**_SECURE))) is True


@pytest.mark.parametrize("policy", _POLICIES, ids=[p.policy_id for p in _POLICIES])
def test_every_input_a_policy_reads_is_required(policy: CompliancePolicy) -> None:
    """Each check is a conjunction: one insecure input fails it, the rest secure.

    This is what kills an ``and`` flipped to ``or``: with every other input
    secure, an ``or`` would still pass.
    """
    reads = _reads(policy)
    assert reads, policy.policy_id
    secure = PolicyInput(**_SECURE)
    for name in reads:
        weakened = replace(secure, **{name: _LEAST_SECURE[name]})
        assert bool(policy.check(weakened)) is False, (policy.policy_id, name)


@pytest.mark.parametrize(("name", "at", "past"), _THRESHOLDS)
def test_each_numeric_threshold_is_inclusive(name: str, at: int, past: int) -> None:
    policies = [p for p in _POLICIES if name in _reads(p)]
    assert policies, name
    for policy in policies:
        assert bool(policy.check(PolicyInput(**(_SECURE | {name: at})))) is True, policy.policy_id
        assert bool(policy.check(PolicyInput(**(_SECURE | {name: past})))) is False, policy.policy_id


def test_every_check_reads_only_what_its_rego_names() -> None:
    """A secure value on any field the Rego does not name cannot rescue a check."""
    for policy in _POLICIES:
        reads = set(_reads(policy))
        only_reads = PolicyInput(**{n: v for n, v in _SECURE.items() if n in reads})
        assert bool(policy.check(only_reads)) is True, policy.policy_id
        everything_else = PolicyInput(**{n: v for n, v in _SECURE.items() if n not in reads})
        assert bool(policy.check(everything_else)) is False, policy.policy_id


# ---------------------------------------------------------------------------
# Catalogue text
# ---------------------------------------------------------------------------


def _catalogue() -> dict[str, dict[str, str]]:
    return {
        p.policy_id: {
            "name": p.name,
            "framework": p.framework.value,
            "control_id": p.control_id,
            "severity": p.severity.value,
            "description": p.description,
            "remediation": p.remediation,
        }
        for p in _POLICIES
    }


def test_the_catalogue_text_matches_the_reviewed_copy() -> None:
    """Names, control ids and wording are what an auditor reads in a report.

    A change to any of them should be a reviewed edit to the fixture, not a
    side effect. Regenerate with ``python -c`` over ``_catalogue()`` after a
    deliberate change.
    """
    expected = json.loads(_CATALOGUE.read_text(encoding="utf-8"))
    assert _catalogue() == expected
