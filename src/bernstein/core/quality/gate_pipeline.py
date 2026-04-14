"""Pipeline structure, data classes, and constants for quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

TIMED_OUT_PREFIX = "Timed out after "
NO_PYTHON_FILES = "No Python files changed."

if TYPE_CHECKING:
    from bernstein.core.quality.quality_gates import QualityGatesConfig

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
]


def build_default_pipeline(config: QualityGatesConfig) -> list[GatePipelineStep]:
    """Build the implicit pipeline used when the seed file omits one."""
    return [
        GatePipelineStep(name=name, required=required, condition=condition)
        for flag, name, required, condition in _DEFAULT_GATE_SPECS
        if _is_gate_enabled(config, flag)
    ]
