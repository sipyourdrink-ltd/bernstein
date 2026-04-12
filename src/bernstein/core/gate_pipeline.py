"""Pipeline structure, data classes, and constants for quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

TIMED_OUT_PREFIX = "Timed out after "
NO_PYTHON_FILES = "No Python files changed."

if TYPE_CHECKING:
    from bernstein.core.quality_gates import QualityGatesConfig

GateStatus = Literal["pass", "fail", "warn", "timeout", "skipped", "bypassed"]

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
        "benchmark",
        "dep_audit",
        "migration_reversibility",
        "large_file",
        "integration_test_gen",
        "review_rubric",
        "test_expansion",
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
    """Result for one gate execution."""

    name: str
    status: GateStatus
    required: bool
    blocked: bool
    cached: bool
    duration_ms: int
    details: str
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)


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


def build_default_pipeline(config: QualityGatesConfig) -> list[GatePipelineStep]:
    """Build the implicit pipeline used when the seed file omits one."""
    pipeline: list[GatePipelineStep] = []
    if config.auto_format:
        pipeline.append(GatePipelineStep(name="auto_format", required=False, condition="any_changed"))
    if config.lint:
        pipeline.append(GatePipelineStep(name="lint", required=True, condition="always"))
    if config.type_check:
        pipeline.append(GatePipelineStep(name="type_check", required=True, condition="python_changed"))
    if config.tests:
        pipeline.append(GatePipelineStep(name="tests", required=True, condition="python_changed"))
    if config.security_scan:
        pipeline.append(GatePipelineStep(name="security_scan", required=True, condition="python_changed"))
    if config.complexity_check:
        pipeline.append(GatePipelineStep(name="complexity_check", required=True, condition="python_changed"))
    if config.dead_code_check:
        pipeline.append(GatePipelineStep(name="dead_code", required=False, condition="python_changed"))
    if config.comment_quality_check:
        pipeline.append(GatePipelineStep(name="comment_quality", required=False, condition="python_changed"))
    if config.import_cycle_check:
        pipeline.append(GatePipelineStep(name="import_cycle", required=True, condition="python_changed"))
    if config.coverage_delta:
        pipeline.append(GatePipelineStep(name="coverage_delta", required=True, condition="python_changed"))
    if config.merge_conflict_check:
        pipeline.append(GatePipelineStep(name="merge_conflict", required=True, condition="any_changed"))
    if config.pii_scan:
        pipeline.append(GatePipelineStep(name="pii_scan", required=True, condition="any_changed"))
    if config.dlp_scan:
        pipeline.append(GatePipelineStep(name="dlp_scan", required=True, condition="any_changed"))
    if config.mutation_testing:
        pipeline.append(GatePipelineStep(name="mutation_testing", required=True, condition="python_changed"))
    if config.intent_verification.enabled:
        pipeline.append(GatePipelineStep(name="intent_verification", required=True, condition="any_changed"))
    if config.dep_audit:
        pipeline.append(GatePipelineStep(name="dep_audit", required=True, condition="deps_changed"))
    if config.benchmark.enabled:
        pipeline.append(GatePipelineStep(name="benchmark", required=True, condition="always"))
    if config.migration_reversibility_check:
        pipeline.append(GatePipelineStep(name="migration_reversibility", required=True, condition="any_changed"))
    if config.large_file_check:
        pipeline.append(GatePipelineStep(name="large_file", required=False, condition="any_changed"))
    if config.integration_test_gen:
        pipeline.append(GatePipelineStep(name="integration_test_gen", required=True, condition="python_changed"))
    if config.review_rubric:
        pipeline.append(GatePipelineStep(name="review_rubric", required=True, condition="python_changed"))
    if config.test_expansion:
        pipeline.append(GatePipelineStep(name="test_expansion", required=False, condition="python_changed"))
    if config.agent_test_mutation:
        pipeline.append(GatePipelineStep(name="agent_test_mutation", required=True, condition="tests_changed"))
    return pipeline
