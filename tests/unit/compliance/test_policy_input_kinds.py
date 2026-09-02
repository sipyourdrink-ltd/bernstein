"""Asserted-versus-observed policy inputs in the compliance policy library.

A ``PolicyInput`` field is either something Bernstein observed (and can point
at the evidence for) or something the operator asserted.  These tests pin the
consequences of that distinction: an asserted control is rendered as a
declaration, is marked unevidenced wherever it is serialised, and never
contributes to an evidenced-coverage count.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bernstein.core.compliance_policies import (
    _BY_ID,
    ALL_POLICIES,
    PolicyEvidenceStatus,
    PolicyInput,
    PolicyInputKind,
    classify_policy_input_fields,
    evaluate_all,
    evaluate_policy,
    observe_audit_retention_days,
    policy_input_fields,
    summarise_evidence_coverage,
)
from click.testing import CliRunner

from bernstein.cli.commands.compliance_cmd import compliance_group

_ENFORCEMENT_FREE_PREFIX = "The operator declares"


# ---------------------------------------------------------------------------
# 1. Rendering
# ---------------------------------------------------------------------------


def test_asserted_field_renders_as_a_declaration_not_as_enforcement() -> None:
    """soc2-cc6-01 reads mfa_enabled, which the operator asserts.

    Its rendered control statement must attribute the claim to the operator
    rather than state that the system enforces it.
    """
    inp = PolicyInput(rbac_enabled=True, mfa_enabled=True)
    result = evaluate_policy(_BY_ID["soc2-cc6-01"], inp)

    assert result.passed is True
    assert result.evidence_status is PolicyEvidenceStatus.OPERATOR_ASSERTED
    assert result.control_statement.startswith(_ENFORCEMENT_FREE_PREFIX)
    assert "mfa_enabled" in result.asserted_inputs
    # The bare description ("Access to systems is restricted ...") must never
    # stand on its own as the rendered statement.
    assert result.control_statement != _BY_ID["soc2-cc6-01"].description


def test_observed_policy_renders_without_the_operator_declaration_prefix(tmp_path: Path) -> None:
    """A policy whose every input is observed is not rendered as a claim."""
    _seed_audit_segments(tmp_path, ages_in_days=[400])
    inp = PolicyInput(audit_retention_days=observe_audit_retention_days(tmp_path))
    result = evaluate_policy(_BY_ID["soc2-cc7-03"], inp)

    assert result.passed is True
    assert result.evidence_status is PolicyEvidenceStatus.EVIDENCED
    assert not result.control_statement.startswith(_ENFORCEMENT_FREE_PREFIX)
    assert result.asserted_inputs == ()
    assert result.evidence_refs


# ---------------------------------------------------------------------------
# 2. Serialised evidence
# ---------------------------------------------------------------------------


def test_asserted_field_is_marked_unevidenced_in_the_pack(tmp_path: Path) -> None:
    """The machine-readable artefact marks operator-asserted controls."""
    runner = CliRunner()
    res = runner.invoke(
        compliance_group,
        [
            "check",
            "--framework",
            "soc2",
            "--workdir",
            str(tmp_path),
            "--json-output",
            "--fail-on",
            "none",
            "--rbac-enabled",
            "--mfa-enabled",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)

    by_id = {row["policy_id"]: row for row in payload["results"]}
    mfa_row = by_id["soc2-cc6-01"]
    assert mfa_row["passed"] is True
    assert mfa_row["evidence_status"] == PolicyEvidenceStatus.OPERATOR_ASSERTED.value
    assert mfa_row["asserted_inputs"] == ["mfa_enabled", "rbac_enabled"]
    assert mfa_row["control_statement"].startswith(_ENFORCEMENT_FREE_PREFIX)

    assert payload["summary"]["evidence"]["operator_asserted"] > 0


# ---------------------------------------------------------------------------
# 3. Coverage
# ---------------------------------------------------------------------------


def test_coverage_report_does_not_count_asserted_fields_as_evidenced() -> None:
    """Every SOC 2 control passing does not make every control evidenced."""
    inp = PolicyInput(
        audit_logging=True,
        audit_hmac_chain=True,
        audit_retention_days=365,
        sandbox_enabled=True,
        seccomp_enabled=True,
        network_isolation=True,
        read_only_rootfs=True,
        tls_enforced=True,
        secrets_rotation_days=30,
        mfa_enabled=True,
        rbac_enabled=True,
        least_privilege_caps=True,
        vulnerability_scanning=True,
        sbom_enabled=True,
        change_approval_gates=True,
        incident_response_plan=True,
        data_classification=True,
        encrypt_at_rest=True,
        encrypt_in_transit=True,
        log_integrity=True,
        access_review_days=30,
        password_min_length=16,
        session_timeout_minutes=15,
        agent_token_expiry_hours=1,
        rate_limiting_enabled=True,
        waf_enabled=True,
        backup_enabled=True,
        backup_encryption=True,
        dr_rto_hours=1,
        code_signing=True,
        dependency_pinning=True,
        sast_in_ci=True,
        phi_detection=True,
        data_residency_enforced=True,
    )
    results = evaluate_all(inp)
    assert all(r.passed for r in results), "fixture must make every policy pass"

    coverage = summarise_evidence_coverage(results)
    assert coverage.total == len(results)
    assert coverage.evidenced < coverage.total, "all-passing must not mean all-evidenced"
    assert coverage.operator_asserted == coverage.total - coverage.evidenced
    assert "soc2-cc6-01" in coverage.operator_asserted_policy_ids
    assert coverage.evidenced_ratio < 1.0


# ---------------------------------------------------------------------------
# 4. Exhaustiveness over the dataclass
# ---------------------------------------------------------------------------


def test_every_policy_input_field_declares_its_kind() -> None:
    """A new PolicyInput field cannot be added without choosing a kind."""
    kinds = classify_policy_input_fields(PolicyInput)
    field_names = {f.name for f in dataclasses.fields(PolicyInput)}
    assert set(kinds) == field_names
    for name, kind in kinds.items():
        assert isinstance(kind, PolicyInputKind), f"{name} has a non-enum kind"

    @dataclasses.dataclass(frozen=True)
    class _Unclassified:
        forgotten: bool = False

    with pytest.raises(ValueError, match="forgotten"):
        classify_policy_input_fields(_Unclassified)


# ---------------------------------------------------------------------------
# 5. Observed fields carry their evidence
# ---------------------------------------------------------------------------


def test_observed_field_carries_an_evidence_reference() -> None:
    """Observed kind is only expressible together with an evidence reference."""
    kinds = classify_policy_input_fields(PolicyInput)
    observed = [n for n, k in kinds.items() if k is PolicyInputKind.OBSERVED]
    assert observed, "the type must be exercised by at least one real field"

    by_name = {f.name: f for f in dataclasses.fields(PolicyInput)}
    for name in observed:
        ref = by_name[name].metadata.get("evidence_ref", "")
        assert ref, f"observed field {name} carries no evidence reference"

    @dataclasses.dataclass(frozen=True)
    class _Unreferenced:
        floating: int = dataclasses.field(
            default=0,
            metadata={"kind": PolicyInputKind.OBSERVED},
        )

    with pytest.raises(ValueError, match="evidence_ref"):
        classify_policy_input_fields(_Unreferenced)


# ---------------------------------------------------------------------------
# Supporting guards
# ---------------------------------------------------------------------------


def test_rego_input_refs_match_the_python_check() -> None:
    """Evidence status is derived from the Rego rule; it must match the check."""
    for policy in ALL_POLICIES:
        from_rego = set(policy_input_fields(policy.policy_id))
        from_check = {n for n in policy.check.__code__.co_names if n in _POLICY_INPUT_NAMES}
        assert from_rego, f"{policy.policy_id} references no policy input"
        assert from_rego == from_check, f"{policy.policy_id}: rego {from_rego} != check {from_check}"


def test_observed_retention_days_cannot_exceed_the_oldest_retained_segment(tmp_path: Path) -> None:
    """Retention is read off the segments on disk, not off an operator claim."""
    assert observe_audit_retention_days(tmp_path) == 0

    _seed_audit_segments(tmp_path, ages_in_days=[40, 3])
    assert observe_audit_retention_days(tmp_path) == 40

    _seed_audit_segments(tmp_path, ages_in_days=[400], archived=True)
    assert observe_audit_retention_days(tmp_path) == 400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLICY_INPUT_NAMES = {f.name for f in dataclasses.fields(PolicyInput)}
_DATE_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _seed_audit_segments(audit_dir: Path, ages_in_days: list[int], *, archived: bool = False) -> None:
    """Write ``<date>.jsonl`` (or archived ``.jsonl.gz``) segments."""
    target = audit_dir / "archive" if archived else audit_dir
    target.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz=UTC).date()
    for age in ages_in_days:
        day = (today - timedelta(days=age)).isoformat()
        assert _DATE_TOKEN.match(day)
        if archived:
            (target / f"{day}.jsonl.gz").write_bytes(gzip.compress(b"{}\n"))
        else:
            (target / f"{day}.jsonl").write_text("{}\n", encoding="utf-8")
