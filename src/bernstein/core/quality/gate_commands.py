"""Individual gate implementations for the quality-gate runner."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.quality.gate_pipeline import (
    NO_PYTHON_FILES,
    TIMED_OUT_PREFIX,
    GateResult,
    GateStatus,
)

if TYPE_CHECKING:
    from bernstein.core.models import Task
    from bernstein.core.quality.auto_formatter import FormatterConfig
    from bernstein.core.quality.comment_quality import DocstyleKind
    from bernstein.core.quality.gate_pipeline import GatePipelineStep

logger = logging.getLogger(__name__)


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


def _build_registry_from_config(config: Any) -> tuple[FormatterConfig, ...]:
    """Build a formatter registry from gate config, honouring per-language overrides.

    Falls back to the default registry entries for languages without config
    overrides.
    """
    from bernstein.core.quality.auto_formatter import (  # pyright: ignore[reportPrivateUsage]
        _DEFAULT_REGISTRY,
        FormatterConfig,
    )

    overrides: dict[str, tuple[str, ...]] = {}
    if config.auto_format_python_command:
        overrides["Python"] = tuple(shlex.split(config.auto_format_python_command))
    if config.auto_format_js_command:
        overrides["JS/TS"] = tuple(shlex.split(config.auto_format_js_command))
    if config.auto_format_rust_command:
        overrides["Rust"] = tuple(shlex.split(config.auto_format_rust_command))

    if not overrides:
        return _DEFAULT_REGISTRY

    result: list[FormatterConfig] = []
    for cfg in _DEFAULT_REGISTRY:
        if cfg.language in overrides:
            result.append(
                FormatterConfig(
                    language=cfg.language,
                    command=overrides[cfg.language],
                    extensions=cfg.extensions,
                    timeout_s=cfg.timeout_s,
                )
            )
        else:
            result.append(cfg)
    return tuple(result)


class GateRunnerCommandsMixin:
    """Mixin providing all individual gate implementations for GateRunner.

    This mixin is combined with :class:`~bernstein.core.gate_pipeline.GateRunner`
    at runtime.  Methods here reference ``self._config``, ``self._workdir``,
    ``self._base_ref``, ``self._changed_files_resolved``, and helper methods
    from the cache mixin via the GateRunner instance.
    """

    # -- mixin initialiser (called from GateRunner.__init__) -----------------

    @staticmethod
    def __init_commands__(instance: object) -> None:
        """Placeholder for future per-instance state needed by command gates."""

    # -- tests gate ----------------------------------------------------------

    async def _run_tests_gate(
        self: Any,
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
        if detail.startswith(TIMED_OUT_PREFIX):
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

    # -- complexity gate -----------------------------------------------------

    def _run_complexity_gate_sync(
        self: Any,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the complexity delta gate."""
        python_files = self._python_files(changed_files)
        command = self._complexity_gate_command(step, python_files)
        if command is None:
            return self._skipped(step, NO_PYTHON_FILES)

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

    # -- dead code gate ------------------------------------------------------

    def _run_dead_code_gate_sync(
        self: Any,
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
            return self._skipped(step, NO_PYTHON_FILES)

        command = self._dead_code_command(step, python_files)
        ok, vulture_detail = qg.run_command_sync(command, run_dir, self._config.timeout_s)
        if vulture_detail.startswith(TIMED_OUT_PREFIX):
            return GateResult(
                name=step.name, status="timeout", required=step.required,
                blocked=False, cached=False, duration_ms=0,
                details=vulture_detail, metadata={"command": command},
            )

        report = self._run_dead_code_ast_analysis(dead_code_detector, python_files, run_dir)
        return self._build_dead_code_result(step, command, ok, vulture_detail, report)

    def _run_dead_code_ast_analysis(self: Any, dead_code_detector: Any, python_files: list[str], run_dir: Path) -> Any:
        """Run AST-based dead code analysis, returning the report."""
        from bernstein.core import dead_code_detector as dcd

        try:
            return dcd.analyse(
                python_files, run_dir,
                check_unused_imports=self._config.dead_code_check_unused_imports,
                check_unreachable=self._config.dead_code_check_unreachable,
                check_lost_callers=self._config.dead_code_check_lost_callers,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("dead_code_detector.analyse failed: %s", exc)
            return dcd.DeadCodeReport()

    @staticmethod
    def _build_dead_code_result(
        step: GatePipelineStep, command: str, ok: bool, vulture_detail: str, report: Any,
    ) -> GateResult:
        """Build a GateResult from combined vulture + AST dead-code analysis."""
        ast_details = "\n".join(f"  [{i.kind}] {i.file}: {i.detail}" for i in report.issues) if report.issues else ""
        vulture_ok = ok and vulture_detail == "(no output)"

        if vulture_ok and report.passed:
            return GateResult(
                name=step.name, status="pass", required=step.required,
                blocked=False, cached=False, duration_ms=0,
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
            name=step.name, status=status, required=step.required,
            blocked=step.required or bool(lost_caller_issues),
            cached=False, duration_ms=0, details=full_detail,
            metadata={"command": command, "ast_issues": len(report.issues),
                       "lost_callers": len(lost_caller_issues), "has_breaking": has_breaking},
        )

    # -- comment quality gate ------------------------------------------------

    def _run_comment_quality_gate_sync(
        self: Any,
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
            return self._skipped(step, NO_PYTHON_FILES)

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

    # -- import cycle gate ---------------------------------------------------

    def _run_import_cycle_gate_sync(
        self: Any,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the import-cycle gate with a built-in AST fallback."""
        command = self._optional_command("import_cycle", step.command_override)
        python_files = self._python_files(changed_files)
        if not python_files:
            return self._skipped(step, NO_PYTHON_FILES)
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

    # -- coverage delta gate -------------------------------------------------

    def _run_coverage_delta_gate_sync(
        self: Any,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Run the coverage-delta gate."""
        from bernstein.core.quality.coverage_gate import CoverageGate

        python_files = self._python_files(changed_files)
        if not python_files:
            return self._skipped(step, NO_PYTHON_FILES)

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

    # -- benchmark gate ------------------------------------------------------

    def _run_benchmark_gate_sync(
        self: Any,
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

    # -- migration reversibility gate ----------------------------------------

    @staticmethod
    def _check_alembic_migrations(run_dir: Path) -> tuple[int, list[str]]:
        """Check Alembic migration files for missing downgrade. Returns (count, issues)."""
        count = 0
        issues: list[str] = []
        for candidate in ("alembic/versions", "migrations/versions", "db/versions"):
            versions_dir = run_dir / candidate
            if not versions_dir.is_dir():
                continue
            for migration_file in sorted(versions_dir.glob("*.py")):
                if migration_file.name.startswith("_"):
                    continue
                count += 1
                try:
                    source = migration_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                rel = migration_file.relative_to(run_dir)
                if "def downgrade" not in source:
                    issues.append(f"{rel}: missing downgrade() function")
                elif _migration_downgrade_is_pass(source):
                    issues.append(f"{rel}: downgrade() is empty (pass-only) — no rollback defined")
        return count, issues

    @staticmethod
    def _check_sql_migrations(run_dir: Path) -> tuple[int, list[str]]:
        """Check SQL up/down migration pairs. Returns (count, issues)."""
        count = 0
        issues: list[str] = []
        for candidate in ("migrations", "db/migrations", "sql/migrations", "database/migrations"):
            mig_dir = run_dir / candidate
            if not mig_dir.is_dir():
                continue
            up_files = {f.stem for f in mig_dir.glob("*_up.sql")} | {
                f.stem.replace(".up", "") for f in mig_dir.glob("*.up.sql")
            }
            down_files = {f.stem for f in mig_dir.glob("*_down.sql")} | {
                f.stem.replace(".down", "") for f in mig_dir.glob("*.down.sql")
            }
            for stem in sorted(up_files):
                count += 1
                down_stem = stem.replace("_up", "_down")
                if down_stem not in down_files and stem not in down_files:
                    issues.append(f"{mig_dir.relative_to(run_dir)}/{stem}_up.sql: no matching down migration")
        return count, issues

    def _run_migration_reversibility_gate_sync(
        self: Any,
        step: GatePipelineStep,
        run_dir: Path,
    ) -> GateResult:
        """Check that every DB migration has a corresponding down/rollback path."""
        alembic_count, alembic_issues = self._check_alembic_migrations(run_dir)
        sql_count, sql_issues = self._check_sql_migrations(run_dir)
        migration_count = alembic_count + sql_count
        issues = alembic_issues + sql_issues

        if migration_count == 0:
            return self._skipped(step, "No migration files found — skipping reversibility check.")

        if not issues:
            return GateResult(
                name=step.name, status="pass", required=step.required,
                blocked=False, cached=False, duration_ms=0,
                details=f"All {migration_count} migration(s) have rollback paths.",
                metadata={"migration_count": migration_count},
            )

        detail = f"{len(issues)} migration(s) missing rollback:\n" + "\n".join(f"  - {i}" for i in issues)
        return GateResult(
            name=step.name, status="fail", required=step.required,
            blocked=step.required, cached=False, duration_ms=0,
            details=detail,
            metadata={"migration_count": migration_count, "missing_rollback": len(issues)},
        )

    # -- large file gate -----------------------------------------------------

    def _run_large_file_gate_sync(
        self: Any,
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

    # -- auto format gate ----------------------------------------------------

    def _run_auto_format_gate_sync(
        self: Any,
        step: GatePipelineStep,
        run_dir: Path,
        changed_files: list[str],
    ) -> GateResult:
        """Auto-format changed files in place before lint runs.

        Delegates to :func:`~bernstein.core.quality.auto_formatter.auto_format_changed_files`
        which supports Python (ruff), JS/TS (prettier), Rust (rustfmt), and
        Go (gofmt) via a pluggable registry.  Custom per-language commands from
        the gate config are injected as overrides.

        The gate always passes — it fixes rather than blocks.  Any files
        reformatted are reported in the gate details so that the commit/push
        step can stage the changes.
        """
        from bernstein.core.quality.auto_formatter import auto_format_changed_files

        if not changed_files:
            return self._skipped(step, "No changed files to format.")

        # Build a custom registry that honours per-language command overrides
        # from the gate config while keeping the full default set.
        registry = _build_registry_from_config(self._config)

        results = auto_format_changed_files(
            workdir=run_dir,
            changed_files=changed_files,
            registry=registry,
        )

        if not results:
            return self._skipped(step, "No formattable files changed.")

        formatted: list[str] = []
        skipped_langs: list[str] = []
        total_duration_ms = 0

        for r in results:
            total_duration_ms += int(r.duration_s * 1000)
            if r.error:
                skipped_langs.append(f"{r.formatter_used} ({r.error})")
            elif r.files_formatted > 0:
                formatted.append(f"{r.formatter_used}: {r.files_formatted} file(s) reformatted")

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
            duration_ms=total_duration_ms,
            details=". ".join(parts),
            metadata={"formatted_langs": [r.split(":")[0] for r in formatted]},
        )

    # -- integration test gen gate -------------------------------------------

    async def _run_integration_test_gen_gate(
        self: Any,
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

    # -- review rubric gate --------------------------------------------------

    async def _run_review_rubric_gate(
        self: Any,
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
        if result.passed:
            gate_status = "pass"
        elif not step.required:
            gate_status = "warn"
        else:
            gate_status = "fail"
        return GateResult(
            name=step.name,
            status=gate_status,
            required=step.required,
            blocked=result.blocked,
            cached=False,
            duration_ms=0,
            details=result.detail,
            metadata=metadata,
        )

    # -- merge conflict gate -------------------------------------------------

    def _run_merge_conflict_gate_sync(
        self: Any,
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

    # -- test expansion gate -------------------------------------------------

    def _run_test_expansion_gate_sync(
        self: Any,
        step: GatePipelineStep,
        _task: Any,
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
            # Derive expected test path: src/foo/bar.py -> tests/unit/test_bar.py
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

    # -- helper: complexity command ------------------------------------------

    def _complexity_gate_command(self: Any, step: GatePipelineStep, python_files: list[str]) -> str | None:
        """Build the complexity gate command for changed Python files."""
        if not python_files:
            return None
        command = step.command_override or self._config.complexity_check_command
        if not command:
            return None
        return f"{command} {self._quote_paths(python_files)}"

    # -- helper: dead code command -------------------------------------------

    def _dead_code_command(self: Any, step: GatePipelineStep, python_files: list[str]) -> str:
        """Build the dead-code command line."""
        command = step.command_override or self._config.dead_code_command
        return f"{command} {self._quote_paths(python_files)} --min-confidence {self._config.dead_code_min_confidence}"

    # -- helper: measure complexity ------------------------------------------

    def _measure_complexity_sync(self: Any, command: str, cwd: Path) -> tuple[float | None, str]:
        """Execute a complexity command and parse its average score."""
        ok, detail = self._run_command_and_capture(command, cwd)
        if not ok:
            return None, detail
        score = self._parse_complexity_average(detail)
        if score is None:
            return None, "Could not parse complexity output."
        return score, detail

    def _measure_complexity_base_sync(self: Any, command: str) -> tuple[float | None, str]:
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
                encoding="utf-8",
                errors="replace",
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
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )

    # -- helper: parse complexity output -------------------------------------

    @staticmethod
    def _extract_complexity_from_dict(raw_map: dict[str, object]) -> float | None:
        """Extract complexity average from a parsed JSON dict."""
        for key in ("average_complexity", "average", "mean_complexity"):
            value = raw_map.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        complexities: list[float] = []
        for value in raw_map.values():
            if not isinstance(value, list):
                continue
            for item in cast("list[object]", value):
                if isinstance(item, dict):
                    complexity = cast("dict[str, object]", item).get("complexity")
                    if isinstance(complexity, (int, float)):
                        complexities.append(float(complexity))
        return sum(complexities) / len(complexities) if complexities else None

    def _parse_complexity_average(self: Any, output: str) -> float | None:
        """Parse an average complexity score from command output."""
        try:
            raw: object = json.loads(output)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict):
            result = self._extract_complexity_from_dict(cast("dict[str, object]", raw))
            if result is not None:
                return result
        try:
            return float(output.strip())
        except ValueError:
            return None

    # -- helper: detect import cycles ----------------------------------------

    @staticmethod
    def _discover_source_modules(run_dir: Path) -> tuple[dict[str, Path], Path]:
        """Discover Python source modules, returning module-to-path map and source root."""
        source_root = run_dir / "src"
        search_root = source_root if source_root.exists() else run_dir
        module_to_path: dict[str, Path] = {}
        for py_file in sorted(search_root.rglob("*.py")):
            rel_parts = py_file.relative_to(run_dir).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if "tests" in rel_parts:
                continue
            module = _module_name_from_path(py_file, search_root)
            if module:
                module_to_path[module] = py_file
        return module_to_path, search_root

    @staticmethod
    def _build_import_graph(module_to_path: dict[str, Path]) -> dict[str, set[str]]:
        """Build a module import graph from AST parsing."""
        graph: dict[str, set[str]] = {mod: set() for mod in module_to_path}
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
        return graph

    @staticmethod
    def _find_cycles(
        graph: dict[str, set[str]],
        changed_modules: set[str],
    ) -> set[tuple[str, ...]]:
        """Find import cycles that intersect with changed modules via DFS."""
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
                    cycle = (*stack[start:], neighbor)
                    if changed_modules.intersection(cycle):
                        cycles.add(cycle)
            stack.pop()
            in_stack.discard(node)

        for module in graph:
            if module not in visited:
                visit(module)
        return cycles

    def _detect_import_cycles_builtin(self: Any, changed_files: list[str], run_dir: Path) -> tuple[bool, str]:
        """Detect import cycles with a simple AST-based resolver."""
        module_to_path, search_root = self._discover_source_modules(run_dir)
        graph = self._build_import_graph(module_to_path)

        changed_modules = {
            _module_name_from_path(run_dir / rel_path, search_root)
            for rel_path in changed_files
            if rel_path.endswith(".py")
        }
        changed_modules.discard("")

        cycles = self._find_cycles(graph, changed_modules)
        if not cycles:
            return False, "No import cycles detected."
        cycle_lines = [" -> ".join(cycle) for cycle in sorted(cycles)]
        return True, "Import cycles detected: " + "; ".join(cycle_lines)
