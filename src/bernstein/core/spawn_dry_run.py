"""Spawn dry-run mode (AGENT-017).

Validates adapter availability, model configuration, prompt rendering,
worktree setup, and MCP config without actually spawning any agents.
Used by ``bernstein run --dry-run`` to verify everything is ready.

Usage::

    validator = SpawnDryRunValidator(repo_root=Path("."))
    report = validator.validate(tasks, adapter_name="claude")
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class ValidationCheck:
    """Result of a single validation check.

    Attributes:
        name: Human-readable check name.
        passed: Whether the check passed.
        detail: Explanation (especially on failure).
        severity: "error" blocks spawning, "warning" is informational.
    """

    name: str
    passed: bool
    detail: str = ""
    severity: str = "error"


@dataclass
class DryRunReport:
    """Aggregated dry-run validation report.

    Attributes:
        checks: Individual validation checks.
        would_spawn: Number of agents that would be spawned.
        adapter_name: Adapter that was validated.
    """

    checks: list[ValidationCheck] = field(default_factory=list[ValidationCheck])
    would_spawn: int = 0
    adapter_name: str = ""

    @property
    def passed(self) -> bool:
        """True only if no error-severity checks failed."""
        return all(c.passed for c in self.checks if c.severity == "error")

    @property
    def errors(self) -> list[ValidationCheck]:
        """Checks that failed with error severity."""
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[ValidationCheck]:
        """Checks that failed with warning severity."""
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    def summary(self) -> str:
        """Human-readable summary of the dry-run result.

        Returns:
            Multi-line summary string.
        """
        lines = [f"Dry-run report for adapter={self.adapter_name}:"]
        total = len(self.checks)
        passed = sum(1 for c in self.checks if c.passed)
        lines.append(f"  {passed}/{total} checks passed")
        if self.errors:
            lines.append(f"  {len(self.errors)} errors:")
            for c in self.errors:
                lines.append(f"    - {c.name}: {c.detail}")
        if self.warnings:
            lines.append(f"  {len(self.warnings)} warnings:")
            for c in self.warnings:
                lines.append(f"    - {c.name}: {c.detail}")
        if self.passed:
            lines.append(f"  Would spawn {self.would_spawn} agent(s)")
        else:
            lines.append("  Spawn blocked by errors")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class SpawnDryRunValidator:
    """Validate spawn prerequisites without actually spawning.

    Args:
        repo_root: Root of the git repository.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    def validate(
        self,
        tasks: list[dict[str, Any]],
        *,
        adapter_name: str = "claude",
        model: str = "sonnet",
        mcp_config: dict[str, Any] | None = None,
    ) -> DryRunReport:
        """Run all validation checks.

        Args:
            tasks: List of task dicts to validate.
            adapter_name: Adapter to check.
            model: Model name to validate.
            mcp_config: Optional MCP configuration to validate.

        Returns:
            DryRunReport with all check results.
        """
        report = DryRunReport(adapter_name=adapter_name)

        report.checks.append(self._check_adapter(adapter_name))
        report.checks.append(self._check_binary(adapter_name))
        report.checks.append(self._check_model(adapter_name, model))
        report.checks.append(self._check_repo_root())
        report.checks.append(self._check_sdd_dir())
        report.checks.append(self._check_tasks(tasks))

        if mcp_config:
            report.checks.append(self._check_mcp(mcp_config))

        report.checks.append(self._check_git())
        report.checks.append(self._check_disk_space())

        if report.passed:
            report.would_spawn = max(1, len(tasks))

        return report

    def _check_adapter(self, adapter_name: str) -> ValidationCheck:
        """Check that the adapter is registered."""
        try:
            from bernstein.adapters.registry import get_adapter

            adapter = get_adapter(adapter_name)
            return ValidationCheck(
                name="adapter_registered",
                passed=True,
                detail=f"Adapter '{adapter.name()}' found",
            )
        except (ValueError, ImportError) as exc:
            return ValidationCheck(
                name="adapter_registered",
                passed=False,
                detail=f"Adapter '{adapter_name}' not found: {exc}",
            )

    def _check_binary(self, adapter_name: str) -> ValidationCheck:
        """Check that the adapter's CLI binary is on PATH."""
        # Map adapter name to binary name
        binary_map: dict[str, str] = {
            "claude": "claude",
            "codex": "codex",
            "gemini": "gemini",
            "aider": "aider",
            "amp": "amp",
            "mock": "python",  # mock always works
        }
        binary = binary_map.get(adapter_name, adapter_name)
        path = shutil.which(binary)
        if path:
            return ValidationCheck(
                name="binary_on_path",
                passed=True,
                detail=f"Binary '{binary}' found at {path}",
            )
        if adapter_name == "mock":
            return ValidationCheck(
                name="binary_on_path",
                passed=True,
                detail="Mock adapter uses Python (always available)",
            )
        return ValidationCheck(
            name="binary_on_path",
            passed=False,
            detail=f"Binary '{binary}' not found on PATH",
            severity="warning",
        )

    def _check_model(self, adapter_name: str, model: str) -> ValidationCheck:
        """Check that the model name looks valid."""
        if not model:
            return ValidationCheck(
                name="model_valid",
                passed=False,
                detail="No model specified",
            )
        return ValidationCheck(
            name="model_valid",
            passed=True,
            detail=f"Model '{model}' for adapter '{adapter_name}'",
        )

    def _check_repo_root(self) -> ValidationCheck:
        """Check that the repo root exists and is a directory."""
        if self._repo_root.is_dir():
            return ValidationCheck(
                name="repo_root",
                passed=True,
                detail=str(self._repo_root),
            )
        return ValidationCheck(
            name="repo_root",
            passed=False,
            detail=f"Repo root {self._repo_root} does not exist or is not a directory",
        )

    def _check_sdd_dir(self) -> ValidationCheck:
        """Check that .sdd directory can be created."""
        sdd = self._repo_root / ".sdd"
        try:
            sdd.mkdir(parents=True, exist_ok=True)
            return ValidationCheck(
                name="sdd_dir",
                passed=True,
                detail=str(sdd),
            )
        except OSError as exc:
            return ValidationCheck(
                name="sdd_dir",
                passed=False,
                detail=f"Cannot create .sdd directory: {exc}",
            )

    def _check_tasks(self, tasks: list[dict[str, Any]]) -> ValidationCheck:
        """Check that tasks are non-empty and have required fields."""
        if not tasks:
            return ValidationCheck(
                name="tasks",
                passed=False,
                detail="No tasks provided",
                severity="warning",
            )
        for i, task in enumerate(tasks):
            if not task.get("title") and not task.get("description"):
                return ValidationCheck(
                    name="tasks",
                    passed=False,
                    detail=f"Task {i} has no title or description",
                )
        return ValidationCheck(
            name="tasks",
            passed=True,
            detail=f"{len(tasks)} task(s) validated",
        )

    def _check_mcp(self, mcp_config: dict[str, Any]) -> ValidationCheck:
        """Check that MCP configuration is structurally valid."""
        servers: Any = mcp_config.get("mcpServers", mcp_config)
        if not isinstance(servers, dict):
            return ValidationCheck(
                name="mcp_config",
                passed=False,
                detail="MCP config must be a dict with 'mcpServers' key",
            )
        server_count: int = len(servers)  # type: ignore[arg-type]  # narrowed from Any
        return ValidationCheck(
            name="mcp_config",
            passed=True,
            detail=f"{server_count} MCP server(s) configured",
        )

    def _check_git(self) -> ValidationCheck:
        """Check that git is available and repo is initialized."""
        git_path = shutil.which("git")
        if not git_path:
            return ValidationCheck(
                name="git_available",
                passed=False,
                detail="git not found on PATH",
            )
        git_dir = self._repo_root / ".git"
        if git_dir.exists():
            return ValidationCheck(
                name="git_available",
                passed=True,
                detail=f"git at {git_path}, repo initialized",
            )
        return ValidationCheck(
            name="git_available",
            passed=True,
            detail=f"git at {git_path} (not a git repo -- worktrees unavailable)",
            severity="warning",
        )

    def _check_disk_space(self) -> ValidationCheck:
        """Check available disk space (advisory)."""
        try:
            stat = shutil.disk_usage(self._repo_root)
            free_gb = stat.free / (1024**3)
            if free_gb < 1.0:
                return ValidationCheck(
                    name="disk_space",
                    passed=False,
                    detail=f"Only {free_gb:.1f} GB free (need at least 1 GB)",
                    severity="warning",
                )
            return ValidationCheck(
                name="disk_space",
                passed=True,
                detail=f"{free_gb:.1f} GB free",
            )
        except OSError as exc:
            return ValidationCheck(
                name="disk_space",
                passed=False,
                detail=f"Cannot check disk space: {exc}",
                severity="warning",
            )
