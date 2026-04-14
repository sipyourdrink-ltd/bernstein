"""Pipeline resolution, file handling, caching, and report persistence for quality gates."""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.quality.gate_pipeline import (
    TIMED_OUT_PREFIX,
    VALID_GATE_NAMES,
    GatePipelineStep,
    GateReport,
    GateResult,
    build_default_pipeline,
    is_dep_file,
    normalize_gate_condition,
)

if TYPE_CHECKING:
    from bernstein.core.models import Task
    from bernstein.core.quality.gate_plugins import GatePluginRegistry

logger = logging.getLogger(__name__)


class GateRunnerCacheMixin:
    """Mixin providing pipeline resolution, file handling, caching, and report persistence.

    This mixin is combined with :class:`~bernstein.core.gate_pipeline.GateRunner`
    at runtime.  Methods here reference ``self._config``, ``self._workdir``,
    ``self._base_ref``, ``self._changed_files_resolved``, and
    ``self._gate_plugin_registry`` via the GateRunner instance.
    """

    # -- mixin initialiser (called from GateRunner.__init__) -----------------

    @staticmethod
    def __init_cache__(instance: object) -> None:
        """Initialise per-instance cache state."""
        inst = cast("Any", instance)
        inst._cache_lock = threading.Lock()
        inst._cache_loaded = False
        inst._cache_entries = {}  # dict[str, dict[str, Any]]

    # -- pipeline resolution -------------------------------------------------

    def _resolve_pipeline(self: Any) -> list[GatePipelineStep]:
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

    def _condition_matches(self: Any, condition: str, changed_files: list[str]) -> bool:
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
            return any(is_dep_file(path) for path in changed_files)
        raise ValueError(f"Unsupported gate condition: {condition!r}")

    # -- command builders ----------------------------------------------------

    def _lint_command(self: Any, step: GatePipelineStep, changed_files: list[str]) -> str | None:
        if step.command_override is not None:
            return step.command_override
        python_files = self._python_files(changed_files)
        if self._changed_files_resolved:
            if not python_files:
                return None
            return f"ruff check {self._quote_paths(python_files)}"
        return self._config.lint_command

    def _type_check_command(self: Any, step: GatePipelineStep, changed_files: list[str]) -> str | None:
        if step.command_override is not None:
            return step.command_override
        python_files = self._python_files(changed_files)
        if self._changed_files_resolved:
            if not python_files:
                return None
            expanded = self._expand_type_check_files(python_files)
            return f"pyright {self._quote_paths(expanded)}"
        return self._config.type_check_command

    def _expand_type_check_files(self: Any, python_files: list[str]) -> list[str]:
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

    def _tests_command(self: Any, step: GatePipelineStep, run_dir: Path, changed_files: list[str]) -> str | None:
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

    def _optional_command(self: Any, gate_name: str, command_override: str | None) -> str | None:
        if command_override is not None:
            return command_override
        return {
            "security_scan": self._config.security_scan_command,
            "coverage_delta": self._config.coverage_delta_command,
            "complexity_check": self._config.complexity_check_command,
            "import_cycle": self._config.import_cycle_command,
        }[gate_name]

    # -- file helpers --------------------------------------------------------

    def _python_files(self: Any, changed_files: list[str]) -> list[str]:
        return [path for path in changed_files if path.endswith(".py")]

    def _quote_paths(self: Any, paths: list[str]) -> str:
        return " ".join(shlex.quote(path) for path in paths)

    def _tests_by_name_pattern(self: Any, run_dir: Path, module_name: str) -> set[str]:
        """Find test files matching a module name via glob patterns."""
        found: set[str] = set()
        for pattern in (f"tests/**/test_{module_name}.py", f"tests/**/*{module_name}*test.py"):
            for candidate in sorted(run_dir.glob(pattern)):
                if candidate.is_file():
                    found.add(candidate.relative_to(run_dir).as_posix())
        return found

    def _tests_in_ancestor_dirs(self: Any, run_dir: Path, rel_path: str) -> set[str]:
        """Find test files in ancestor test directories of a source file."""
        found: set[str] = set()
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
                    found.add(test_file.relative_to(run_dir).as_posix())
        return found

    def _impacted_tests(self: Any, run_dir: Path, changed_python_files: list[str]) -> list[str]:
        impacted: set[str] = set()
        for rel_path in changed_python_files:
            module_name = Path(rel_path).stem
            impacted.update(self._tests_by_name_pattern(run_dir, module_name))
            impacted.update(self._tests_in_ancestor_dirs(run_dir, rel_path))
        return sorted(impacted)

    def _resolve_changed_files_sync(self: Any, task: Task, run_dir: Path) -> list[str] | None:
        owned = self._existing_relative_paths(run_dir, task.owned_files)
        if owned:
            return owned
        git_diff = self._git_diff_changed_files(run_dir)
        if git_diff is not None:
            return git_diff
        return None

    def _existing_relative_paths(self: Any, run_dir: Path, candidates: list[str]) -> list[str]:
        existing: list[str] = []
        for rel in candidates:
            candidate = run_dir / rel
            if candidate.exists() and candidate.is_file():
                existing.append(Path(rel).as_posix())
        return sorted(set(existing))

    def _git_diff_changed_files(self: Any, run_dir: Path) -> list[str] | None:
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-only", f"{self._base_ref}..HEAD"],
                cwd=run_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in (0, 1):
            return None
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return self._existing_relative_paths(run_dir, lines)

    def _is_test_path(self: Any, path: str) -> bool:
        candidate = Path(path)
        return (
            candidate.parts[:1] == ("tests",)
            or candidate.name.startswith("test_")
            or candidate.name.endswith("_test.py")
        )

    # -- cache ---------------------------------------------------------------

    def _build_gate_config_for_cache(self: Any, step: GatePipelineStep) -> dict[str, object]:
        """Build the gate-specific config dict used for cache key hashing."""
        name = step.name
        cfg = self._config
        base: dict[str, object] = {
            "name": name,
            "required": step.required,
            "condition": step.condition,
            "command_override": step.command_override,
            "base_ref": self._base_ref,
            "timeout_s": cfg.timeout_s,
        }
        # Map each gate name to the config fields relevant for that gate.
        _gate_config_fields: dict[str, list[str]] = {
            "lint": ["lint_command"],
            "type_check": ["type_check_command"],
            "tests": ["test_command", "flaky_detection", "flaky_min_runs", "flaky_threshold"],
            "pii_scan": ["pii_scan_paths", "pii_ignore_paths", "pii_allowlist_prefixes"],
            "security_scan": ["security_scan", "security_scan_command"],
            "coverage_delta": ["coverage_delta", "coverage_delta_command"],
            "complexity_check": ["complexity_check", "complexity_threshold", "complexity_check_command"],
            "dead_code": [
                "dead_code_check",
                "dead_code_command",
                "dead_code_min_confidence",
                "dead_code_check_lost_callers",
                "dead_code_check_unused_imports",
                "dead_code_check_unreachable",
            ],
            "comment_quality": ["comment_quality_check", "comment_quality_docstyle"],
            "import_cycle": ["import_cycle_check", "import_cycle_command"],
            "merge_conflict": ["merge_conflict_check"],
        }
        for field_name in _gate_config_fields.get(name, []):
            base[field_name] = getattr(cfg, field_name, None)
        return base

    def _hash_changed_files(self: Any, run_dir: Path, changed_files: list[str]) -> list[dict[str, str]] | None:
        """Hash changed files for cache key. Returns None on read error."""
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
        return hashed_files

    def _make_cache_key_sync(
        self: Any,
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
        hashed_files = self._hash_changed_files(run_dir, changed_files)
        if hashed_files is None:
            return None
        relevant_config = self._build_gate_config_for_cache(step)
        payload = {"step": relevant_config, "files": hashed_files}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _get_cached_result_sync(self: Any, cache_key: str) -> GateResult | None:
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

    def _store_cached_result_sync(self: Any, cache_key: str, result: GateResult) -> None:
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

    def _ensure_cache_loaded_sync(self: Any) -> None:
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

    # -- report persistence --------------------------------------------------

    def _persist_report_sync(self: Any, report: GateReport) -> None:
        report_dir = self._workdir / ".sdd" / "runtime" / "gates"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{report.task_id}.json"
        report_path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8")

    def _skipped(self: Any, step: GatePipelineStep, details: str) -> GateResult:
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

    # -- command execution ---------------------------------------------------

    def _run_command_and_capture(self: Any, command: str, run_dir: Path) -> tuple[bool, str]:
        """Execute a command gate synchronously and capture its output."""
        from bernstein.core import quality_gates as qg

        return qg.run_command_sync(command, run_dir, self._config.timeout_s)

    def _command_failure_result(self: Any, step: GatePipelineStep, detail: str, command: str) -> GateResult:
        """Translate a command failure into a gate result."""
        if detail.startswith(TIMED_OUT_PREFIX):
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

    # -- plugin registry -----------------------------------------------------

    def _plugin_registry(self: Any) -> GatePluginRegistry:
        """Return a cached quality-gate plugin registry."""
        from bernstein.core.quality.gate_plugins import GatePluginRegistry

        if self._gate_plugin_registry is None:
            registry = GatePluginRegistry(self._workdir, built_in_names=VALID_GATE_NAMES)
            registry.discover()
            self._gate_plugin_registry = registry
        return self._gate_plugin_registry
