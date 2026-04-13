"""Orchestrator self-test on startup: validate server, adapters, config, disk, git.

Runs a battery of fast checks before the orchestrator enters its main loop.
Each check is independent and reports a pass/fail/skip result. If any
critical check fails, the orchestrator should refuse to start.

Usage::

    results = run_startup_selftest(
        server_url="http://localhost:8052",
        workdir=Path("/my/project"),
        adapter_name="claude",
    )
    if not results.all_critical_passed:
        sys.exit(1)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

logger = logging.getLogger(__name__)

# Minimum disk space in MB to operate
_MIN_DISK_MB = 100


class CheckStatus(StrEnum):
    """Outcome of a single self-test check."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    WARN = "warn"


@dataclass(frozen=True)
class CheckResult:
    """Result of a single self-test check.

    Attributes:
        name: Check identifier (e.g. ``"server_reachable"``).
        status: Pass/fail/skip/warn outcome.
        critical: Whether failure should block startup.
        message: Human-readable description.
        detail: Optional extra context.
    """

    name: str
    status: CheckStatus
    critical: bool
    message: str
    detail: str = ""


@dataclass(frozen=True)
class SelfTestReport:
    """Aggregate result of all startup self-test checks.

    Attributes:
        checks: Individual check results.
        all_critical_passed: True if no critical check failed.
        summary: One-line summary string.
    """

    checks: list[CheckResult]
    all_critical_passed: bool
    summary: str

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dictionary with checks list and summary.
        """
        return {
            "all_critical_passed": self.all_critical_passed,
            "summary": self.summary,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "critical": c.critical,
                    "message": c.message,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }


def run_startup_selftest(
    *,
    server_url: str = "http://localhost:8052",
    workdir: Path | None = None,
    adapter_name: str = "claude",
    client: httpx.Client | None = None,
) -> SelfTestReport:
    """Run all startup self-test checks.

    Args:
        server_url: Task server URL to check.
        workdir: Project working directory.
        adapter_name: CLI adapter name to validate.
        client: Optional httpx client for server check (injectable for testing).

    Returns:
        Aggregate self-test report.
    """
    checks: list[CheckResult] = []

    checks.append(_check_server(server_url, client))
    checks.append(_check_adapter(adapter_name))
    checks.append(_check_config(workdir))
    checks.append(_check_disk_space(workdir))
    checks.append(_check_git(workdir))
    checks.append(_check_sdd_dir(workdir))

    all_critical = all(c.status != CheckStatus.FAIL for c in checks if c.critical)
    passed = sum(1 for c in checks if c.status == CheckStatus.PASS)
    failed = sum(1 for c in checks if c.status == CheckStatus.FAIL)
    summary = f"Self-test: {passed} passed, {failed} failed, {len(checks) - passed - failed} skipped/warned"

    report = SelfTestReport(
        checks=checks,
        all_critical_passed=all_critical,
        summary=summary,
    )

    for check in checks:
        log_level = logging.INFO if check.status == CheckStatus.PASS else logging.WARNING
        if check.status == CheckStatus.FAIL and check.critical:
            log_level = logging.ERROR
        logger.log(log_level, "Self-test [%s] %s: %s", check.status.value, check.name, check.message)

    return report


def _check_server(server_url: str, client: httpx.Client | None = None) -> CheckResult:
    """Check that the task server is reachable.

    Args:
        server_url: Server base URL.
        client: Optional httpx client.

    Returns:
        Check result.
    """
    try:
        import httpx as _httpx

        c = client or _httpx.Client(timeout=5.0)
        try:
            resp = c.get(f"{server_url}/status")
            resp.raise_for_status()
            return CheckResult(
                name="server_reachable",
                status=CheckStatus.PASS,
                critical=True,
                message=f"Task server at {server_url} is reachable",
            )
        except Exception as exc:
            return CheckResult(
                name="server_reachable",
                status=CheckStatus.FAIL,
                critical=True,
                message=f"Task server at {server_url} is unreachable",
                detail=str(exc),
            )
        finally:
            if client is None:
                c.close()
    except ImportError:
        return CheckResult(
            name="server_reachable",
            status=CheckStatus.SKIP,
            critical=True,
            message="httpx not available",
        )


def _check_adapter(adapter_name: str) -> CheckResult:
    """Check that the CLI adapter binary is available.

    Args:
        adapter_name: Adapter name (e.g. ``"claude"``).

    Returns:
        Check result.
    """
    binary = adapter_name
    if adapter_name == "auto":
        binary = "claude"

    if shutil.which(binary) is not None:
        return CheckResult(
            name="adapter_available",
            status=CheckStatus.PASS,
            critical=False,
            message=f"Adapter binary '{binary}' found in PATH",
        )
    return CheckResult(
        name="adapter_available",
        status=CheckStatus.WARN,
        critical=False,
        message=f"Adapter binary '{binary}' not found in PATH",
        detail="Agents may fail to spawn if the adapter is not installed",
    )


def _check_config(workdir: Path | None) -> CheckResult:
    """Check that bernstein.yaml exists and is parseable.

    Args:
        workdir: Project working directory.

    Returns:
        Check result.
    """
    if workdir is None:
        return CheckResult(
            name="config_valid",
            status=CheckStatus.SKIP,
            critical=False,
            message="No workdir provided",
        )

    config_path = workdir / "bernstein.yaml"
    if not config_path.exists():
        return CheckResult(
            name="config_valid",
            status=CheckStatus.WARN,
            critical=False,
            message="bernstein.yaml not found (using defaults)",
        )

    try:
        from bernstein.core.seed import parse_seed

        parse_seed(config_path)
        return CheckResult(
            name="config_valid",
            status=CheckStatus.PASS,
            critical=False,
            message="bernstein.yaml is valid",
        )
    except Exception as exc:
        return CheckResult(
            name="config_valid",
            status=CheckStatus.FAIL,
            critical=False,
            message="bernstein.yaml is invalid",
            detail=str(exc),
        )


def _check_disk_space(workdir: Path | None) -> CheckResult:
    """Check that sufficient disk space is available.

    Args:
        workdir: Project working directory.

    Returns:
        Check result.
    """
    if workdir is None:
        return CheckResult(
            name="disk_space",
            status=CheckStatus.SKIP,
            critical=False,
            message="No workdir provided",
        )

    try:
        stat = os.statvfs(str(workdir))
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        if free_mb >= _MIN_DISK_MB:
            return CheckResult(
                name="disk_space",
                status=CheckStatus.PASS,
                critical=True,
                message=f"{free_mb:.0f} MB free (minimum: {_MIN_DISK_MB} MB)",
            )
        return CheckResult(
            name="disk_space",
            status=CheckStatus.FAIL,
            critical=True,
            message=f"Low disk space: {free_mb:.0f} MB free (minimum: {_MIN_DISK_MB} MB)",
        )
    except OSError as exc:
        return CheckResult(
            name="disk_space",
            status=CheckStatus.WARN,
            critical=True,
            message="Could not check disk space",
            detail=str(exc),
        )


def _check_git(workdir: Path | None) -> CheckResult:
    """Check that git is available and the workdir is a git repo.

    Args:
        workdir: Project working directory.

    Returns:
        Check result.
    """
    if shutil.which("git") is None:
        return CheckResult(
            name="git_available",
            status=CheckStatus.WARN,
            critical=False,
            message="git not found in PATH",
        )

    if workdir is None:
        return CheckResult(
            name="git_available",
            status=CheckStatus.PASS,
            critical=False,
            message="git is available",
        )

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(workdir),
        )
        if result.returncode == 0:
            return CheckResult(
                name="git_available",
                status=CheckStatus.PASS,
                critical=False,
                message="git is available and workdir is a git repository",
            )
        return CheckResult(
            name="git_available",
            status=CheckStatus.WARN,
            critical=False,
            message="Workdir is not a git repository",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="git_available",
            status=CheckStatus.WARN,
            critical=False,
            message="git check failed",
            detail=str(exc),
        )


def _check_sdd_dir(workdir: Path | None) -> CheckResult:
    """Check that the .sdd state directory exists or can be created.

    Args:
        workdir: Project working directory.

    Returns:
        Check result.
    """
    if workdir is None:
        return CheckResult(
            name="sdd_dir",
            status=CheckStatus.SKIP,
            critical=False,
            message="No workdir provided",
        )

    sdd = workdir / ".sdd"
    if sdd.exists() and sdd.is_dir():
        return CheckResult(
            name="sdd_dir",
            status=CheckStatus.PASS,
            critical=False,
            message=".sdd state directory exists",
        )

    try:
        sdd.mkdir(parents=True, exist_ok=True)
        return CheckResult(
            name="sdd_dir",
            status=CheckStatus.PASS,
            critical=False,
            message=".sdd state directory created",
        )
    except OSError as exc:
        return CheckResult(
            name="sdd_dir",
            status=CheckStatus.FAIL,
            critical=True,
            message="Cannot create .sdd state directory",
            detail=str(exc),
        )
