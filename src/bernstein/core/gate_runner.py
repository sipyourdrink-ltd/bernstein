"""Async quality gate runner with incremental execution and cached reports.

The ``GateRunner`` class orchestrates a quality-gate pipeline for one task.
Implementation details are split across three focused sub-modules:

- :mod:`bernstein.core.gate_pipeline` -- pipeline data classes, constants,
  and the ``build_default_pipeline`` factory.
- :mod:`bernstein.core.gate_commands` -- individual gate implementations
  (``GateRunnerCommandsMixin``).
- :mod:`bernstein.core.gate_cache` -- pipeline resolution, file handling,
  caching, and report persistence (``GateRunnerCacheMixin``).

All public names are re-exported from this module so existing call-sites
remain unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from bernstein.core.gate_cache import GateRunnerCacheMixin
from bernstein.core.gate_commands import GateRunnerCommandsMixin
from bernstein.core.gate_commands import _migration_downgrade_is_pass as _migration_downgrade_is_pass
from bernstein.core.gate_commands import _module_name_from_path as _module_name_from_path
from bernstein.core.gate_commands import _resolve_import_from as _resolve_import_from
from bernstein.core.gate_pipeline import LEGACY_PYTHON_CONDITION as LEGACY_PYTHON_CONDITION
from bernstein.core.gate_pipeline import NO_PYTHON_FILES, TIMED_OUT_PREFIX
from bernstein.core.gate_pipeline import VALID_GATE_CONDITIONS as VALID_GATE_CONDITIONS
from bernstein.core.gate_pipeline import VALID_GATE_NAMES as VALID_GATE_NAMES
from bernstein.core.gate_pipeline import GatePipelineStep as GatePipelineStep
from bernstein.core.gate_pipeline import GateReport as GateReport
from bernstein.core.gate_pipeline import GateResult as GateResult
from bernstein.core.gate_pipeline import GateStatus as GateStatus
from bernstein.core.gate_pipeline import _empty_metadata as _empty_metadata
from bernstein.core.gate_pipeline import build_default_pipeline as build_default_pipeline
from bernstein.core.gate_pipeline import is_dep_file as _is_dep_file  # noqa: F401  # backward compat
from bernstein.core.gate_pipeline import normalize_gate_condition as normalize_gate_condition
from bernstein.core.telemetry import start_span

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from bernstein.core.gate_plugins import GatePluginRegistry
    from bernstein.core.models import Task
    from bernstein.core.quality_gates import QualityGatesConfig

logger = logging.getLogger(__name__)


class GateRunner(
    GateRunnerCommandsMixin,
    GateRunnerCacheMixin,
):
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
        self._changed_files_resolved = True
        self._gate_plugin_registry: GatePluginRegistry | None = None

        # Initialise cache mixin state.
        GateRunnerCacheMixin.__init_cache__(self)

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
                return self._skipped(step, NO_PYTHON_FILES)
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
                return self._skipped(step, NO_PYTHON_FILES)
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
        if detail.startswith(TIMED_OUT_PREFIX):
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
