"""Async quality gate runner with incremental execution and cached reports."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from bernstein.core.telemetry import start_span

_NO_PYTHON_FILES = "No Python files changed."
_TIMED_OUT_PREFIX = "Timed out after "

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.models import Task
    from bernstein.core.quality.comment_quality import DocstyleKind
    from bernstein.core.quality.gate_plugins import GatePluginRegistry
    from bernstein.core.quality.quality_gates import QualityGatesConfig

logger = logging.getLogger(__name__)

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


def _is_dep_file(path: str) -> bool:
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


def _module_name_from_path(path: Path, root: Path) -> str:
    """Return the dotted module name for a Python file relative to ``root``."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ""
    parts = list(rel.parts)
    if not parts:
        return ""
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _resolve_import_from(module_name: str, level: int, imported_module: str | None) -> str:
    """Resolve an ``ImportFrom`` target against a current module name."""
    if level <= 0:
        return imported_module or ""
    parts = module_name.split(".")
    package_parts = parts[:-1]
    if level > len(package_parts):
        return imported_module or ""
    base_parts = package_parts[: len(package_parts) - level + 1]
    if imported_module:
        base_parts.extend(imported_module.split("."))
    return ".".join(part for part in base_parts if part)


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


def _migration_downgrade_is_pass(source: str) -> bool:
    """Return True when an Alembic downgrade() body contains only ``pass``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "downgrade":
            continue
        body = node.body
        # Strip leading docstring if present.
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        if not body:
            return True
        return len(body) == 1 and isinstance(body[0], ast.Pass)
    return False


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


class GateRunner:
    """Run a configured quality-gate pipeline for one task.

    Args:
        config: Quality gates configuration.
        workdir: Repository root containing ``.sdd``.
        base_ref: Git base ref for incremental diff fallback.
    """

    def __init__(self, config: QualityGatesConfig, workdir: Path, *, base_ref: str = "main") -> None:
        self._config = config
        self._workdir = workdir
        self._base_ref = base_ref
        self._cache_lock = threading.Lock()
        self._cache_loaded = False
        self._cache_entries: dict[str, dict[str, Any]] = {}
        self._changed_files_resolved = True
        self._gate_plugin_registry: GatePluginRegistry | None = None

    async def run_all(
        self,
        task: Task,
        run_dir: Path,
        *,
        skip_gates: Iterable[str] | None = None,
        bypass_reason: str | None = None,
    ) -> GateReport:
        """Run the configured gate pipeline and persist a per-task report."""
        started = time.perf_counter()
        skip_set = {gate.strip() for gate in skip_gates or () if gate.strip()}
        if skip_set and not self._config.allow_bypass:
            raise ValueError("quality gate bypass is disabled by configuration")

        resolved_changed_files = await asyncio.to_thread(self._resolve_changed_files_sync, task, run_dir)
        self._changed_files_resolved = resolved_changed_files is not None
        changed_files = sorted(resolved_changed_files or [])
        pipeline = self._resolve_pipeline()

        # Run auto_format steps first (they modify files) before parallel gates.
        format_steps = [s for s in pipeline if s.name == "auto_format"]
        other_steps = [s for s in pipeline if s.name != "auto_format"]

        format_results: list[GateResult] = []
        for step in format_steps:
            format_results.append(
                await self._run_step(
                    step,
                    task,
                    run_dir,
                    changed_files,
                    skip_set=skip_set,
                    bypass_reason=bypass_reason,
                )
            )

        other_results = list(
            await asyncio.gather(
                *[
                    self._run_step(
                        step,
                        task,
                        run_dir,
                        changed_files,
                        skip_set=skip_set,
                        bypass_reason=bypass_reason,
                    )
                    for step in other_steps
                ]
            )
        )
        results = format_results + other_results
        report = GateReport(
            task_id=task.id,
            overall_pass=all(not result.blocked for result in results),
            total_duration_ms=int((time.perf_counter() - started) * 1000),
            gates_run=[result.name for result in results],
            results=results,
            changed_files=changed_files,
            cache_hits=sum(1 for result in results if result.cached),
        )
        await asyncio.to_thread(self._persist_report_sync, report)
        return report

    async def run_gate(
        self,
        step: GatePipelineStep,
        task: Task,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run one gate step and return its structured result."""
        cache_key = await asyncio.to_thread(self._make_cache_key_sync, step, run_dir, changed_files)
        if cache_key is not None:
            cached_result = await asyncio.to_thread(self._get_cached_result_sync, cache_key)
            if cached_result is not None:
                cached_result.cached = True
                return cached_result

        started = time.perf_counter()
        with start_span(
            "quality_gate.execute",
            {
                "task.id": task.id,
                "quality_gate.name": step.name,
                "quality_gate.required": step.required,
            },
        ) as span:
            result = await self._execute_gate(step, task, run_dir, changed_files)
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            if span is not None:
                span.set_attribute("quality_gate.status", result.status)
                span.set_attribute("quality_gate.blocked", result.blocked)
                span.set_attribute("quality_gate.cached", result.cached)
                span.set_attribute("quality_gate.duration_ms", result.duration_ms)

        if cache_key is not None and result.status in {"pass", "fail", "skipped"}:
            await asyncio.to_thread(self._store_cached_result_sync, cache_key, result)
        return result

    async def _run_step(
        self,
        step: GatePipelineStep,
        task: Task,
        run_dir: Path,
        changed_files: list[str],
        *,
        skip_set: set[str],
        bypass_reason: str | None,
    ) -> GateResult:
        if step.name in skip_set:
            return GateResult(
                name=step.name,
                status="bypassed",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=bypass_reason or "Bypassed via CLI.",
                metadata={"reason": bypass_reason or "", "actor": "cli"},
            )
        if not self._condition_matches(step.condition, changed_files):
            return GateResult(
                name=step.name,
                status="skipped",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=f"Skipped because condition {step.condition!r} was not met.",
                metadata={},
            )
        return await self.run_gate(step, task, run_dir, changed_files)

    async def _execute_gate(
        self,
        step: GatePipelineStep,
        task: Task,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        from bernstein.core import quality_gates as qg

        if step.name == "auto_format":
            return await asyncio.to_thread(self._run_auto_format_gate_sync, step, run_dir, changed_files)

        if step.name == "test_expansion":
            return await asyncio.to_thread(self._run_test_expansion_gate_sync, step, task, run_dir, changed_files)

        if step.name == "lint":
            command = self._lint_command(step, changed_files)
            if command is None:
                return self._skipped(step, _NO_PYTHON_FILES)
            return await self._run_command_gate(
                step,
                command,
                run_dir,
                self._config.timeout_s,
                pass_detail="no lint violations",
            )

        if step.name == "type_check":
            command = self._type_check_command(step, changed_files)
            if command is None:
                return self._skipped(step, _NO_PYTHON_FILES)
            return await self._run_command_gate(
                step,
                command,
                run_dir,
                self._config.timeout_s,
                pass_detail="no type errors",
            )

        if step.name == "tests":
            return await self._run_tests_gate(step, task, run_dir, changed_files)

        if step.name == "security_scan":
            command = self._optional_command("security_scan", step.command_override)
            if command is None:
                return self._skipped(step, "No security scan command configured.")
            return await self._run_command_gate(
                step,
                command,
                run_dir,
                self._config.timeout_s,
                pass_detail="no security issues found",
            )

        if step.name == "pii_scan":
            pii_result = await asyncio.to_thread(
                qg.run_pii_gate_sync,
                self._config,
                run_dir,
                changed_files if self._changed_files_resolved else None,
            )
            status: GateStatus = "pass" if pii_result.passed else "fail"
            return GateResult(
                name="pii_scan",
                status=status,
                required=step.required,
                blocked=step.required and not pii_result.passed and pii_result.blocked,
                cached=False,
                duration_ms=0,
                details=pii_result.detail,
                metadata={},
            )

        if step.name == "dlp_scan":
            dlp_result = await asyncio.to_thread(
                qg.run_dlp_gate_sync,
                self._config,
                run_dir,
                changed_files if self._changed_files_resolved else None,
            )
            dlp_status: GateStatus = "pass" if dlp_result.passed else "fail"
            return GateResult(
                name="dlp_scan",
                status=dlp_status,
                required=step.required,
                blocked=step.required and not dlp_result.passed and dlp_result.blocked,
                cached=False,
                duration_ms=0,
                details=dlp_result.detail,
                metadata={},
            )

        if step.name == "mutation_testing":
            ok, detail, score = await asyncio.to_thread(qg.run_mutation_gate_sync, self._config, run_dir)
            return GateResult(
                name="mutation_testing",
                status="pass" if ok else "fail",
                required=step.required,
                blocked=step.required and not ok,
                cached=False,
                duration_ms=0,
                details=detail,
                metadata={"mutation_score": score} if score is not None else {},
            )

        if step.name == "agent_test_mutation":
            ok, detail, score = await asyncio.to_thread(
                qg.run_agent_test_mutation_gate_sync, self._config, task, run_dir
            )
            return GateResult(
                name="agent_test_mutation",
                status="pass" if ok else "fail",
                required=step.required,
                blocked=step.required and not ok,
                cached=False,
                duration_ms=0,
                details=detail,
                metadata={"mutation_score": score} if score is not None else {},
            )

        if step.name == "intent_verification":
            verdict, blocked = await asyncio.to_thread(
                qg.run_intent_gate_sync,
                task,
                run_dir,
                self._config.intent_verification,
            )
            return GateResult(
                name="intent_verification",
                status="fail" if blocked else "pass",
                required=step.required,
                blocked=step.required and blocked,
                cached=False,
                duration_ms=0,
                details=f"Intent verdict: {verdict.verdict} — {verdict.reason}",
                metadata={"verdict": verdict.verdict, "model": verdict.model},
            )

        if step.name == "complexity_check":
            return await asyncio.to_thread(self._run_complexity_gate_sync, step, run_dir, changed_files)

        if step.name == "dead_code":
            return await asyncio.to_thread(self._run_dead_code_gate_sync, step, run_dir, changed_files)

        if step.name == "comment_quality":
            return await asyncio.to_thread(self._run_comment_quality_gate_sync, step, run_dir, changed_files)

        if step.name == "import_cycle":
            return await asyncio.to_thread(self._run_import_cycle_gate_sync, step, run_dir, changed_files)

        if step.name == "coverage_delta":
            return await asyncio.to_thread(self._run_coverage_delta_gate_sync, step, run_dir, changed_files)

        if step.name == "merge_conflict":
            return await asyncio.to_thread(self._run_merge_conflict_gate_sync, step, run_dir, changed_files)

        if step.name == "dep_audit":
            command = step.command_override or self._config.dep_audit_command
            return await self._run_command_gate(
                step,
                command,
                run_dir,
                self._config.timeout_s,
                pass_detail="no vulnerable dependencies found",
            )

        if step.name == "benchmark":
            return await asyncio.to_thread(self._run_benchmark_gate_sync, step, run_dir)

        if step.name == "migration_reversibility":
            return await asyncio.to_thread(self._run_migration_reversibility_gate_sync, step, run_dir)

        if step.name == "large_file":
            return await asyncio.to_thread(self._run_large_file_gate_sync, step, run_dir, changed_files)

        if step.name == "integration_test_gen":
            return await self._run_integration_test_gen_gate(step, task, run_dir)

        if step.name == "review_rubric":
            return await self._run_review_rubric_gate(step, task, run_dir)

        plugin = self._plugin_registry().get(step.name)
        if plugin is not None:
            try:
                plugin_result = await asyncio.to_thread(
                    plugin.run,
                    changed_files,
                    run_dir,
                    task.title,
                    task.description,
                )
            except Exception as exc:
                return GateResult(
                    name=step.name,
                    status="fail",
                    required=step.required,
                    blocked=step.required,
                    cached=False,
                    duration_ms=0,
                    details=f"Gate plugin {step.name!r} failed: {exc}",
                    metadata={},
                )
            blocked = plugin_result.blocked or (step.required and plugin_result.status == "fail")
            return GateResult(
                name=step.name,
                status=plugin_result.status,
                required=step.required,
                blocked=blocked,
                cached=False,
                duration_ms=0,
                details=plugin_result.details,
                metadata=dict(plugin_result.metadata),
            )

        raise ValueError(f"Unsupported gate name: {step.name!r}")

    async def _run_command_gate(
        self,
        step: GatePipelineStep,
        command: str,
        run_dir: Path,
        timeout_s: int,
        *,
        pass_detail: str | None = None,
    ) -> GateResult:
        from bernstein.core import quality_gates as qg

        ok, detail = await asyncio.to_thread(qg.run_command_sync, command, run_dir, timeout_s)
        status: GateStatus
        blocked = False
        normalized_detail = detail
        if detail.startswith(_TIMED_OUT_PREFIX):
            status = "timeout"
            blocked = step.required
        elif ok:
            status = "pass"
            normalized_detail = pass_detail or detail
        else:
            status = "fail"
            blocked = step.required
        return GateResult(
            name=step.name,
            status=status,
            required=step.required,
            blocked=blocked,
            cached=False,
            duration_ms=0,
            details=normalized_detail,
            metadata={"command": command},
        )

    async def _run_tests_gate(
        self,
        step: GatePipelineStep,
        task: Task,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the tests gate, optionally recording flaky-test telemetry."""
        from bernstein.core import quality_gates as qg
        from bernstein.core.quality.flaky_detector import FlakyDetector, parse_pytest_output

        command = self._tests_command(step, run_dir, changed_files)
        if command is None:
            return self._skipped(step, "No impacted tests detected.")

        ok, detail = await asyncio.to_thread(qg.run_command_sync, command, run_dir, self._config.timeout_s)
        metadata: dict[str, Any] = {"command": command}
        if detail.startswith(_TIMED_OUT_PREFIX):
            return GateResult(
                name=step.name,
                status="timeout",
                required=step.required,
                blocked=step.required,
                cached=False,
                duration_ms=0,
                details=detail,
                metadata=metadata,
            )

        status: GateStatus = "pass" if ok else "fail"
        blocked = step.required and not ok
        details = "all tests passing" if ok else detail

        if self._config.flaky_detection:
            detector = FlakyDetector(
                self._workdir,
                min_runs=self._config.flaky_min_runs,
                flaky_threshold=self._config.flaky_threshold,
            )
            runs = parse_pytest_output(detail, run_id=task.id)
            if runs:
                await asyncio.to_thread(detector.record_run, runs)
                flaky_result = await asyncio.to_thread(detector.analyze)
                if flaky_result.newly_detected:
                    metadata["new_flaky_tests"] = flaky_result.newly_detected
                    if ok:
                        status = "warn"
                        blocked = False
                        details = (
                            f"all tests passing; newly detected flaky tests: {', '.join(flaky_result.newly_detected)}"
                        )

        return GateResult(
            name=step.name,
            status=status,
            required=step.required,
            blocked=blocked,
            cached=False,
            duration_ms=0,
            details=details,
            metadata=metadata,
        )

    def _run_complexity_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the complexity delta gate."""
        python_files = self._python_files(changed_files)
        command = self._complexity_gate_command(step, python_files)
        if command is None:
            return self._skipped(step, _NO_PYTHON_FILES)

        current_score, detail = self._measure_complexity_sync(command, run_dir)
        if current_score is None:
            return self._command_failure_result(step, detail, command)

        baseline_score, baseline_detail = self._measure_complexity_base_sync(command)
        if baseline_score is None:
            return GateResult(
                name=step.name,
                status="warn",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=f"Complexity average: {current_score:.2f}; baseline unavailable ({baseline_detail})",
                metadata={"command": command, "average_complexity": current_score},
            )

        delta_ratio = 0.0 if baseline_score == 0 else (current_score - baseline_score) / baseline_score
        passed = delta_ratio <= self._config.complexity_threshold
        detail_text = (
            f"Complexity average: {baseline_score:.2f} -> {current_score:.2f} "
            f"(delta {delta_ratio:+.1%}, threshold {self._config.complexity_threshold:.1%})"
        )
        return GateResult(
            name=step.name,
            status="pass" if passed else "fail",
            required=step.required,
            blocked=step.required and not passed,
            cached=False,
            duration_ms=0,
            details=detail_text,
            metadata={"command": command, "average_complexity": current_score, "baseline_complexity": baseline_score},
        )

    def _run_dead_code_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the dead-code gate.

        Runs vulture (or a custom command) on changed Python files, then
        augments with AST-based cross-codebase caller analysis via
        :mod:`bernstein.core.dead_code_detector`.
        """
        from bernstein.core import dead_code_detector
        from bernstein.core import quality_gates as qg

        python_files = self._python_files(changed_files)
        if not python_files:
            return self._skipped(step, _NO_PYTHON_FILES)

        # --- vulture pass ---
        command = self._dead_code_command(step, python_files)
        ok, vulture_detail = qg.run_command_sync(command, run_dir, self._config.timeout_s)
        if vulture_detail.startswith(_TIMED_OUT_PREFIX):
            return GateResult(
                name=step.name,
                status="timeout",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=vulture_detail,
                metadata={"command": command},
            )

        # --- AST + cross-codebase caller analysis ---
        try:
            report = dead_code_detector.analyse(
                python_files,
                run_dir,
                check_unused_imports=self._config.dead_code_check_unused_imports,
                check_unreachable=self._config.dead_code_check_unreachable,
                check_lost_callers=self._config.dead_code_check_lost_callers,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("dead_code_detector.analyse failed: %s", exc)
            report = dead_code_detector.DeadCodeReport()

        # Combine vulture output and AST findings
        ast_details = ""
        if report.issues:
            ast_details = "\n".join(f"  [{i.kind}] {i.file}: {i.detail}" for i in report.issues)

        vulture_ok = ok and vulture_detail == "(no output)"
        ast_ok = report.passed

        if vulture_ok and ast_ok:
            return GateResult(
                name=step.name,
                status="pass",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=f"No dead code detected. {report.summary()}",
                metadata={"command": command, "ast_issues": 0},
            )

        detail_parts: list[str] = []
        if not vulture_ok and vulture_detail not in ("(no output)", ""):
            detail_parts.append(f"vulture:\n{vulture_detail}")
        if ast_details:
            detail_parts.append(f"AST analysis:\n{ast_details}")

        full_detail = "\n".join(detail_parts) or vulture_detail
        lost_caller_issues = [i for i in report.issues if i.kind == "lost_caller"]
        has_breaking = bool(lost_caller_issues) or (not ok and vulture_detail.startswith("Command error:") is not False)

        status: GateStatus = "fail" if (step.required or lost_caller_issues) else "warn"
        return GateResult(
            name=step.name,
            status=status,
            required=step.required,
            blocked=step.required or bool(lost_caller_issues),
            cached=False,
            duration_ms=0,
            details=full_detail,
            metadata={
                "command": command,
                "ast_issues": len(report.issues),
                "lost_callers": len(lost_caller_issues),
                "has_breaking": has_breaking,
            },
        )

    def _run_comment_quality_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the comment-quality gate on changed Python files.

        Checks docstring accuracy, completeness, redundancy, and style via
        :mod:`bernstein.core.comment_quality`.
        """
        from bernstein.core import comment_quality

        python_files = self._python_files(changed_files)
        if not python_files:
            return self._skipped(step, _NO_PYTHON_FILES)

        raw_style = self._config.comment_quality_docstyle
        valid_styles = ("google", "numpy", "rest", "auto")
        docstyle: DocstyleKind = raw_style if raw_style in valid_styles else "auto"  # type: ignore[assignment]

        try:
            report = comment_quality.analyse(
                python_files,
                run_dir,
                docstyle=docstyle,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("comment_quality.analyse failed: %s", exc)
            return GateResult(
                name=step.name,
                status="fail",
                required=step.required,
                blocked=step.required,
                cached=False,
                duration_ms=0,
                details=f"Comment quality gate error: {exc}",
                metadata={},
            )

        if report.passed and not report.issues:
            return GateResult(
                name=step.name,
                status="pass",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=report.summary(),
                metadata={
                    "checked_functions": report.checked_functions,
                    "issue_count": 0,
                },
            )

        issue_lines = "\n".join(f"  [{i.kind}] {i.file}:{i.line} {i.symbol}: {i.detail}" for i in report.issues)
        status: GateStatus = "fail" if not report.passed else "warn"
        return GateResult(
            name=step.name,
            status=status,
            required=step.required,
            blocked=step.required and not report.passed,
            cached=False,
            duration_ms=0,
            details=f"{report.summary()}\n{issue_lines}",
            metadata={
                "checked_functions": report.checked_functions,
                "issue_count": len(report.issues),
                "blocking_issues": sum(1 for i in report.issues if i.kind in ("inaccurate", "incomplete")),
            },
        )

    def _run_import_cycle_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the import-cycle gate with a built-in AST fallback."""
        command = self._optional_command("import_cycle", step.command_override)
        python_files = self._python_files(changed_files)
        if not python_files:
            return self._skipped(step, _NO_PYTHON_FILES)
        if command is not None:
            ok, detail = self._run_command_and_capture(command, run_dir)
            if ok:
                return GateResult(
                    name=step.name,
                    status="pass",
                    required=step.required,
                    blocked=False,
                    cached=False,
                    duration_ms=0,
                    details="no import cycles detected",
                    metadata={"command": command},
                )
            return self._command_failure_result(step, detail, command)

        has_cycles, detail = self._detect_import_cycles_builtin(python_files, run_dir)
        return GateResult(
            name=step.name,
            status="fail" if has_cycles else "pass",
            required=step.required,
            blocked=step.required and has_cycles,
            cached=False,
            duration_ms=0,
            details=detail,
            metadata={},
        )

    def _run_coverage_delta_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the coverage-delta gate."""
        from bernstein.core.quality.coverage_gate import CoverageGate

        python_files = self._python_files(changed_files)
        if not python_files:
            return self._skipped(step, _NO_PYTHON_FILES)

        command = self._optional_command("coverage_delta", step.command_override)
        if command is not None:
            ok, detail = self._run_command_and_capture(command, run_dir)
            if ok:
                return GateResult(
                    name=step.name,
                    status="pass",
                    required=step.required,
                    blocked=False,
                    cached=False,
                    duration_ms=0,
                    details=detail,
                    metadata={"command": command},
                )
            return self._command_failure_result(step, detail, command)

        try:
            evaluation = CoverageGate(self._workdir, run_dir, base_ref=self._base_ref).evaluate()
        except Exception as exc:
            return GateResult(
                name=step.name,
                status="fail",
                required=step.required,
                blocked=step.required,
                cached=False,
                duration_ms=0,
                details=str(exc),
                metadata={},
            )
        return GateResult(
            name=step.name,
            status="pass" if evaluation.passed else "fail",
            required=step.required,
            blocked=step.required and not evaluation.passed,
            cached=False,
            duration_ms=0,
            details=evaluation.detail,
            metadata={
                "baseline_pct": evaluation.baseline_pct,
                "current_pct": evaluation.current_pct,
                "delta_pct": evaluation.delta_pct,
            },
        )

    def _run_benchmark_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
    ) -> GateResult:
        """Run the benchmark regression gate."""
        from bernstein.core.quality.benchmark_gate import BenchmarkGate

        cfg = self._config.benchmark
        command = step.command_override or cfg.command
        try:
            evaluation = BenchmarkGate(
                self._workdir,
                run_dir,
                base_ref=self._base_ref,
                benchmark_command=command,
                threshold=cfg.threshold,
            ).evaluate()
        except Exception as exc:
            return GateResult(
                name=step.name,
                status="fail",
                required=step.required,
                blocked=step.required,
                cached=False,
                duration_ms=0,
                details=str(exc),
                metadata={},
            )
        regression_names = [r.name for r in evaluation.regressions]
        return GateResult(
            name=step.name,
            status="pass" if evaluation.passed else "fail",
            required=step.required,
            blocked=step.required and not evaluation.passed,
            cached=False,
            duration_ms=0,
            details=evaluation.detail,
            metadata={
                "threshold": cfg.threshold,
                "regressions": regression_names,
                "benchmark_count": len(evaluation.current_metrics),
            },
        )

    def _run_migration_reversibility_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
    ) -> GateResult:
        """Check that every DB migration has a corresponding down/rollback path.

        Supports Alembic (``downgrade()`` function must be non-trivial) and
        generic up/down SQL file pairs.  When no migration files are found the
        gate passes with a skip note so projects without migrations are not
        affected.
        """
        issues: list[str] = []
        migration_count = 0

        # --- Alembic migrations (versions/*.py) ---
        alembic_dirs: list[Path] = []
        for candidate in ("alembic/versions", "migrations/versions", "db/versions"):
            p = run_dir / candidate
            if p.is_dir():
                alembic_dirs.append(p)

        for versions_dir in alembic_dirs:
            for migration_file in sorted(versions_dir.glob("*.py")):
                if migration_file.name.startswith("_"):
                    continue
                migration_count += 1
                try:
                    source = migration_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                # A bare `pass` or empty body after def downgrade means no rollback.
                if "def downgrade" not in source:
                    issues.append(f"{migration_file.relative_to(run_dir)}: missing downgrade() function")
                elif _migration_downgrade_is_pass(source):
                    issues.append(
                        f"{migration_file.relative_to(run_dir)}: downgrade() is empty (pass-only) — no rollback defined"
                    )

        # --- Generic SQL up/down pairs ---
        sql_migration_dirs: list[Path] = []
        for candidate in ("migrations", "db/migrations", "sql/migrations", "database/migrations"):
            p = run_dir / candidate
            if p.is_dir():
                sql_migration_dirs.append(p)

        for mig_dir in sql_migration_dirs:
            up_files = {f.stem for f in mig_dir.glob("*_up.sql")} | {
                f.stem.replace(".up", "") for f in mig_dir.glob("*.up.sql")
            }
            down_files = {f.stem for f in mig_dir.glob("*_down.sql")} | {
                f.stem.replace(".down", "") for f in mig_dir.glob("*.down.sql")
            }
            for stem in sorted(up_files):
                migration_count += 1
                down_stem = stem.replace("_up", "_down")
                if down_stem not in down_files and stem not in down_files:
                    issues.append(f"{mig_dir.relative_to(run_dir)}/{stem}_up.sql: no matching down migration")

        if migration_count == 0:
            return self._skipped(step, "No migration files found — skipping reversibility check.")

        if not issues:
            return GateResult(
                name=step.name,
                status="pass",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=f"All {migration_count} migration(s) have rollback paths.",
                metadata={"migration_count": migration_count},
            )

        detail = f"{len(issues)} migration(s) missing rollback:\n" + "\n".join(f"  - {i}" for i in issues)
        return GateResult(
            name=step.name,
            status="fail",
            required=step.required,
            blocked=step.required,
            cached=False,
            duration_ms=0,
            details=detail,
            metadata={"migration_count": migration_count, "missing_rollback": len(issues)},
        )

    def _run_large_file_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Warn when an agent-created or modified file exceeds 500 lines.

        Large files are a heuristic signal that a module should be decomposed.
        The gate always runs as a warning (never blocks) by default.
        """
        threshold = self._config.large_file_threshold
        oversized: list[tuple[str, int]] = []

        for rel_path in changed_files:
            file_path = run_dir / rel_path
            if not file_path.is_file():
                continue
            try:
                line_count = sum(1 for _ in file_path.open(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if line_count > threshold:
                oversized.append((rel_path, line_count))

        if not oversized:
            return GateResult(
                name=step.name,
                status="pass",
                required=step.required,
                blocked=False,
                cached=False,
                duration_ms=0,
                details=f"No files exceed the {threshold}-line threshold.",
                metadata={"threshold": threshold},
            )

        lines = [f"  {path}: {count} lines (>{threshold})" for path, count in sorted(oversized)]
        detail = f"{len(oversized)} file(s) exceed {threshold} lines and should be decomposed:\n" + "\n".join(lines)
        # Always warn; never block — this is a heuristic, not a hard requirement.
        return GateResult(
            name=step.name,
            status="warn",
            required=step.required,
            blocked=False,
            cached=False,
            duration_ms=0,
            details=detail,
            metadata={"threshold": threshold, "oversized_files": len(oversized)},
        )

    def _run_auto_format_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Auto-format changed files in place before lint runs.

        Runs language-appropriate formatters on changed files only (not the
        whole repo).  The gate always passes — it fixes rather than blocks.
        Any files reformatted are reported in the gate details so that the
        commit/push step can stage the changes.

        Supported languages:
        - Python: ``ruff format`` (configured via ``auto_format_python_command``)
        - JS/TS:  ``prettier --write`` (configured via ``auto_format_js_command``,
          only when ``prettier`` is on PATH)
        - Rust:   ``rustfmt`` (configured via ``auto_format_rust_command``,
          only when ``rustfmt`` is on PATH)
        """
        import shutil

        if not changed_files:
            return self._skipped(step, "No changed files to format.")

        formatted: list[str] = []
        skipped_langs: list[str] = []

        # --- Python: ruff format ---
        py_files = [f for f in changed_files if f.endswith(".py")]
        if py_files:
            py_cmd_base = self._config.auto_format_python_command
            py_exe = shlex.split(py_cmd_base)[0] if py_cmd_base else "ruff"
            # ruff is invoked via "uv run ruff" in this project, but we honour
            # whatever command is configured.  Resolve the first token via PATH;
            # if it's "uv" we always try (uv is present in the project env).
            if py_exe == "uv" or shutil.which(py_exe) is not None:
                py_result = self._run_formatter_sync(py_cmd_base, py_files, run_dir, lang="Python")
                if py_result:
                    formatted.append(py_result)
            else:
                skipped_langs.append(f"Python ({py_exe!r} not found)")

        # --- JS/TS: prettier ---
        js_exts = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
        js_files = [f for f in changed_files if Path(f).suffix in js_exts]
        if js_files:
            js_cmd_base = self._config.auto_format_js_command
            js_exe = shlex.split(js_cmd_base)[0] if js_cmd_base else "prettier"
            if shutil.which(js_exe) is not None:
                js_result = self._run_formatter_sync(js_cmd_base, js_files, run_dir, lang="JS/TS")
                if js_result:
                    formatted.append(js_result)
            else:
                skipped_langs.append(f"JS/TS ({js_exe!r} not found)")

        # --- Rust: rustfmt ---
        rs_files = [f for f in changed_files if f.endswith(".rs")]
        if rs_files:
            rs_cmd_base = self._config.auto_format_rust_command
            rs_exe = shlex.split(rs_cmd_base)[0] if rs_cmd_base else "rustfmt"
            if shutil.which(rs_exe) is not None:
                rs_result = self._run_formatter_sync(rs_cmd_base, rs_files, run_dir, lang="Rust")
                if rs_result:
                    formatted.append(rs_result)
            else:
                skipped_langs.append(f"Rust ({rs_exe!r} not found)")

        if not py_files and not js_files and not rs_files:
            return self._skipped(step, "No formattable files changed (Python/JS/TS/Rust).")

        parts: list[str] = []
        if formatted:
            parts.append("; ".join(formatted))
        else:
            parts.append("all changed files already well-formatted")
        if skipped_langs:
            parts.append(f"skipped: {', '.join(skipped_langs)}")

        return GateResult(
            name=step.name,
            status="pass",
            required=step.required,
            blocked=False,
            cached=False,
            duration_ms=0,
            details=". ".join(parts),
            metadata={"formatted_langs": [r.split(":")[0] for r in formatted]},
        )

    def _run_formatter_sync(
        self,
        base_command: str,
        files: list[str],
        run_dir: Path,
        lang: str,
    ) -> str | None:
        """Run a formatter on the specified files; return a summary string or None."""
        cmd = shlex.split(base_command) + files
        try:
            proc = subprocess.run(
                cmd,
                cwd=run_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("auto_format: %s formatter error: %s", lang, exc)
            return None

        if proc.returncode not in (0, 1):
            # exit code 1 from ruff means "files were changed", which is fine;
            # anything else is unexpected but we still don't block.
            logger.warning(
                "auto_format: %s formatter exited %d: %s",
                lang,
                proc.returncode,
                (proc.stderr or proc.stdout)[:200],
            )
            return None

        # Count reformatted files by inspecting stdout (ruff reports "N file(s) reformatted")
        output = (proc.stdout or "").strip()
        reformatted = 0
        if "reformatted" in output:
            for token in output.split():
                try:
                    reformatted = int(token)
                    break
                except ValueError:
                    continue

        if reformatted:
            return f"{lang}: {reformatted} file(s) reformatted"
        return None

    async def _run_integration_test_gen_gate(
        self,
        step: GatePipelineStep,
        task: Task,
        run_dir: Path,
    ) -> GateResult:
        """Run the integration test generation gate."""
        from bernstein.core.quality.integration_test_gen import IntegTestGenConfig, generate_and_run

        cfg = IntegTestGenConfig(
            enabled=True,
            block_on_fail=step.required,
        )
        result = await generate_and_run(task, run_dir, cfg)
        metadata: dict[str, Any] = {}
        if result.test_path:
            metadata["test_path"] = result.test_path
        if result.errors:
            metadata["errors"] = result.errors[:3]
        return GateResult(
            name=step.name,
            status="pass" if result.passed else "fail",
            required=step.required,
            blocked=result.blocked,
            cached=False,
            duration_ms=0,
            details=result.detail,
            metadata=metadata,
        )

    async def _run_review_rubric_gate(
        self,
        step: GatePipelineStep,
        task: Task,
        run_dir: Path,
    ) -> GateResult:
        """Run the multi-dimensional code review rubric gate."""
        from bernstein.core.quality.review_rubric import ReviewRubricConfig, RubricHistoryWriter, score_diff

        cfg = ReviewRubricConfig(
            enabled=True,
            block_below_threshold=step.required,
        )
        result = await score_diff(task, run_dir, cfg)
        RubricHistoryWriter(run_dir).record(task.id, result)
        metadata: dict[str, Any] = {"composite": result.composite}
        for dim in result.dimensions:
            metadata[dim.name] = dim.score
        if result.errors:
            metadata["errors"] = result.errors[:3]
        return GateResult(
            name=step.name,
            status="pass" if result.passed else ("warn" if not step.required else "fail"),
            required=step.required,
            blocked=result.blocked,
            cached=False,
            duration_ms=0,
            details=result.detail,
            metadata=metadata,
        )

    def _run_merge_conflict_gate_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the merge-conflict prediction gate."""
        from bernstein.core.git_basic import is_git_repo, run_git
        from bernstein.core.merge_queue import detect_merge_conflicts

        if not changed_files:
            return self._skipped(step, "No changed files detected.")
        if not is_git_repo(run_dir):
            return self._skipped(step, "Not a git repository.")

        branch_result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], run_dir)
        if not branch_result.ok:
            return GateResult(
                name=step.name,
                status="fail",
                required=step.required,
                blocked=step.required,
                cached=False,
                duration_ms=0,
                details=branch_result.stderr.strip() or "Failed to resolve current branch",
                metadata={},
            )
        branch = branch_result.stdout.strip()
        if not branch or branch == "HEAD":
            return self._skipped(step, "Detached HEAD; merge conflict prediction skipped.")

        result = detect_merge_conflicts(branch, self._base_ref, run_dir)
        if result.has_conflicts:
            return GateResult(
                name=step.name,
                status="fail",
                required=step.required,
                blocked=step.required,
                cached=False,
                duration_ms=0,
                details=f"Conflicts predicted in: {', '.join(result.conflicting_files)}",
                metadata={"branch": branch, "base_ref": self._base_ref},
            )
        return GateResult(
            name=step.name,
            status="pass",
            required=step.required,
            blocked=False,
            cached=False,
            duration_ms=0,
            details="No merge conflicts predicted.",
            metadata={"branch": branch, "base_ref": self._base_ref},
        )

    def _run_command_and_capture(self, command: str, run_dir: Path) -> tuple[bool, str]:
        """Execute a command gate synchronously and capture its output."""
        from bernstein.core import quality_gates as qg

        return qg.run_command_sync(command, run_dir, self._config.timeout_s)

    def _command_failure_result(self, step: GatePipelineStep, detail: str, command: str) -> GateResult:
        """Translate a command failure into a gate result."""
        if detail.startswith(_TIMED_OUT_PREFIX):
            return GateResult(
                name=step.name,
                status="timeout",
                required=step.required,
                blocked=step.required,
                cached=False,
                duration_ms=0,
                details=detail,
                metadata={"command": command},
            )
        return GateResult(
            name=step.name,
            status="fail",
            required=step.required,
            blocked=step.required,
            cached=False,
            duration_ms=0,
            details=detail,
            metadata={"command": command},
        )

    def _complexity_gate_command(self, step: GatePipelineStep, python_files: list[str]) -> str | None:
        """Build the complexity gate command for changed Python files."""
        if not python_files:
            return None
        command = step.command_override or self._config.complexity_check_command
        if not command:
            return None
        return f"{command} {self._quote_paths(python_files)}"

    def _dead_code_command(self, step: GatePipelineStep, python_files: list[str]) -> str:
        """Build the dead-code command line."""
        command = step.command_override or self._config.dead_code_command
        return f"{command} {self._quote_paths(python_files)} --min-confidence {self._config.dead_code_min_confidence}"

    def _measure_complexity_sync(self, command: str, cwd: Path) -> tuple[float | None, str]:
        """Execute a complexity command and parse its average score."""
        ok, detail = self._run_command_and_capture(command, cwd)
        if not ok:
            return None, detail
        score = self._parse_complexity_average(detail)
        if score is None:
            return None, "Could not parse complexity output."
        return score, detail

    def _measure_complexity_base_sync(self, command: str) -> tuple[float | None, str]:
        """Measure complexity against the configured base ref in a temporary worktree."""
        temp_parent = self._workdir / ".sdd" / "tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="complexity-base-", dir=temp_parent) as temp_dir:
            temp_path = Path(temp_dir)
            add_proc = subprocess.run(
                ["git", "worktree", "add", "--detach", str(temp_path), self._base_ref],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if add_proc.returncode != 0:
                return None, add_proc.stderr.strip() or f"Failed to create baseline worktree for {self._base_ref}"
            try:
                return self._measure_complexity_sync(command, temp_path)
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(temp_path)],
                    cwd=self._workdir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

    def _parse_complexity_average(self, output: str) -> float | None:
        """Parse an average complexity score from command output."""
        try:
            raw: object = json.loads(output)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict):
            raw_map = cast("dict[str, object]", raw)
            for key in ("average_complexity", "average", "mean_complexity"):
                value = raw_map.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            complexities: list[float] = []
            for value in raw_map.values():
                if isinstance(value, list):
                    items = cast("list[object]", value)
                    for item in items:
                        if isinstance(item, dict):
                            item_map = cast("dict[str, object]", item)
                            complexity = item_map.get("complexity")
                            if isinstance(complexity, (int, float)):
                                complexities.append(float(complexity))
            if complexities:
                return sum(complexities) / len(complexities)
        stripped = output.strip()
        try:
            return float(stripped)
        except ValueError:
            return None

    def _detect_import_cycles_builtin(self, changed_files: list[str], run_dir: Path) -> tuple[bool, str]:
        """Detect import cycles with a simple AST-based resolver."""
        source_root = run_dir / "src"
        search_root = source_root if source_root.exists() else run_dir
        module_to_path: dict[str, Path] = {}
        for py_file in sorted(search_root.rglob("*.py")):
            if any(part.startswith(".") for part in py_file.relative_to(run_dir).parts):
                continue
            if "tests" in py_file.relative_to(run_dir).parts:
                continue
            module = _module_name_from_path(py_file, source_root if source_root.exists() else run_dir)
            if module:
                module_to_path[module] = py_file

        graph: dict[str, set[str]] = {module: set() for module in module_to_path}
        for module, path in module_to_path.items():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in module_to_path:
                            graph[module].add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    target = _resolve_import_from(module, node.level, node.module)
                    if target and target in module_to_path:
                        graph[module].add(target)

        changed_modules = {
            _module_name_from_path(run_dir / rel_path, source_root if source_root.exists() else run_dir)
            for rel_path in changed_files
            if rel_path.endswith(".py")
        }
        changed_modules.discard("")

        cycles: set[tuple[str, ...]] = set()
        visited: set[str] = set()
        stack: list[str] = []
        in_stack: set[str] = set()

        def visit(node: str) -> None:
            visited.add(node)
            stack.append(node)
            in_stack.add(node)
            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    visit(neighbor)
                elif neighbor in in_stack:
                    start = stack.index(neighbor)
                    cycle = tuple([*stack[start:], neighbor])
                    if changed_modules.intersection(cycle):
                        cycles.add(cycle)
            stack.pop()
            in_stack.discard(node)

        for module in graph:
            if module not in visited:
                visit(module)

        if not cycles:
            return False, "No import cycles detected."
        cycle_lines = [" -> ".join(cycle) for cycle in sorted(cycles)]
        return True, "Import cycles detected: " + "; ".join(cycle_lines)

    def _plugin_registry(self) -> GatePluginRegistry:
        """Return a cached quality-gate plugin registry."""
        from bernstein.core.quality.gate_plugins import GatePluginRegistry

        if self._gate_plugin_registry is None:
            registry = GatePluginRegistry(self._workdir, built_in_names=VALID_GATE_NAMES)
            registry.discover()
            self._gate_plugin_registry = registry
        return self._gate_plugin_registry

    def _resolve_pipeline(self) -> list[GatePipelineStep]:
        pipeline = self._config.pipeline if self._config.pipeline is not None else build_default_pipeline(self._config)
        normalized: list[GatePipelineStep] = []
        for step in pipeline:
            if step.name not in VALID_GATE_NAMES and self._plugin_registry().get(step.name) is None:
                raise ValueError(f"Unsupported gate name: {step.name!r}")
            normalized.append(
                GatePipelineStep(
                    name=step.name,
                    required=step.required,
                    condition=normalize_gate_condition(step.condition),
                    command_override=step.command_override,
                )
            )
        return normalized

    def _condition_matches(self, condition: str, changed_files: list[str]) -> bool:
        if not self._changed_files_resolved:
            return True
        if condition == "always":
            return True
        if condition == "any_changed":
            return bool(changed_files)
        if condition == "python_changed":
            return bool(self._python_files(changed_files))
        if condition == "tests_changed":
            return any(self._is_test_path(path) for path in changed_files)
        if condition == "deps_changed":
            return any(_is_dep_file(path) for path in changed_files)
        raise ValueError(f"Unsupported gate condition: {condition!r}")

    def _lint_command(self, step: GatePipelineStep, changed_files: list[str]) -> str | None:
        if step.command_override is not None:
            return step.command_override
        python_files = self._python_files(changed_files)
        if self._changed_files_resolved:
            if not python_files:
                return None
            return f"ruff check {self._quote_paths(python_files)}"
        return self._config.lint_command

    def _type_check_command(self, step: GatePipelineStep, changed_files: list[str]) -> str | None:
        if step.command_override is not None:
            return step.command_override
        python_files = self._python_files(changed_files)
        if self._changed_files_resolved:
            if not python_files:
                return None
            expanded = self._expand_type_check_files(python_files)
            return f"pyright {self._quote_paths(expanded)}"
        return self._config.type_check_command

    def _expand_type_check_files(self, python_files: list[str]) -> list[str]:
        """Expand changed Python files to include transitive importers.

        Uses the source dependency graph to find all modules that directly or
        transitively import the changed files.  This ensures that a signature
        change in one module triggers type-checking of all impacted callers.

        Falls back to the original changed-file list when the dependency index
        is unavailable or the graph is empty.

        Args:
            python_files: Relative paths of changed ``.py`` files.

        Returns:
            Sorted list of Python file paths covering changed files plus
            all discovered dependents.
        """
        from bernstein.core.quality.test_impact import TestImpactAnalyzer

        try:
            analyzer = TestImpactAnalyzer(self._workdir)
            expanded = analyzer.get_dependent_source_files(python_files)
            # Only keep .py files in the expanded set.
            return [f for f in expanded if f.endswith(".py")]
        except Exception:
            logger.debug("Dependency expansion for type-check failed; using changed files only", exc_info=True)
            return python_files

    def _tests_command(self, step: GatePipelineStep, run_dir: Path, changed_files: list[str]) -> str | None:
        from bernstein.core.quality.flaky_detector import FlakyDetector
        from bernstein.core.quality.test_impact import TestImpactAnalyzer

        if step.command_override is not None:
            command = step.command_override
        elif not self._changed_files_resolved:
            command = self._config.test_command
        else:
            analyzer = TestImpactAnalyzer(run_dir)
            analysis = analyzer.analyze(changed_files)
            if analysis.fallback_used:
                command = self._config.test_command
            elif analysis.affected_tests:
                command = f"uv run pytest {self._quote_paths(analysis.affected_tests)} -x -q"
            else:
                command = self._config.test_command if self._python_files(changed_files) else None
        if command is None:
            return None
        if self._config.flaky_detection:
            deselect = FlakyDetector(
                self._workdir,
                min_runs=self._config.flaky_min_runs,
                flaky_threshold=self._config.flaky_threshold,
            ).pytest_deselect_args()
            if deselect:
                command = f"{command} {deselect}"
        return command

    def _optional_command(self, gate_name: str, command_override: str | None) -> str | None:
        if command_override is not None:
            return command_override
        return {
            "security_scan": self._config.security_scan_command,
            "coverage_delta": self._config.coverage_delta_command,
            "complexity_check": self._config.complexity_check_command,
            "import_cycle": self._config.import_cycle_command,
        }[gate_name]

    def _python_files(self, changed_files: list[str]) -> list[str]:
        return [path for path in changed_files if path.endswith(".py")]

    def _quote_paths(self, paths: list[str]) -> str:
        return " ".join(shlex.quote(path) for path in paths)

    def _impacted_tests(self, run_dir: Path, changed_python_files: list[str]) -> list[str]:
        impacted: set[str] = set()
        for rel_path in changed_python_files:
            path = Path(rel_path)
            module_name = path.stem
            for pattern in (f"tests/**/test_{module_name}.py", f"tests/**/*{module_name}*test.py"):
                for candidate in sorted(run_dir.glob(pattern)):
                    if candidate.is_file():
                        impacted.add(candidate.relative_to(run_dir).as_posix())

            parent = (run_dir / rel_path).parent
            candidate_dirs = [
                parent / "tests",
                *(ancestor / "tests" for ancestor in parent.parents if ancestor != run_dir.parent),
            ]
            for candidate_dir in candidate_dirs:
                if not candidate_dir.exists() or not candidate_dir.is_dir():
                    continue
                try:
                    candidate_dir.relative_to(run_dir)
                except ValueError:
                    continue
                for test_file in sorted(candidate_dir.glob("test_*.py")):
                    if test_file.is_file():
                        impacted.add(test_file.relative_to(run_dir).as_posix())
        return sorted(impacted)

    def _resolve_changed_files_sync(self, task: Task, run_dir: Path) -> list[str] | None:
        owned = self._existing_relative_paths(run_dir, task.owned_files)
        if owned:
            return owned
        git_diff = self._git_diff_changed_files(run_dir)
        if git_diff is not None:
            return git_diff
        return None

    def _existing_relative_paths(self, run_dir: Path, candidates: list[str]) -> list[str]:
        existing: list[str] = []
        for rel in candidates:
            candidate = run_dir / rel
            if candidate.exists() and candidate.is_file():
                existing.append(Path(rel).as_posix())
        return sorted(set(existing))

    def _git_diff_changed_files(self, run_dir: Path) -> list[str] | None:
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-only", f"{self._base_ref}..HEAD"],
                cwd=run_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in (0, 1):
            return None
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return self._existing_relative_paths(run_dir, lines)

    def _make_cache_key_sync(
        self,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> str | None:
        if step.name not in VALID_GATE_NAMES:
            return None
        if step.name in {"coverage_delta", "complexity_check", "merge_conflict"}:
            return None
        if step.name == "tests" and self._config.flaky_detection:
            return None
        if not self._config.cache_enabled or not self._changed_files_resolved:
            return None
        hashed_files: list[dict[str, str]] = []
        for rel_path in sorted(changed_files):
            file_path = run_dir / rel_path
            if not file_path.exists() or not file_path.is_file():
                continue
            try:
                digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            except OSError:
                return None
            hashed_files.append({"path": rel_path, "sha256": digest})
        relevant_config = {
            "name": step.name,
            "required": step.required,
            "condition": step.condition,
            "command_override": step.command_override,
            "base_ref": self._base_ref,
            "timeout_s": self._config.timeout_s,
            "lint_command": self._config.lint_command if step.name == "lint" else None,
            "type_check_command": self._config.type_check_command if step.name == "type_check" else None,
            "test_command": self._config.test_command if step.name == "tests" else None,
            "flaky_detection": self._config.flaky_detection if step.name == "tests" else None,
            "flaky_min_runs": self._config.flaky_min_runs if step.name == "tests" else None,
            "flaky_threshold": self._config.flaky_threshold if step.name == "tests" else None,
            "pii_scan_paths": self._config.pii_scan_paths if step.name == "pii_scan" else None,
            "pii_ignore_paths": self._config.pii_ignore_paths if step.name == "pii_scan" else None,
            "pii_allowlist_prefixes": self._config.pii_allowlist_prefixes if step.name == "pii_scan" else None,
            "security_scan": self._config.security_scan if step.name == "security_scan" else None,
            "security_scan_command": self._config.security_scan_command if step.name == "security_scan" else None,
            "coverage_delta": self._config.coverage_delta if step.name == "coverage_delta" else None,
            "coverage_delta_command": self._config.coverage_delta_command if step.name == "coverage_delta" else None,
            "complexity_check": self._config.complexity_check if step.name == "complexity_check" else None,
            "complexity_threshold": self._config.complexity_threshold if step.name == "complexity_check" else None,
            "complexity_check_command": (
                self._config.complexity_check_command if step.name == "complexity_check" else None
            ),
            "dead_code_check": self._config.dead_code_check if step.name == "dead_code" else None,
            "dead_code_command": self._config.dead_code_command if step.name == "dead_code" else None,
            "dead_code_min_confidence": self._config.dead_code_min_confidence if step.name == "dead_code" else None,
            "dead_code_check_lost_callers": (
                self._config.dead_code_check_lost_callers if step.name == "dead_code" else None
            ),
            "dead_code_check_unused_imports": (
                self._config.dead_code_check_unused_imports if step.name == "dead_code" else None
            ),
            "dead_code_check_unreachable": (
                self._config.dead_code_check_unreachable if step.name == "dead_code" else None
            ),
            "comment_quality_check": (self._config.comment_quality_check if step.name == "comment_quality" else None),
            "comment_quality_docstyle": (
                self._config.comment_quality_docstyle if step.name == "comment_quality" else None
            ),
            "import_cycle_check": self._config.import_cycle_check if step.name == "import_cycle" else None,
            "import_cycle_command": self._config.import_cycle_command if step.name == "import_cycle" else None,
            "merge_conflict_check": self._config.merge_conflict_check if step.name == "merge_conflict" else None,
        }
        payload = {"step": relevant_config, "files": hashed_files}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _get_cached_result_sync(self, cache_key: str) -> GateResult | None:
        self._ensure_cache_loaded_sync()
        raw = self._cache_entries.get(cache_key)
        if raw is None:
            return None
        status = raw.get("status")
        if status not in {"pass", "fail", "warn", "skipped"}:
            return None
        return GateResult(
            name=str(raw["name"]),
            status=status,
            required=bool(raw["required"]),
            blocked=bool(raw["blocked"]),
            cached=False,
            duration_ms=int(raw["duration_ms"]),
            details=str(raw["details"]),
            metadata=dict(raw.get("metadata", {})),
        )

    def _store_cached_result_sync(self, cache_key: str, result: GateResult) -> None:
        self._ensure_cache_loaded_sync()
        with self._cache_lock:
            self._cache_entries[cache_key] = {
                "name": result.name,
                "status": result.status,
                "required": result.required,
                "blocked": result.blocked,
                "duration_ms": result.duration_ms,
                "details": result.details,
                "metadata": result.metadata,
            }
            cache_path = self._workdir / ".sdd" / "caching" / "gate_cache.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(self._cache_entries, indent=2, sort_keys=True), encoding="utf-8")

    def _ensure_cache_loaded_sync(self) -> None:
        if self._cache_loaded:
            return
        with self._cache_lock:
            if self._cache_loaded:
                return
            cache_path = self._workdir / ".sdd" / "caching" / "gate_cache.json"
            if cache_path.exists():
                try:
                    raw_data: object = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(raw_data, dict):
                        raw_entries = cast("dict[object, object]", raw_data)
                        entries: dict[str, dict[str, Any]] = {}
                        for key, value in raw_entries.items():
                            if isinstance(value, dict):
                                raw_value = cast("dict[object, Any]", value)
                                entries[str(key)] = {
                                    str(item_key): item_value for item_key, item_value in raw_value.items()
                                }
                        self._cache_entries = entries
                except (OSError, json.JSONDecodeError):
                    logger.warning("Failed to load gate cache from %s", cache_path)
            self._cache_loaded = True

    def _persist_report_sync(self, report: GateReport) -> None:
        report_dir = self._workdir / ".sdd" / "runtime" / "gates"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{report.task_id}.json"
        report_path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8")

    def _skipped(self, step: GatePipelineStep, details: str) -> GateResult:
        return GateResult(
            name=step.name,
            status="skipped",
            required=step.required,
            blocked=False,
            cached=False,
            duration_ms=0,
            details=details,
            metadata={},
        )

    def _run_test_expansion_gate_sync(
        self,
        step: GatePipelineStep,
        task: Any,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Check whether agent-modified source files have corresponding test coverage.

        Identifies source files without matching test files and reports them
        as uncovered. This is a non-blocking advisory gate.
        """
        source_files = [f for f in changed_files if not self._is_test_path(f) and f.endswith(".py")]
        uncovered: list[str] = []
        for src in source_files:
            # Derive expected test path: src/foo/bar.py → tests/unit/test_bar.py
            src_path = Path(src)
            expected_test = Path("tests") / "unit" / f"test_{src_path.stem}.py"
            if not (run_dir / expected_test).exists() and expected_test.as_posix() not in changed_files:
                uncovered.append(src)

        if not source_files:
            return self._skipped(step, "No Python source files changed.")

        # Always pass — this is an advisory gate. Record uncovered files in metadata
        # and write needs_coverage.json for downstream consumers.
        if uncovered:
            import json as _json

            coverage_file = run_dir / ".sdd" / "runtime" / "needs_coverage.json"
            coverage_file.parent.mkdir(parents=True, exist_ok=True)
            entries = [{"source_file": f, "expected_test": f"tests/unit/test_{Path(f).stem}.py"} for f in uncovered]
            coverage_file.write_text(_json.dumps(entries, indent=2), encoding="utf-8")

        details = (
            f"{len(uncovered)} source file(s) without test coverage: {', '.join(uncovered[:5])}"
            if uncovered
            else f"All {len(source_files)} source file(s) have corresponding tests."
        )
        return GateResult(
            name=step.name,
            status="pass",
            required=step.required,
            blocked=False,
            cached=False,
            duration_ms=0,
            details=details,
            metadata={"uncovered_files": uncovered} if uncovered else {},
        )

    def _is_test_path(self, path: str) -> bool:
        candidate = Path(path)
        return (
            candidate.parts[:1] == ("tests",)
            or candidate.name.startswith("test_")
            or candidate.name.endswith("_test.py")
        )
