"""Async quality gate runner with incremental execution and cached reports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.models import Task
    from bernstein.core.quality_gates import QualityGatesConfig

logger = logging.getLogger(__name__)

GateStatus = Literal["pass", "fail", "timeout", "skipped", "bypassed"]

VALID_GATE_NAMES = frozenset(
    {
        "lint",
        "type_check",
        "tests",
        "pii_scan",
        "mutation_testing",
        "intent_verification",
        "security_scan",
        "coverage_delta",
        "complexity_check",
        "import_cycle",
    }
)
VALID_GATE_CONDITIONS = frozenset({"always", "python_changed", "tests_changed", "any_changed"})
LEGACY_PYTHON_CONDITION = "changed_files.any('.py')"


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
    metadata: dict[str, Any] = field(default_factory=dict)


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
    pipeline = [
        GatePipelineStep(name="lint", required=True, condition="always"),
        GatePipelineStep(name="type_check", required=config.type_check, condition="python_changed"),
        GatePipelineStep(name="tests", required=config.tests, condition="python_changed"),
        GatePipelineStep(name="pii_scan", required=config.pii_scan, condition="any_changed"),
    ]
    if config.mutation_testing:
        pipeline.append(GatePipelineStep(name="mutation_testing", required=True, condition="python_changed"))
    if config.intent_verification.enabled:
        pipeline.append(GatePipelineStep(name="intent_verification", required=False, condition="any_changed"))
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

        results = await asyncio.gather(
            *[
                self._run_step(
                    step,
                    task,
                    run_dir,
                    changed_files,
                    skip_set=skip_set,
                    bypass_reason=bypass_reason,
                )
                for step in pipeline
            ]
        )
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
        result = await self._execute_gate(step, task, run_dir, changed_files)
        result.duration_ms = int((time.perf_counter() - started) * 1000)

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

        if step.name == "lint":
            command = self._lint_command(step, changed_files)
            if command is None:
                return self._skipped(step, "No Python files changed.")
            return await self._run_command_gate(step, command, run_dir, self._config.timeout_s, pass_detail="no lint violations")

        if step.name == "type_check":
            command = self._type_check_command(step, changed_files)
            if command is None:
                return self._skipped(step, "No Python files changed.")
            return await self._run_command_gate(step, command, run_dir, self._config.timeout_s, pass_detail="no type errors")

        if step.name == "tests":
            command = self._tests_command(step, run_dir, changed_files)
            if command is None:
                return self._skipped(step, "No impacted tests detected.")
            return await self._run_command_gate(step, command, run_dir, self._config.timeout_s, pass_detail="all tests passing")

        if step.name == "pii_scan":
            pii_result = await asyncio.to_thread(
                qg._run_pii_gate,
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

        if step.name == "mutation_testing":
            ok, detail, score = await asyncio.to_thread(qg._run_mutation_gate, self._config, run_dir)
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

        if step.name == "intent_verification":
            verdict, blocked = await asyncio.to_thread(qg._run_intent_gate, task, run_dir, self._config.intent_verification)
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

        if step.name in {"security_scan", "coverage_delta", "complexity_check", "import_cycle"}:
            command = self._optional_command(step.name, step.command_override)
            if command is None:
                return self._skipped(step, f"{step.name} is not configured.")
            return await self._run_command_gate(step, command, run_dir, self._config.timeout_s)

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

        ok, detail = await asyncio.to_thread(qg._run_command, command, run_dir, timeout_s)
        status: GateStatus
        blocked = False
        normalized_detail = detail
        if detail.startswith("Timed out after "):
            status = "timeout"
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

    def _resolve_pipeline(self) -> list[GatePipelineStep]:
        pipeline = self._config.pipeline if self._config.pipeline is not None else build_default_pipeline(self._config)
        normalized: list[GatePipelineStep] = []
        for step in pipeline:
            if step.name not in VALID_GATE_NAMES:
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
            return f"pyright {self._quote_paths(python_files)}"
        return self._config.type_check_command

    def _tests_command(self, step: GatePipelineStep, run_dir: Path, changed_files: list[str]) -> str | None:
        if step.command_override is not None:
            return step.command_override
        if not self._changed_files_resolved:
            return self._config.test_command
        impacted = self._impacted_tests(run_dir, self._python_files(changed_files))
        if impacted:
            return f"uv run pytest {self._quote_paths(impacted)}"
        return self._config.test_command if self._python_files(changed_files) else None

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
            for candidate_dir in [parent / "tests", *(ancestor / "tests" for ancestor in parent.parents if ancestor != run_dir.parent)]:
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
            "pii_scan_paths": self._config.pii_scan_paths if step.name == "pii_scan" else None,
            "pii_ignore_paths": self._config.pii_ignore_paths if step.name == "pii_scan" else None,
            "pii_allowlist_prefixes": self._config.pii_allowlist_prefixes if step.name == "pii_scan" else None,
            "security_scan_command": self._config.security_scan_command if step.name == "security_scan" else None,
            "coverage_delta_command": self._config.coverage_delta_command if step.name == "coverage_delta" else None,
            "complexity_check_command": self._config.complexity_check_command if step.name == "complexity_check" else None,
            "import_cycle_command": self._config.import_cycle_command if step.name == "import_cycle" else None,
        }
        payload = {"step": relevant_config, "files": hashed_files}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _get_cached_result_sync(self, cache_key: str) -> GateResult | None:
        self._ensure_cache_loaded_sync()
        raw = self._cache_entries.get(cache_key)
        if raw is None:
            return None
        status = raw.get("status")
        if status not in {"pass", "fail", "skipped"}:
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
                    data = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._cache_entries = {str(key): value for key, value in data.items() if isinstance(value, dict)}
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

    def _is_test_path(self, path: str) -> bool:
        candidate = Path(path)
        return candidate.parts[:1] == ("tests",) or candidate.name.startswith("test_") or candidate.name.endswith("_test.py")
