"""Route the policy library through the govern audit check contract (#5075).

Three implementations assess the same install and none calls another:
``core/security/compliance_library.py`` holds 23 ``check_*`` functions that read
the project directory but is imported only by the config seeder;
``bernstein compliance check`` evaluates a
:class:`~bernstein.core.security.compliance_policies.PolicyInput` snapshot built
from command-line flags; ``doctor`` answers a third question entirely. Two of
them can therefore describe one install differently -- ``compliance check`` with
no flags reports every control failing on a project whose configuration the
policy library reads as satisfied.

This module is the routing layer. :func:`observe_compliance_controls` and
:func:`run_compliance_checks` both delegate to the same ``check_*`` functions
in ``core/security/compliance_library.py``; :func:`policy_input_from_project`
calls :func:`observe_compliance_controls` and shapes the result into the
:class:`~bernstein.core.security.compliance_policies.PolicyInput` snapshot
the policy engine evaluates. Because the policy snapshot and the audit
findings draw from the same library functions, neither surface can re-decide
a control the other would deny: the only way the two surfaces can disagree
is by misreading the same library result.

The verdict a check earns follows from what its implementation reads, not from
what its title claims:

* a check that tests whether a key is present in a configuration file reports
  ``declared`` -- the operator asserted the control, nothing was read that would
  confirm it. The summary names that gap rather than reporting a pass.
* a check that reads an artefact on disk reports ``measured`` and carries the
  digest of what it read. A locator that was probed and found missing is
  recorded as :data:`ABSENT`, so a measured finding never carries empty evidence
  and never implies a digest for bytes that do not exist.
* ``check_consent_management`` reads both a document set and a configuration
  key, so its verdict follows the source that satisfied it.

Making the key-presence checks assert something about the resolved
configuration is #5056 and is deliberately not done here: this slice changes
where the checks are reported, never what they conclude.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.govern.audit_sweep import CheckOutcome, CheckVerdict
from bernstein.core.security import compliance_library
from bernstein.core.security.compliance_library import (
    ComplianceFramework,
    get_framework_rules,
)
from bernstein.core.security.compliance_policies import PolicyInput

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

#: Namespace every compliance check id is prefixed with.
CMP_NAMESPACE = "CMP"

#: The audit area these checks belong to.
CMP_AREA = "compliance"

#: Digest recorded for a locator that was probed and found not to exist. A
#: measured finding names what it looked at either way; it never claims a
#: digest for bytes that were never there.
ABSENT = "absent"

#: Ids that named a check that no longer exists. An id in here is never reused.
RETIRED_CHECK_IDS: frozenset[str] = frozenset()

#: What a declared verdict says, appended to the library's own evidence line.
_DECLARED_GAP = "configuration key presence only; nothing was read that confirms the control"


@dataclass(frozen=True, slots=True)
class ComplianceCheckSpec:
    """One policy-library check, as the audit registers it.

    Attributes:
        check_id: Stable ``CMP-nnn`` id. Never reused, never renumbered.
        function_name: The ``check_*`` function in ``compliance_library``.
        area: The audit area, always :data:`CMP_AREA`.
        source: ``config`` when the check tests configuration key presence,
            ``artifact`` when it reads something on disk, ``either`` when it
            reads both and the verdict follows whichever satisfied it.
        asserts: What the check does and does not prove, in one line.
        policy_input_field: The boolean
            :class:`~bernstein.core.security.compliance_policies.PolicyInput`
            field this observation feeds, or ``""`` when the policy engine
            expresses the same control as a numeric threshold the library does
            not measure.
        dir_locators: Project-relative directories the check probes.
        file_locators: Project-relative files the check probes.
    """

    check_id: str
    function_name: str
    source: str
    asserts: str
    policy_input_field: str = ""
    dir_locators: tuple[str, ...] = ()
    file_locators: tuple[str, ...] = ()

    @property
    def area(self) -> str:
        """Return the audit area this check belongs to."""
        return CMP_AREA


_INCIDENT_RESPONSE_DOCS = (
    "docs/incident-response.md",
    "docs/incident_response.md",
    "docs/INCIDENT_RESPONSE.md",
    ".sdd/incident-response.yaml",
    "INCIDENT_RESPONSE.md",
)

_PRIVACY_DOCS = (
    "docs/privacy-policy.md",
    "docs/PRIVACY.md",
    "PRIVACY.md",
    "docs/data-processing.md",
    ".sdd/privacy.yaml",
)

_LOCK_FILES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "package-lock.json",
)

_CONSENT_DOCS = (
    "docs/consent-management.md",
    "docs/data-subject-rights.md",
    ".sdd/consent.yaml",
)

#: The registry. Ids are assigned once, in this order, and never renumbered.
_SPECS: tuple[ComplianceCheckSpec, ...] = (
    ComplianceCheckSpec(
        check_id="CMP-001",
        function_name="check_audit_logging_enabled",
        source="artifact",
        asserts="the audit directory exists; not that anything was written to it",
        policy_input_field="audit_logging",
        dir_locators=(".sdd/audit",),
    ),
    ComplianceCheckSpec(
        check_id="CMP-002",
        function_name="check_auth_configured",
        source="config",
        asserts="an auth section is present; not that any method resolves",
    ),
    ComplianceCheckSpec(
        check_id="CMP-003",
        function_name="check_encryption_at_rest",
        source="config",
        asserts="an encryption setting is present; not that state is encrypted",
        policy_input_field="encrypt_at_rest",
    ),
    ComplianceCheckSpec(
        check_id="CMP-004",
        function_name="check_access_controls",
        source="config",
        asserts="a role section is present; not that any binding is enforced",
        policy_input_field="rbac_enabled",
    ),
    ComplianceCheckSpec(
        check_id="CMP-005",
        function_name="check_data_retention",
        source="config",
        asserts="a retention section is present; not that anything is expired",
    ),
    ComplianceCheckSpec(
        check_id="CMP-006",
        function_name="check_backup_configured",
        source="config",
        asserts="a backup section is present; not that a backup ran",
        policy_input_field="backup_enabled",
    ),
    ComplianceCheckSpec(
        check_id="CMP-007",
        function_name="check_tls_enforced",
        source="config",
        asserts="a TLS setting is enabled; not that any connection used it",
        policy_input_field="tls_enforced",
    ),
    ComplianceCheckSpec(
        check_id="CMP-008",
        function_name="check_incident_response_plan",
        source="artifact",
        asserts="a response document exists; not that it was exercised",
        policy_input_field="incident_response_plan",
        file_locators=_INCIDENT_RESPONSE_DOCS,
    ),
    ComplianceCheckSpec(
        check_id="CMP-009",
        function_name="check_secrets_management",
        source="config",
        asserts="a secrets section is present; not that a backend answers",
    ),
    ComplianceCheckSpec(
        check_id="CMP-010",
        function_name="check_vulnerability_scanning",
        source="config",
        asserts="a scanning setting is enabled; not that a scan ran",
        policy_input_field="vulnerability_scanning",
    ),
    ComplianceCheckSpec(
        check_id="CMP-011",
        function_name="check_change_management",
        source="config",
        asserts="a gate section is enabled; not that any change was gated",
        policy_input_field="change_approval_gates",
    ),
    ComplianceCheckSpec(
        check_id="CMP-012",
        function_name="check_network_isolation",
        source="config",
        asserts="an isolation setting is present; not that a sandbox applied it",
        policy_input_field="network_isolation",
    ),
    ComplianceCheckSpec(
        check_id="CMP-013",
        function_name="check_logging_integrity",
        source="config",
        asserts="a chain setting is enabled; not that any chain verifies",
        policy_input_field="log_integrity",
    ),
    ComplianceCheckSpec(
        check_id="CMP-014",
        function_name="check_session_management",
        source="config",
        asserts="a session setting is present; not that any session expired",
    ),
    ComplianceCheckSpec(
        check_id="CMP-015",
        function_name="check_password_policy",
        source="config",
        asserts="a minimum length is configured; not that it is enforced",
    ),
    ComplianceCheckSpec(
        check_id="CMP-016",
        function_name="check_mfa_enabled",
        source="config",
        asserts="an MFA flag is set; not that any factor was presented",
        policy_input_field="mfa_enabled",
    ),
    ComplianceCheckSpec(
        check_id="CMP-017",
        function_name="check_sdd_state_directory",
        source="artifact",
        asserts="the state directory exists; not that it holds valid state",
        dir_locators=(".sdd",),
    ),
    ComplianceCheckSpec(
        check_id="CMP-018",
        function_name="check_rate_limiting",
        source="config",
        asserts="a rate-limit setting is present; not that a limit applied",
        policy_input_field="rate_limiting_enabled",
    ),
    ComplianceCheckSpec(
        check_id="CMP-019",
        function_name="check_dependency_pinning",
        source="artifact",
        asserts="a lock file exists; not that the environment matches it",
        policy_input_field="dependency_pinning",
        file_locators=_LOCK_FILES,
    ),
    ComplianceCheckSpec(
        check_id="CMP-020",
        function_name="check_privacy_policy",
        source="artifact",
        asserts="a privacy document exists; not that processing matches it",
        file_locators=_PRIVACY_DOCS,
    ),
    ComplianceCheckSpec(
        check_id="CMP-021",
        function_name="check_data_classification",
        source="config",
        asserts="a classification setting is present; not that data is labelled",
        policy_input_field="data_classification",
    ),
    ComplianceCheckSpec(
        check_id="CMP-022",
        function_name="check_phi_detection",
        source="config",
        asserts="a detection flag is set; not that any scan ran",
        policy_input_field="phi_detection",
    ),
    ComplianceCheckSpec(
        check_id="CMP-023",
        function_name="check_consent_management",
        source="either",
        asserts="a consent document or configuration key exists; not that consent was captured",
        file_locators=_CONSENT_DOCS,
    ),
)

_SPEC_BY_ID = {spec.check_id: spec for spec in _SPECS}
_SPEC_BY_FUNCTION = {spec.function_name: spec for spec in _SPECS}


def iter_compliance_checks() -> tuple[ComplianceCheckSpec, ...]:
    """Return every registered compliance check, ordered by id."""
    return _SPECS


def required_check_ids(profile: ComplianceFramework) -> frozenset[str]:
    """Return the check ids *profile* requires.

    A profile selects which ids an operator has to look at. It says nothing
    about whether the install satisfies them, and produces no claim of its own:
    the findings are the same findings whichever profile is named.
    """
    return frozenset(
        _SPEC_BY_FUNCTION[rule.check_function_name].check_id
        for rule in get_framework_rules(profile)
        if rule.check_function_name in _SPEC_BY_FUNCTION
    )


# ---------------------------------------------------------------------------
# The shared read
# ---------------------------------------------------------------------------


def _library_result(spec: ComplianceCheckSpec, project_root: Path) -> Any:
    check_fn = getattr(compliance_library, spec.function_name)
    return check_fn(project_root)


def observe_compliance_controls(project_root: Path) -> dict[str, bool]:
    """Evaluate every compliance check and return ``check_id -> satisfied``.

    The booleans are exactly what ``compliance_library``'s check functions
    concluded; this module does not re-decide them.
    """
    return {spec.check_id: bool(_library_result(spec, project_root).passed) for spec in _SPECS}


def policy_input_from_project(project_root: Path) -> PolicyInput:
    """Project the shared read into the snapshot the policy engine evaluates.

    ``compliance check`` evaluates a :class:`PolicyInput`; the policy library
    reads the project. Building the snapshot here from the library's own
    observations is what keeps the two surfaces from describing one install
    differently.

    Controls the policy engine expresses as a numeric threshold
    (``secrets_rotation_days``, ``password_min_length``,
    ``session_timeout_minutes``, ``audit_retention_days``) are left at their
    least-secure defaults: the library tests key presence for those, which is
    not a measurement of the threshold, and inventing one would be the
    overstatement this routing exists to remove.
    """
    observed = observe_compliance_controls(project_root)
    fields: dict[str, Any] = {
        spec.policy_input_field: observed[spec.check_id] for spec in _SPECS if spec.policy_input_field
    }
    return PolicyInput(**fields)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        return _digest_bytes(path.read_bytes())
    except OSError:
        return ABSENT


def _dir_digest(path: Path) -> str:
    """Digest a directory by the sorted names it holds.

    A directory has no bytes of its own, so the listing is what was read. An
    empty directory still digests to something -- the check's claim is that the
    directory exists, and the evidence says exactly what was found in it.
    """
    try:
        names = sorted(entry.name for entry in path.iterdir())
    except OSError:
        return ABSENT
    return _digest_bytes("\n".join(names).encode("utf-8"))


def _evidence_for(spec: ComplianceCheckSpec, project_root: Path) -> tuple[tuple[str, str], ...]:
    """Return ``(locator, digest)`` for every locator the check probes."""
    pairs: list[tuple[str, str]] = []
    for locator in spec.dir_locators:
        pairs.append((locator, _dir_digest(project_root / locator)))
    for locator in spec.file_locators:
        path = project_root / locator
        pairs.append((locator, _file_digest(path) if path.is_file() else ABSENT))
    return tuple(pairs)


def _read_something(evidence: Sequence[tuple[str, str]]) -> bool:
    return any(digest != ABSENT for _, digest in evidence)


# ---------------------------------------------------------------------------
# The producer
# ---------------------------------------------------------------------------


def _outcome_for(spec: ComplianceCheckSpec, project_root: Path) -> CheckOutcome:
    result = _library_result(spec, project_root)
    passed = bool(result.passed)
    evidence = _evidence_for(spec, project_root)

    measured = spec.source == "artifact" or (spec.source == "either" and _read_something(evidence))
    if measured:
        return CheckOutcome(
            check_id=spec.check_id,
            area=CMP_AREA,
            verdict=CheckVerdict.MEASURED,
            passed=passed,
            summary=f"{result.evidence} ({spec.asserts})",
            remediation=result.remediation,
            evidence=evidence,
        )

    return CheckOutcome(
        check_id=spec.check_id,
        area=CMP_AREA,
        verdict=CheckVerdict.DECLARED,
        passed=None,
        summary=f"{result.evidence} -- {_DECLARED_GAP} ({spec.asserts})",
        remediation=result.remediation,
        evidence=(),
    )


def select_check_ids(*, only: Iterable[str] = (), skip: Iterable[str] = ()) -> tuple[str, ...]:
    """Resolve ``--only`` / ``--skip`` selectors to check ids, ordered by id.

    ``only`` matches an id, an id namespace prefix (``CMP``) or the area name.
    A selector that matches nothing raises: an audit that silently ran no check
    is the failure mode this command exists to remove.
    """
    selectors = tuple(only)
    if selectors:
        chosen: set[str] = set()
        for selector in selectors:
            wanted = selector.strip().upper()
            matched = {
                spec.check_id
                for spec in _SPECS
                if spec.check_id.upper().startswith(wanted) or spec.area.upper() == wanted
            }
            if not matched:
                raise ValueError(f"no registered check matches selector {selector!r}")
            chosen |= matched
    else:
        chosen = set(_SPEC_BY_ID)

    for selector in skip:
        wanted = selector.strip().upper()
        if wanted not in _SPEC_BY_ID:
            raise ValueError(f"no registered check has id {selector!r}")
        chosen.discard(wanted)
    return tuple(sorted(chosen))


def run_compliance_checks(
    project_root: Path,
    *,
    only: Iterable[str] = (),
    skip: Iterable[str] = (),
) -> tuple[CheckOutcome, ...]:
    """Run the selected compliance checks over *project_root*, ordered by id."""
    return tuple(_outcome_for(_SPEC_BY_ID[cid], project_root) for cid in select_check_ids(only=only, skip=skip))


def count_by_outcome(outcomes: Iterable[CheckOutcome]) -> dict[str, int]:
    """Return the report's four denominators; there is no score and no grade."""
    counts = {"measured_pass": 0, "measured_fail": 0, "declared": 0, "not_measurable": 0}
    for outcome in outcomes:
        if outcome.verdict is CheckVerdict.MEASURED:
            counts["measured_pass" if outcome.passed else "measured_fail"] += 1
        elif outcome.verdict is CheckVerdict.DECLARED:
            counts["declared"] += 1
        else:
            counts["not_measurable"] += 1
    return counts


__all__ = [
    "ABSENT",
    "CMP_AREA",
    "CMP_NAMESPACE",
    "RETIRED_CHECK_IDS",
    "ComplianceCheckSpec",
    "ComplianceFramework",
    "count_by_outcome",
    "iter_compliance_checks",
    "observe_compliance_controls",
    "policy_input_from_project",
    "required_check_ids",
    "run_compliance_checks",
    "select_check_ids",
]
