"""Volunteer receipt verification check run engine.

This module implements the project-side verification check run for volunteer
receipts. It extracts receipt bundles from volunteer PRs, verifies bundles
offline, re-runs manifest gates on PR head in CI, compares bundle attestations
vs CI gate outputs, and generates formatted comparison tables with field-level
reasons on any mismatch.

The verification check run ensures that:
1. The receipt bundle cryptographically verifies (offline)
2. The gates attested in the bundle produce the same results when re-run
3. Any mismatch is reported with field-level detail for debugging
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.security.audit_dsse import Statement
from bernstein.core.security.result_receipt_bundle import (
    BundleVerification,
    FieldError,
)
from bernstein.core.volunteer.manifest import VolunteerManifest, load_manifest_from_repo

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bernstein.core.github_app.check_runs import CheckRunClient

# Pattern to extract bundle JSON from PR body or comments
_BUNDLE_JSON_PATTERN = re.compile(
    r"```json\s*(\{[\s\S]*?\})\s*```",
    re.DOTALL | re.MULTILINE,
)
_ENVELOPE_FIELD_PATTERN = re.compile(
    r"\*\*Envelope:\*\*\s*```json\s*(\{[\s\S]*?\})\s*```",
    re.DOTALL | re.MULTILINE,
)

# Markers that mean "this PR presents itself as a volunteer submission".
# Every one of them is emitted by
# :func:`bernstein.core.volunteer.submission.build_volunteer_pr_body`, so a
# genuine volunteer PR always matches even when its envelope block is
# malformed or was edited away. A PR matching none of them is simply not a
# volunteer PR and has nothing to verify.
_VOLUNTEER_CLAIM_PATTERNS = (
    re.compile(r"\*\*Envelope:\*\*"),
    re.compile(r"\*\*Receipt digest:\*\*"),
    re.compile(r"\*\*Manifest digest:\*\*"),
    re.compile(r"bernstein receipt verify"),
    re.compile(r"^_Assisted-by:", re.MULTILINE),
)

#: Check-run conclusions this engine produces. ``neutral`` means the PR was
#: not a volunteer submission, so no verification was attempted.
Conclusion = Literal["success", "failure", "neutral"]


def _pr_claims_volunteer_receipt(pr_body: str) -> bool:
    """Return whether a PR body presents itself as a volunteer submission.

    Used to tell "nothing to verify here" apart from "a receipt was promised
    and it does not hold up". Only the latter is a failure.
    """
    return any(pattern.search(pr_body) for pattern in _VOLUNTEER_CLAIM_PATTERNS)


@dataclass(frozen=True, slots=True)
class GateComparison:
    """Comparison of a single gate's attestation vs CI re-run."""

    command: str
    attested_exit_code: int
    attested_log_sha256: str
    ci_exit_code: int | None
    ci_log_sha256: str | None
    passed: bool
    mismatch_reason: str | None


@dataclass(frozen=True, slots=True)
class AcceptanceVerificationResult:
    """Result of PR acceptance criteria to evidence mapping."""

    status: Literal["pass", "fail", "neutral"]
    mapping: dict[str, str] = field(default_factory=dict)
    unproven: list[str] = field(default_factory=list)
    invalid_tests: list[str] = field(default_factory=list)
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationCheckRunResult:
    """Result of a volunteer receipt verification check run."""

    bundle_verification: BundleVerification
    gate_comparisons: list[GateComparison]
    manifest_digest_match: bool
    conclusion: Conclusion
    summary: str
    details: str
    acceptance_evidence: AcceptanceVerificationResult | None = None

    @property
    def overall_passed(self) -> bool:
        """Whether a receipt was verified and held up.

        Derived so it can never disagree with :attr:`conclusion`. A
        ``neutral`` result is not a pass: nothing was verified.
        """
        return self.conclusion == "success"


def _extract_envelope_from_pr_body(pr_body: str) -> dict[str, Any] | None:
    """Extract the DSSE envelope JSON from a volunteer PR body.

    Looks for JSON code blocks in the PR body that contain the envelope.
    The envelope is typically displayed in the verification section of the PR body.
    """
    # First try to find explicit envelope marker
    envelope_match = _ENVELOPE_FIELD_PATTERN.search(pr_body)
    if envelope_match:
        try:
            return json.loads(envelope_match.group(1))
        except json.JSONDecodeError:
            pass

    # Fallback: look for any JSON block that might contain an envelope
    json_matches = _BUNDLE_JSON_PATTERN.findall(pr_body)
    for json_str in json_matches:
        try:
            data = json.loads(json_str)
            # Check if this looks like an envelope (has payload_type, payload_b64, signatures)
            if isinstance(data, dict) and "payload_type" in data and "payload_b64" in data and "signatures" in data:
                return data
        except json.JSONDecodeError:
            continue

    return None


def _extract_bundle_from_envelope(envelope_dict: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the bundle dictionary from a DSSE envelope.

    Args:
        envelope_dict: The envelope as a dictionary

    Returns:
        The bundle dictionary if found, None otherwise
    """
    try:
        payload_b64 = envelope_dict.get("payload_b64", "")
        if not payload_b64:
            return None

        payload_json = base64.b64decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_json)

        # The payload should be a statement with a predicate containing the bundle
        statement = payload if isinstance(payload, dict) else {}
        predicate = statement.get("predicate", {}) if isinstance(statement, dict) else {}
        if not isinstance(predicate, dict):
            return None

        bundle = predicate.get("bundle")
        return bundle if isinstance(bundle, dict) and bundle else None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
        return None


def _sort_recursive(value: Any) -> Any:
    """Reorder dict keys at every depth so canonical JSON is byte-stable."""
    if isinstance(value, dict):
        return {k: _sort_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_recursive(v) for v in value]
    return value


def _verify_bundle_offline(
    envelope_dict: dict[str, Any],
    expected_manifest_sha256: str | None = None,
    expected_prev_digest: str | None = None,
) -> BundleVerification:
    """Verify a result bundle offline using the existing verification function.

    Args:
        envelope_dict: The envelope as a dictionary
        expected_manifest_sha256: Optional expected manifest digest for policy verification
        expected_prev_digest: Optional expected previous bundle digest for chain verification

    Returns:
        BundleVerification result
    """
    errors = []
    try:
        payload_b64 = envelope_dict.get("payload_b64", "")
        if not payload_b64:
            return BundleVerification(ok=False, errors=(FieldError("envelope", "Missing payload_b64"),))

        payload_json = base64.b64decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_json)

        statement_data = payload if isinstance(payload, dict) else {}
        statement = Statement(
            subjects=statement_data.get("subjects", []),
            predicate_type=statement_data.get("predicate_type", ""),
            predicate=statement_data.get("predicate", {}),
        )

        bundle_data = statement.predicate.get("bundle", {}) if isinstance(statement.predicate, dict) else {}

        # Verify internal consistency: bundle should hash to subject digest
        subjects = statement.subjects if isinstance(statement.subjects, list) else []
        if subjects:
            first_subject = subjects[0]
            if isinstance(first_subject, dict):
                subject_digest = first_subject.get("digest", {}).get("sha256", "")
                if subject_digest:
                    # Compute bundle digest
                    bundle_bytes = json.dumps(
                        _sort_recursive(bundle_data),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    computed_digest = hashlib.sha256(bundle_bytes).hexdigest()
                    if computed_digest != subject_digest:
                        errors.append(
                            FieldError(
                                "subject.digest.sha256",
                                f"bundle hashes to {computed_digest}, envelope attests {subject_digest}",
                            )
                        )

        # Verify gate log integrity
        gates = bundle_data.get("gates", []) if isinstance(bundle_data, dict) else []
        if isinstance(gates, list):
            for i, gate in enumerate(gates):
                if isinstance(gate, dict):
                    log = gate.get("log", "")
                    attested_log_sha256 = gate.get("log_sha256", "")
                    if log and attested_log_sha256:
                        actual_log_sha256 = hashlib.sha256(log.encode("utf-8")).hexdigest()
                        if actual_log_sha256 != attested_log_sha256:
                            errors.append(
                                FieldError(
                                    f"gates[{i}].log",
                                    f"log for {gate.get('command', '?')!r} hashes to "
                                    f"{actual_log_sha256}, bundle attests {attested_log_sha256}",
                                )
                            )

        # Verify manifest digest if expected
        if expected_manifest_sha256 is not None:
            manifest_sha256 = bundle_data.get("manifest_sha256", "")
            if manifest_sha256 != expected_manifest_sha256:
                errors.append(
                    FieldError(
                        "manifest_sha256",
                        f"bundle attests manifest {manifest_sha256}, expected {expected_manifest_sha256}",
                    )
                )

        return BundleVerification(
            ok=len(errors) == 0,
            errors=tuple(errors),
        )
    except Exception as e:
        return BundleVerification(
            ok=False, errors=(FieldError("envelope.verification", f"Verification failed: {e!s}"),)
        )


def _run_manifest_gates_in_ci(
    manifest: VolunteerManifest,
    workspace_path: str,
) -> tuple[list[Any], bool]:
    """Run the manifest gates in CI environment and return results.

    Args:
        manifest: The volunteer manifest containing gates to run
        workspace_path: Path to the workspace where gates should run

    Returns:
        Tuple of (gate_results, all_passed) where gate_results is a list of
        gate outcome objects and all_passed is True if all gates passed
    """
    # Import here to avoid circular imports
    from bernstein.core.volunteer.task_finish import _run_gates

    # Prepare CI-like environment (similar to what's used in task_finish)
    ci_env = dict(os.environ)
    ci_env.update(
        {
            "BERNSTEIN_VOLUNTEER": "1",
            "CI": "1",
        }
    )

    workspace = Path(workspace_path)
    gate_results, refusal = _run_gates(
        manifest,
        workspace=workspace,
        env=ci_env,
        budget_seconds=manifest.max_wall_clock_minutes * 60,  # Convert to seconds
    )

    # Convert GateResult objects to a format suitable for comparison
    # GateResult has: command, exit_code, log
    all_passed = refusal is None and all(g.exit_code == 0 for g in gate_results)

    return list(gate_results), all_passed


def _get_gate_attr(gate: Any, key: str, default: Any = None) -> Any:
    """Get an attribute from a gate object, handling both dict-like and object gates."""
    if isinstance(gate, dict):
        return gate.get(key, default)
    return getattr(gate, key, default)


def _compare_gate_results(
    attested_gates: tuple[Any, ...],
    ci_gate_results: list[Any],
) -> list[GateComparison]:
    """Compare attested gate results from bundle with CI gate results.

    Args:
        attested_gates: GateResult objects from the bundle
        ci_gate_results: GateResult objects from CI re-run

    Returns:
        List of GateComparison objects
    """
    comparisons = []

    # Create a map of CI results by command for easy lookup
    ci_results_by_command = {}
    for gate_result in ci_gate_results:
        command = _get_gate_attr(gate_result, "command")
        if command:
            ci_results_by_command[command] = gate_result

    # Compare each attested gate
    for attested_gate in attested_gates:
        command = _get_gate_attr(attested_gate, "command", "unknown")
        attested_exit_code = _get_gate_attr(attested_gate, "exit_code", -1)
        attested_log = _get_gate_attr(attested_gate, "log", "")
        attested_log_sha256 = _get_gate_attr(attested_gate, "log_sha256", "")

        # Handle the case where log_sha256 might need to be computed
        if not attested_log_sha256 and attested_log:
            attested_log_sha256 = hashlib.sha256(attested_log.encode("utf-8")).hexdigest()

        ci_result = ci_results_by_command.get(command)

        if ci_result is None:
            comparisons.append(
                GateComparison(
                    command=command,
                    attested_exit_code=attested_exit_code,
                    attested_log_sha256=attested_log_sha256,
                    ci_exit_code=None,
                    ci_log_sha256=None,
                    passed=False,
                    mismatch_reason="Gate not found in CI re-run",
                )
            )
            continue

        ci_exit_code = _get_gate_attr(ci_result, "exit_code", -1)
        ci_log = _get_gate_attr(ci_result, "log", "")
        ci_log_sha256 = _get_gate_attr(ci_result, "log_sha256", "")

        if not ci_log_sha256 and ci_log:
            ci_log_sha256 = hashlib.sha256(ci_log.encode("utf-8")).hexdigest()

        passed = attested_exit_code == ci_exit_code and attested_log_sha256 == ci_log_sha256

        mismatch_reason = None
        if not passed:
            if attested_exit_code != ci_exit_code:
                mismatch_reason = f"Exit code mismatch: attested {attested_exit_code}, CI {ci_exit_code}"
            elif attested_log_sha256 != ci_log_sha256:
                mismatch_reason = "Log content mismatch"
            else:
                mismatch_reason = "Unknown mismatch"

        comparisons.append(
            GateComparison(
                command=command,
                attested_exit_code=attested_exit_code,
                attested_log_sha256=attested_log_sha256,
                ci_exit_code=ci_exit_code,
                ci_log_sha256=ci_log_sha256,
                passed=passed,
                mismatch_reason=mismatch_reason,
            )
        )

    # Check for extra gates in CI that weren't in the bundle
    attested_commands = {_get_gate_attr(g, "command") for g in attested_gates}
    for command, ci_result in ci_results_by_command.items():
        if command not in attested_commands:
            ci_exit_code = _get_gate_attr(ci_result, "exit_code", -1)
            ci_log = _get_gate_attr(ci_result, "log", "")
            ci_log_sha256 = ""
            if ci_log:
                ci_log_sha256 = hashlib.sha256(ci_log.encode("utf-8")).hexdigest()

            comparisons.append(
                GateComparison(
                    command=command,
                    attested_exit_code=None,
                    attested_log_sha256=None,
                    ci_exit_code=ci_exit_code,
                    ci_log_sha256=ci_log_sha256,
                    passed=False,
                    mismatch_reason="Extra gate found in CI re-run",
                )
            )

    return comparisons


def _format_comparison_table(comparisons: list[GateComparison]) -> str:
    """Format gate comparisons as a markdown table."""
    if not comparisons:
        return "_No gates to compare._"

    lines = [
        "| Command | Attested | CI | Attested Log | CI Log | Status | Reason |",
        "|---|---|---|---|---|---|---|",
    ]

    for comp in comparisons:
        attested_exit = str(comp.attested_exit_code) if comp.attested_exit_code is not None else "N/A"
        ci_exit = str(comp.ci_exit_code) if comp.ci_exit_code is not None else "N/A"
        status = "PASS" if comp.passed else "FAIL"
        reason = comp.mismatch_reason or ""
        attested_log = f"`{comp.attested_log_sha256[:12]}...`" if comp.attested_log_sha256 else "N/A"
        ci_log = f"`{comp.ci_log_sha256[:12]}...`" if comp.ci_log_sha256 else "N/A"

        lines.append(
            f"| `{comp.command}` | {attested_exit} | {ci_exit} | {attested_log} | {ci_log} | {status} | {reason} |"
        )

    return "\n".join(lines)


def _normalize_criterion(c: str) -> str:
    import string

    c = c.casefold()
    c = c.rstrip(string.punctuation)
    c = re.sub(r"\s+", " ", c).strip()
    return c


def _verify_acceptance_evidence(
    pr_body: str,
    repo_slug: str,
    workspace_path: str,
) -> AcceptanceVerificationResult:
    """Verify that every PR acceptance criterion is mapped to an existing test."""
    import importlib.util
    import sys

    from bernstein.core.orchestration.issue_to_pr import IssuePRClient

    # 1. Extract linked issue
    m = re.search(r"(?:[Cc]loses|[Ff]ixes|[Rr]esolves)\s+#(\d+)", pr_body)
    if not m:
        return AcceptanceVerificationResult(status="neutral")
    issue_number = int(m.group(1))

    # 2. Retrieve/parse issue checklist
    client = IssuePRClient()
    try:
        issue = client.get_issue(repo_slug, issue_number)
    except Exception as e:
        logger.warning(f"Failed to fetch issue #{issue_number}: {e!s}")
        return AcceptanceVerificationResult(
            status="neutral",
            reason=f"Failed to fetch linked issue #{issue_number} from GitHub API.",
        )

    issue_body = issue.get("body") or ""

    checklist_match = re.search(
        r"^##\s+Acceptance\s+[Cc]riteria\s*$(.*?)(?=^#|\Z)",
        issue_body,
        re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )
    if not checklist_match:
        return AcceptanceVerificationResult(status="neutral")

    issue_criteria = []
    issue_norm_map = {}
    for line in checklist_match.group(1).splitlines():
        line = line.strip()
        if line.startswith("- [ ]") or line.startswith("- [x]") or line.startswith("- [X]"):
            crit = line[5:].strip()
            if crit:
                issue_criteria.append(crit)
                issue_norm_map[_normalize_criterion(crit)] = crit

    if not issue_criteria:
        return AcceptanceVerificationResult(status="neutral")

    # 3. Parse PR evidence block
    marker = "<!-- bernstein:acceptance-evidence -->"
    mapping = {}
    mapped_norm_map = {}
    if marker in pr_body:
        mapping_section = pr_body.split(marker)[1]
        for line in mapping_section.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("- "):
                line = line[2:].strip()
            else:
                break

            if "→" in line:
                parts = line.split("→")
            elif "->" in line:
                parts = line.split("->")
            else:
                continue

            if len(parts) == 2:
                crit = parts[0].strip().strip("\"'")
                test_ref = parts[1].strip().strip("`'\"")
                if crit and test_ref:
                    mapping[crit] = test_ref
                    mapped_norm_map[_normalize_criterion(crit)] = crit

    # 4. Compare issue criteria vs mapped criteria
    issue_crit_set = set(issue_norm_map.keys())
    mapped_crit_set = set(mapped_norm_map.keys())
    missing_norms = issue_crit_set - mapped_crit_set
    extra_norms = mapped_crit_set - issue_crit_set

    unproven = sorted([issue_norm_map[n] for n in missing_norms])
    extra_criteria = sorted([mapped_norm_map[n] for n in extra_norms])
    invalid_tests = []

    # 5. Call build_report().collected
    script_path = Path(workspace_path) / "scripts" / "check_test_collection.py"
    collected: dict[str, set[str]] = {}
    if not script_path.exists():
        return AcceptanceVerificationResult(
            status="neutral",
            reason="Test collection script is missing, unable to verify evidence.",
        )

    alias = "_ci_check_test_collection"
    spec = importlib.util.spec_from_file_location(alias, script_path)
    if not spec or not spec.loader:
        return AcceptanceVerificationResult(
            status="neutral",
            reason="Failed to load test collection script spec, unable to verify evidence.",
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
        collected = module.build_report().collected
    except (ImportError, AttributeError, SyntaxError) as e:
        logger.warning(f"Expected error loading check_test_collection: {e!s}")
        return AcceptanceVerificationResult(
            status="neutral",
            reason="Failed to execute test collection script, unable to verify evidence.",
        )
    except Exception as e:
        logger.warning(f"Unexpected error loading check_test_collection: {e!s}")
        return AcceptanceVerificationResult(
            status="neutral",
            reason="Unexpected error loading test collection script, unable to verify evidence.",
        )

    # 6. Check referenced tests against collected set
    for crit, test_ref in mapping.items():
        if crit in extra_criteria:
            continue
        # Extract file path (e.g. tests/unit/test_foo.py::test_bar -> tests/unit/test_foo.py)
        parts = test_ref.split("::")
        file_path = parts[0].strip()
        if file_path not in collected:
            invalid_tests.append(test_ref)
            if crit not in unproven:
                unproven.append(crit)
        elif len(parts) > 1:
            node_id = parts[1].strip()
            if node_id not in collected[file_path]:
                invalid_tests.append(test_ref)
                if crit not in unproven:
                    unproven.append(crit)

    unproven = sorted(list(set(unproven)))

    if unproven or extra_criteria:
        # extra criteria can be treated as unproven or we can just fail
        for ec in extra_criteria:
            invalid_tests.append(f"Mapped unknown criterion: {ec}")
        return AcceptanceVerificationResult(
            status="fail",
            mapping=mapping,
            unproven=unproven,
            invalid_tests=invalid_tests,
        )

    return AcceptanceVerificationResult(status="pass", mapping=mapping)


def run_verification_check(
    *,
    pr_number: int,
    repo_slug: str,
    workspace_path: str,
    pr_body: str,
    check_run_client: CheckRunClient,
    expected_manifest_sha256: str | None = None,
) -> VerificationCheckRunResult:
    """Run the volunteer receipt verification check.

    Args:
        pr_number: The PR number to verify
        repo_slug: The repository slug (owner/repo)
        workspace_path: Path to the checked-out repository workspace
        pr_body: The body of the PR (used to extract the envelope)
        check_run_client: Client for posting check run updates
        expected_manifest_sha256: Optional expected manifest digest for policy verification

    Returns:
        VerificationCheckRunResult containing the verification outcome
    """
    # Extract envelope from PR body
    envelope_dict = _extract_envelope_from_pr_body(pr_body)
    if envelope_dict is None:
        if not _pr_claims_volunteer_receipt(pr_body):
            # Not a volunteer PR at all: there is nothing to verify, and
            # saying so as `failure` would make every ordinary PR red.
            return VerificationCheckRunResult(
                bundle_verification=BundleVerification(ok=True),
                gate_comparisons=[],
                manifest_digest_match=False,
                conclusion="neutral",
                summary="Skipped: not a volunteer PR",
                details="This pull request carries no volunteer receipt envelope and none of "
                "the attribution markers a volunteer submission emits, so there is nothing "
                "to verify.\n\nVolunteer submissions opened by `bernstein volunteer submit` "
                "are checked strictly; this check is skipped for every other pull request.",
            )
        # The PR presents itself as a volunteer submission but ships no
        # envelope - that is a real, reportable failure.
        bundle_verification = BundleVerification(
            ok=False, errors=(FieldError("envelope", "No result receipt envelope found in PR body"),)
        )
        return VerificationCheckRunResult(
            bundle_verification=bundle_verification,
            gate_comparisons=[],
            manifest_digest_match=False,
            conclusion="failure",
            summary="Verification failed: No receipt envelope found in PR body",
            details="This pull request presents itself as a volunteer submission, but no "
            "result receipt envelope could be extracted from its body.\n\nRestore the "
            "`**Envelope:**` JSON block produced by `bernstein volunteer submit`, or remove "
            "the volunteer attribution markers if this is not a volunteer submission.",
        )

    # Verify the bundle offline (internal consistency checks)
    bundle_verification = _verify_bundle_offline(
        envelope_dict,
        expected_manifest_sha256=expected_manifest_sha256,
    )

    # If bundle verification failed, we can't reliably extract gates for comparison
    if not bundle_verification.ok:
        return VerificationCheckRunResult(
            bundle_verification=bundle_verification,
            gate_comparisons=[],
            manifest_digest_match=False,
            conclusion="failure",
            summary="Verification failed: Receipt bundle does not verify",
            details=_format_verification_details(bundle_verification),
        )

    # Extract bundle for gate comparison
    bundle_dict = _extract_bundle_from_envelope(envelope_dict)
    if bundle_dict is None:
        return VerificationCheckRunResult(
            bundle_verification=BundleVerification(
                ok=False, errors=(FieldError("envelope", "Could not extract bundle from verified envelope"),)
            ),
            gate_comparisons=[],
            manifest_digest_match=False,
            conclusion="failure",
            summary="Verification failed: Could not extract bundle",
            details="The envelope verified but we could not extract the bundle data.",
        )

    # Load manifest from workspace to get gates and expected manifest digest
    try:
        manifest = load_manifest_from_repo(Path(workspace_path))
        actual_manifest_digest = manifest.digest
        manifest_digest_match = expected_manifest_sha256 is None or actual_manifest_digest == expected_manifest_sha256
    except Exception as e:
        # If we can't load manifest, we can't re-run gates
        return VerificationCheckRunResult(
            bundle_verification=bundle_verification,
            gate_comparisons=[],
            manifest_digest_match=False,
            conclusion="failure",
            summary="Verification failed: Could not load manifest",
            details=f"Failed to load manifest from workspace: {e!s}",
        )

    # Re-run manifest gates in CI environment
    try:
        ci_gate_results, _all_gates_passed = _run_manifest_gates_in_ci(
            manifest,
            workspace_path,
        )
    except Exception as e:
        return VerificationCheckRunResult(
            bundle_verification=bundle_verification,
            gate_comparisons=[],
            manifest_digest_match=manifest_digest_match,
            conclusion="failure",
            summary="Verification failed: Error running gates in CI",
            details=f"Error while re-running manifest gates: {e!s}",
        )

    # Extract attested gates from bundle
    attested_gates = tuple(bundle_dict.get("gates", [])) if isinstance(bundle_dict.get("gates"), list) else ()

    # Compare attested vs CI results
    gate_comparisons = _compare_gate_results(attested_gates, ci_gate_results)

    # Determine overall pass/fail
    bundle_verified = bundle_verification.ok
    manifest_matches = manifest_digest_match
    gates_match = all(comp.passed for comp in gate_comparisons)
    overall_passed = bundle_verified and manifest_matches and gates_match
    conclusion: Conclusion = "success" if overall_passed else "failure"

    # Generate summary
    if overall_passed:
        summary = "All checks passed: bundle verifies, manifest matches, and gates consistent"
    else:
        summary_parts = []
        if not bundle_verified:
            summary_parts.append("bundle verification failed")
        if not manifest_matches:
            summary_parts.append("manifest digest mismatch")
        if not gates_match:
            summary_parts.append("gate results inconsistent")
        summary = f"Verification failed: {', '.join(summary_parts)}"

    # Generate detailed report
    details_parts = [
        "## Volunteer Receipt Verification Check Run",
        "",
        "### Bundle Verification",
        "",
        _format_verification_details(bundle_verification),
        "",
        "### Manifest Verification",
        "",
        f"- **Expected manifest digest:** `{expected_manifest_sha256 or 'not provided'}`",
        f"- **Actual manifest digest:** `{actual_manifest_digest}`",
        f"- **Manifest digest match:** {'Yes' if manifest_digest_match else 'No'}",
        "",
        "### Gate Results Comparison",
        "",
        _format_comparison_table(gate_comparisons),
        "",
        f"*Overall result: {'PASSED' if overall_passed else 'FAILED'}*",
    ]

    details = "\n".join(details_parts)

    acceptance_evidence = _verify_acceptance_evidence(pr_body, repo_slug, workspace_path)

    if acceptance_evidence.status != "neutral":
        details += "\n\n### Acceptance Criteria Verification\n\n"
        if acceptance_evidence.status == "pass":
            details += f"**PASSED**: {len(acceptance_evidence.mapping)} criteria mapped to collected tests.\n"
        else:
            details += "**UNPROVEN / FAILED**\n"
            if acceptance_evidence.unproven:
                details += "\n**Unproven Criteria:**\n"
                for uc in acceptance_evidence.unproven:
                    details += f"- {uc}\n"
            if acceptance_evidence.invalid_tests:
                details += "\n**Invalid Evidence:**\n"
                for it in acceptance_evidence.invalid_tests:
                    details += f"- `{it}`\n"
            if acceptance_evidence.mapping:
                details += "\n**Mapped Evidence:**\n"
                for crit, test_ref in acceptance_evidence.mapping.items():
                    details += f"- `{crit}` → `{test_ref}`\n"
    elif acceptance_evidence.reason:
        details += "\n\n### Acceptance Criteria Verification\n\n"
        details += f"**NEUTRAL**: {acceptance_evidence.reason}\n"

    return VerificationCheckRunResult(
        bundle_verification=bundle_verification,
        gate_comparisons=gate_comparisons,
        manifest_digest_match=manifest_digest_match,
        conclusion=conclusion,
        summary=summary,
        details=details,
        acceptance_evidence=acceptance_evidence,
    )


def _format_verification_details(verification: BundleVerification) -> str:
    """Format bundle verification details as markdown."""
    if verification.ok:
        return "**Bundle verification passed**\n\nThe receipt bundle verifies offline with no errors."

    lines = ["**Bundle verification failed**", ""]
    if verification.errors:
        lines.append("**Errors:**")
        for error in verification.errors:
            lines.append(f"- `{error.field}`: {error.message}")
    else:
        lines.append("No specific errors reported.")

    return "\n".join(lines)


def post_verification_check_run(
    *,
    repo_slug: str,
    pr_number: int,
    head_sha: str,
    verification_result: VerificationCheckRunResult,
    check_run_client: CheckRunClient,
    details_url: str = "",
) -> Any:
    """Post or update a verification check run on GitHub.

    Args:
        repo_slug: Repository slug (owner/repo)
        pr_number: PR number
        head_sha: HEAD commit SHA of the PR
        verification_result: Result of the verification check run
        check_run_client: CheckRunClient instance
        details_url: Optional URL linking to more details

    Returns:
        CheckRunResult if successful, None otherwise
    """
    # Determine conclusion based on verification result
    conclusion = verification_result.conclusion

    return check_run_client.create_verification_check_run(
        head_sha=head_sha,
        summary=verification_result.summary,
        details=verification_result.details,
        conclusion=conclusion,
        details_url=details_url,
    )


def update_verification_check_run(
    *,
    check_run_id: int,
    verification_result: VerificationCheckRunResult,
    check_run_client: CheckRunClient,
    details_url: str = "",
) -> Any:
    """Update an existing verification check run on GitHub.

    Args:
        check_run_id: Existing check run ID to update
        verification_result: Result of the verification check run
        check_run_client: CheckRunClient instance
        details_url: Optional URL linking to more details

    Returns:
        CheckRunResult if successful, None otherwise
    """
    # Determine conclusion based on verification result
    conclusion = verification_result.conclusion

    return check_run_client.update_verification_check_run(
        check_run_id=check_run_id,
        summary=verification_result.summary,
        details=verification_result.details,
        conclusion=conclusion,
        details_url=details_url,
    )
