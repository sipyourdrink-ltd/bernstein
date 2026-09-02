"""The compliance policy library routed through the govern audit check contract (#5075)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from bernstein.core.govern.audit_sweep import CheckVerdict
from bernstein.core.govern.compliance_checks import (
    ABSENT,
    CMP_AREA,
    CMP_NAMESPACE,
    RETIRED_CHECK_IDS,
    iter_compliance_checks,
    observe_compliance_controls,
    policy_input_from_project,
    required_check_ids,
    run_compliance_checks,
)
from bernstein.core.security.compliance_library import (
    ComplianceFramework,
    get_framework_rules,
    get_registered_check_names,
)
from bernstein.core.security.compliance_policies import PolicyInput, evaluate_all

if TYPE_CHECKING:
    from pathlib import Path

_CONFIGURED_YAML = """\
auth:
  method: oidc
state_encryption:
  backend: age
rbac:
  roles:
    operator: [tasks:write]
data_retention:
  logs_days: 90
backup:
  schedule: daily
secrets:
  backend: vault
consent:
  basis: contract
quality_gates:
  enabled: true
compliance:
  audit_hmac_chain: true
  phi_detection: true
security:
  tls_enforced: true
  vulnerability_scanning: true
  network_isolation: true
  log_integrity: true
  data_classification: true
  mfa_enabled: true
  rate_limiting_enabled: true
  session_timeout_minutes: 30
  password_min_length: 16
  secrets_rotation_days: 90
"""


def _populate(root: Path) -> Path:
    """Satisfy every policy-library control in *root*, on disk and in config."""
    (root / "bernstein.yaml").write_text(_CONFIGURED_YAML, encoding="utf-8")
    (root / ".sdd" / "audit").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "incident-response.md").write_text("# Incident response\n", encoding="utf-8")
    (root / "docs" / "privacy-policy.md").write_text("# Privacy\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def configured_project(tmp_path: Path) -> Path:
    """An install where every policy-library control is satisfied on disk."""
    return _populate(tmp_path)


@pytest.fixture
def bare_project(tmp_path: Path) -> Path:
    """An install with nothing configured and nothing on disk."""
    return tmp_path


def _outcomes_by_id(project_root: Path) -> dict[str, object]:
    return {outcome.check_id: outcome for outcome in run_compliance_checks(project_root)}


# 1 -------------------------------------------------------------------------


def test_every_compliance_library_check_has_a_stable_cmp_id() -> None:
    """The id table covers the library exactly: no check unrouted, no id without a check."""
    specs = iter_compliance_checks()
    routed = {spec.function_name for spec in specs}
    assert routed == set(get_registered_check_names())
    assert all(spec.check_id.startswith(f"{CMP_NAMESPACE}-") for spec in specs)
    assert all(spec.area == CMP_AREA for spec in specs)


# 2 -------------------------------------------------------------------------


def test_cmp_ids_are_never_reused_or_silently_removed() -> None:
    """Ids are unique, contiguous, and a retired id is tombstoned rather than reassigned."""
    ids = [spec.check_id for spec in iter_compliance_checks()]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)
    assert set(ids).isdisjoint(RETIRED_CHECK_IDS)

    live = {int(check_id.split("-", 1)[1]) for check_id in ids}
    retired = {int(check_id.split("-", 1)[1]) for check_id in RETIRED_CHECK_IDS}
    assert live | retired == set(range(1, len(live) + len(retired) + 1))


# 3 -------------------------------------------------------------------------


def test_key_presence_check_reports_declared_not_measured(tmp_path: Path) -> None:
    """An empty ``auth:`` key is a declaration, and the verdict says so instead of passing."""
    (tmp_path / "bernstein.yaml").write_text("auth:\n", encoding="utf-8")
    spec = next(s for s in iter_compliance_checks() if s.function_name == "check_auth_configured")
    outcome = _outcomes_by_id(tmp_path)[spec.check_id]

    assert outcome.verdict is CheckVerdict.DECLARED  # type: ignore[attr-defined]
    assert outcome.passed is None  # type: ignore[attr-defined]
    assert outcome.evidence == ()  # type: ignore[attr-defined]
    assert "configuration key" in outcome.summary.lower()  # type: ignore[attr-defined]


# 4 -------------------------------------------------------------------------


def test_measured_finding_names_the_bytes_it_read(configured_project: Path) -> None:
    """A file-backed check is measured and its evidence carries that file's real digest."""
    spec = next(s for s in iter_compliance_checks() if s.function_name == "check_dependency_pinning")
    outcome = _outcomes_by_id(configured_project)[spec.check_id]

    assert outcome.verdict is CheckVerdict.MEASURED  # type: ignore[attr-defined]
    assert outcome.passed is True  # type: ignore[attr-defined]
    digest = "sha256:" + hashlib.sha256((configured_project / "uv.lock").read_bytes()).hexdigest()
    assert ("uv.lock", digest) in outcome.evidence  # type: ignore[attr-defined]


# 5 -------------------------------------------------------------------------


def test_measured_finding_records_a_probed_absence_rather_than_empty_evidence(bare_project: Path) -> None:
    """A measured check that found nothing still names every locator it probed."""
    spec = next(s for s in iter_compliance_checks() if s.function_name == "check_incident_response_plan")
    outcome = _outcomes_by_id(bare_project)[spec.check_id]

    assert outcome.verdict is CheckVerdict.MEASURED  # type: ignore[attr-defined]
    assert outcome.passed is False  # type: ignore[attr-defined]
    assert outcome.evidence  # type: ignore[attr-defined]
    assert all(digest == ABSENT for _, digest in outcome.evidence)  # type: ignore[attr-defined]


# 6 -------------------------------------------------------------------------


def test_a_document_backed_check_is_measured_and_a_config_backed_one_is_declared(tmp_path: Path) -> None:
    """The dual-source consent check reports the verdict its evidence actually supports."""
    spec = next(s for s in iter_compliance_checks() if s.function_name == "check_consent_management")

    (tmp_path / "bernstein.yaml").write_text("consent:\n  basis: contract\n", encoding="utf-8")
    declared = _outcomes_by_id(tmp_path)[spec.check_id]
    assert declared.verdict is CheckVerdict.DECLARED  # type: ignore[attr-defined]

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "consent-management.md").write_text("# Consent\n", encoding="utf-8")
    measured = _outcomes_by_id(tmp_path)[spec.check_id]
    assert measured.verdict is CheckVerdict.MEASURED  # type: ignore[attr-defined]
    assert measured.passed is True  # type: ignore[attr-defined]
    assert measured.evidence  # type: ignore[attr-defined]


# 7 -------------------------------------------------------------------------


def test_compliance_check_and_govern_audit_agree_on_the_same_install(configured_project: Path) -> None:
    """One reader feeds both engines, so neither can report the other's install differently.

    Load-bearing test for this change: the ``compliance check`` policy engine
    evaluates a :class:`PolicyInput` snapshot while the policy library reads the
    project. Routed through one observation both agree; on the identical install
    the unrouted default snapshot contradicts them.
    """
    observed = observe_compliance_controls(configured_project)
    derived = policy_input_from_project(configured_project)
    outcomes = _outcomes_by_id(configured_project)

    mapped = [spec for spec in iter_compliance_checks() if spec.policy_input_field]
    assert mapped, "the agreement set must not be empty"
    for spec in mapped:
        assert getattr(derived, spec.policy_input_field) is observed[spec.check_id], spec.check_id
        outcome = outcomes[spec.check_id]
        if outcome.verdict is CheckVerdict.MEASURED:  # type: ignore[attr-defined]
            assert outcome.passed is observed[spec.check_id], spec.check_id  # type: ignore[attr-defined]

    default_failing = {r.policy_id for r in evaluate_all(PolicyInput()) if not r.passed}
    routed_failing = {r.policy_id for r in evaluate_all(derived) if not r.passed}
    assert routed_failing < default_failing


def test_a_bare_install_leaves_every_mapped_control_unsatisfied(bare_project: Path) -> None:
    """The shared reader does not invent a pass when nothing is configured."""
    derived = policy_input_from_project(bare_project)
    for spec in iter_compliance_checks():
        if spec.policy_input_field:
            assert getattr(derived, spec.policy_input_field) is False, spec.check_id


# 8 -------------------------------------------------------------------------


def test_running_only_a_namespace_still_reports_every_registered_check(configured_project: Path) -> None:
    """``--only CMP`` selects the whole namespace, not a subset of it."""
    all_ids = [spec.check_id for spec in iter_compliance_checks()]
    selected = [o.check_id for o in run_compliance_checks(configured_project, only=(CMP_NAMESPACE,))]
    assert selected == sorted(all_ids)


def test_skipping_an_id_removes_only_that_id(configured_project: Path) -> None:
    """``--skip`` is a filter on the requested set, never a suppression of a finding."""
    target = iter_compliance_checks()[0].check_id
    selected = [o.check_id for o in run_compliance_checks(configured_project, skip=(target,))]
    assert target not in selected
    assert len(selected) == len(iter_compliance_checks()) - 1


# 9 -------------------------------------------------------------------------


def test_profile_selects_required_ids_without_asserting_conformance() -> None:
    """A profile names which ids are required; it says nothing about conformance."""
    required = required_check_ids(ComplianceFramework.SOC2)
    all_ids = {spec.check_id for spec in iter_compliance_checks()}

    assert required
    assert required < all_ids
    by_function = {spec.function_name: spec.check_id for spec in iter_compliance_checks()}
    expected = {by_function[rule.check_function_name] for rule in get_framework_rules(ComplianceFramework.SOC2)}
    assert required == expected


# 12 ------------------------------------------------------------------------


@pytest.mark.parametrize("populated", [True, False])
def test_no_measured_finding_ever_carries_empty_evidence(
    populated: bool,
    tmp_path: Path,
) -> None:
    """A measured verdict without evidence is a claim with nothing behind it."""
    root = _populate(tmp_path) if populated else tmp_path
    measured = [o for o in run_compliance_checks(root) if o.verdict is CheckVerdict.MEASURED]
    assert measured, "the fixture must exercise at least one measured check"
    for outcome in measured:
        assert outcome.evidence, outcome.check_id
