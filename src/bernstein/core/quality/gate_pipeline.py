"""Pipeline structure, data classes, and constants for quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

TIMED_OUT_PREFIX = "Timed out after "
NO_PYTHON_FILES = "No Python files changed."
# Prefix emitted by ``quality_gates._run_command`` when the subprocess could
# not be started at all (OSError from ``subprocess.run``: missing shell,
# vanished cwd, ...). Distinct from a non-zero exit with captured output,
# which means the tool ran and reported a real failure. The gate runner maps
# this prefix to ``inconclusive`` (reason ``evidence-missing``) — see
# ``_command_failure_result``.
COMMAND_ERROR_PREFIX = "Command error: "

if TYPE_CHECKING:
    from bernstein.core.quality.quality_gates import QualityGatesConfig

GateStatus = Literal["pass", "fail", "warn", "timeout", "skipped", "bypassed", "inconclusive", "command_not_found"]

# Closed set of reason codes for the ``"inconclusive"`` verdict.
#
# A gate returns ``"inconclusive"`` (with one of these reason codes) when it
# cannot honestly evaluate the evidence it was given — neither "pass" (which
# would be a bypass) nor "fail" (which would be a lie that trains operators
# to re-run until green). At a required gate, ``inconclusive`` blocks exactly
# like ``"fail"``; the difference is the claim, not the outcome.
#
# Issue #4181 (sibling slice to #4182, which carries the verdict into
# receipts and offline re-derivation). See ``gate_runner.py`` for the
# producer sites and ``GateResult.reason`` for the field that carries it.
INCONCLUSIVE_REASONS: frozenset[str] = frozenset(
    {
        # The gate found no evidence at all — no targets to scan, no journal
        # to verify, no file to read. Distinct from "evidence present but
        # unfavourable".
        "evidence-missing",
        # Evidence was located but could not be parsed or read back (binary
        # garbage, truncation, permission denied, encoding error). Distinct
        # from "evidence unfavourable".
        "evidence-unreadable",
        # The runner/subprocess died before producing output (timeout that
        # bypassed the structured timeout path, uncaught exception inside
        # the evaluator, subprocess killed by signal). Distinct from
        # "evidence unfavourable".
        "runner-died-before-output",
        # The command specified for the gate was not found (exit code 127).
        # Distinct from "evidence unfavourable" — no evidence was produced
        # because the tool itself is missing.
        "command-not-found",
    }
)

VALID_GATE_NAMES = frozenset(
    {
        "auto_format",
        "lint",
        "type_check",
        "tests",
        "pii_scan",
        "dlp_scan",
        "mutation_testing",
        "intent_verification",
        "security_scan",
        "coverage_delta",
        "complexity_check",
        "dead_code",
        "comment_quality",
        "import_cycle",
        "merge_conflict",
        "run_config",
        "benchmark",
        "dep_audit",
        "migration_reversibility",
        "large_file",
        "integration_test_gen",
        "review_rubric",
        "test_expansion",
        "agent_test_mutation",
        "behavior_probe",
        "incident_evals",
    }
)
VALID_GATE_CONDITIONS = frozenset({"always", "python_changed", "tests_changed", "any_changed", "deps_changed"})

_DEP_FILE_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
    }
)
_DEP_FILE_PREFIXES = ("requirements",)
LEGACY_PYTHON_CONDITION = "changed_files.any('.py')"


def is_dep_file(path: str) -> bool:
    """Return True if ``path`` looks like a dependency/lockfile."""
    from pathlib import PurePosixPath

    name = PurePosixPath(path).name
    if name in _DEP_FILE_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _DEP_FILE_PREFIXES)


def _empty_metadata() -> dict[str, Any]:
    """Return a typed empty metadata mapping."""
    return {}


def normalize_gate_condition(condition: str) -> str:
    """Normalize a pipeline condition string to the supported condition set."""
    normalized = LEGACY_PYTHON_CONDITION if condition == LEGACY_PYTHON_CONDITION else condition.strip()
    if normalized == LEGACY_PYTHON_CONDITION:
        return "python_changed"
    if normalized not in VALID_GATE_CONDITIONS:
        raise ValueError(f"Unsupported gate condition: {condition!r}")
    return normalized


@dataclass(frozen=True)
class GatePipelineStep:
    """Single gate step in the configured execution pipeline.

    Attributes:
        name: Gate name.
        required: Whether a failing gate blocks completion.
        condition: Execution condition keyed off the changed-file set.
        command_override: Optional shell command replacing the built-in gate behavior.
    """

    name: str
    required: bool
    condition: str = "always"
    command_override: str | None = None


@dataclass
class GateResult:
    """Result for one gate execution.

    Attributes:
        name: Gate name.
        status: Verdict from the closed ``GateStatus`` set. An
            ``"inconclusive"`` status MUST carry a ``reason`` drawn from
            :data:`INCONCLUSIVE_REASONS`; any other status MUST carry
            ``reason=None``. This invariant is enforced by
            ``__post_init__``.
        required: Whether this gate blocks completion on failure.
        blocked: Whether the runner is blocking promotion on this result.
            At a required gate, ``"inconclusive"`` sets ``blocked=True``
            just like ``"fail"`` — the verdict differs, the outcome does
            not (issue #4181).
        cached: Whether the result came from the per-task result cache.
        duration_ms: Wall-clock duration of the gate execution.
        details: Human-readable explanation (truncated at 2000 chars).
        metadata: Structured per-gate metadata (scores, regression lists,
            command strings). May not contain a ``"reason"`` key — that
            lives on the dedicated ``reason`` field above.
        reason: Closed-set reason code for an ``"inconclusive"`` status;
            ``None`` for every other status.
    """

    name: str
    status: GateStatus
    required: bool
    blocked: bool
    cached: bool
    duration_ms: int
    details: str
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    reason: str | None = None

    def __post_init__(self) -> None:
        """Enforce the ``status ↔ reason`` invariant from the docstring."""
        if self.status == "inconclusive":
            if self.reason is None:
                raise ValueError(
                    "GateResult.status='inconclusive' requires a reason drawn "
                    f"from INCONCLUSIVE_REASONS (got reason=None, name={self.name!r})"
                )
            if self.reason not in INCONCLUSIVE_REASONS:
                raise ValueError(
                    f"GateResult.reason={self.reason!r} is not in the closed "
                    f"set INCONCLUSIVE_REASONS={sorted(INCONCLUSIVE_REASONS)} "
                    f"(name={self.name!r})"
                )
        elif self.reason is not None:
            raise ValueError(
                f"GateResult.reason={self.reason!r} is only valid with "
                f"status='inconclusive'; got status={self.status!r} "
                f"(name={self.name!r})"
            )


@dataclass
class GateReport:
    """Structured report for all gates run for a task."""

    task_id: str
    overall_pass: bool
    total_duration_ms: int
    gates_run: list[str]
    results: list[GateResult]
    changed_files: list[str]
    cache_hits: int


def _is_gate_enabled(config: QualityGatesConfig, flag: str) -> bool:
    """Check whether a gate flag is enabled (supports nested .enabled attrs)."""
    value = getattr(config, flag, False)
    if hasattr(value, "enabled"):
        return bool(value.enabled)
    return bool(value)


# (config_flag, gate_name, required, condition)
_DEFAULT_GATE_SPECS: list[tuple[str, str, bool, str]] = [
    ("auto_format", "auto_format", False, "any_changed"),
    ("lint", "lint", True, "always"),
    ("type_check", "type_check", True, "python_changed"),
    ("tests", "tests", True, "python_changed"),
    ("security_scan", "security_scan", True, "python_changed"),
    ("complexity_check", "complexity_check", True, "python_changed"),
    ("dead_code_check", "dead_code", False, "python_changed"),
    ("comment_quality_check", "comment_quality", False, "python_changed"),
    ("import_cycle_check", "import_cycle", True, "python_changed"),
    ("coverage_delta", "coverage_delta", True, "python_changed"),
    ("merge_conflict_check", "merge_conflict", True, "any_changed"),
    # The run's own configuration is never part of a deliverable; a change
    # that carries it would rewrite the repository's configuration for
    # every user of it. Cheap (a set membership test over the changed-file
    # names) and required, so a leak stops the run instead of shipping.
    ("run_config", "run_config", True, "always"),
    ("pii_scan", "pii_scan", True, "any_changed"),
    ("dlp_scan", "dlp_scan", True, "any_changed"),
    ("mutation_testing", "mutation_testing", True, "python_changed"),
    ("intent_verification", "intent_verification", True, "any_changed"),
    ("dep_audit", "dep_audit", True, "deps_changed"),
    ("benchmark", "benchmark", True, "always"),
    ("migration_reversibility_check", "migration_reversibility", True, "any_changed"),
    ("large_file_check", "large_file", False, "any_changed"),
    ("integration_test_gen", "integration_test_gen", True, "python_changed"),
    ("review_rubric", "review_rubric", True, "python_changed"),
    ("test_expansion", "test_expansion", False, "python_changed"),
    ("agent_test_mutation", "agent_test_mutation", True, "tests_changed"),
    # Executes the changed callables against boundary inputs derived from
    # their own signatures. Heavy (one subprocess per probe) and
    # crash-level only, so it ships off and advisory; graduate on evidence.
    ("behavior_probe", "behavior_probe", False, "python_changed"),
    # P0 incident evals block merge; the gate runner reads severity from the
    # YAML files under src/bernstein/eval/cases/incidents/.
    ("incident_evals", "incident_evals", True, "always"),
]


def build_default_pipeline(config: QualityGatesConfig) -> list[GatePipelineStep]:
    """Build the implicit pipeline used when the seed file omits one."""
    return [
        GatePipelineStep(name=name, required=required, condition=condition)
        for flag, name, required, condition in _DEFAULT_GATE_SPECS
        if _is_gate_enabled(config, flag)
    ]
