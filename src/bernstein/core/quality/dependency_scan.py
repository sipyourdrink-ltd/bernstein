"""Scheduled dependency vulnerability scanning."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.audit import AuditLog

logger = logging.getLogger(__name__)

DEFAULT_DEPENDENCY_SCAN_INTERVAL_S = 7 * 24 * 60 * 60
DEFAULT_DEPENDENCY_SCAN_TIMEOUT_S = 60


class DependencyScanStatus(StrEnum):
    """Possible outcomes of a scheduled dependency scan."""

    CLEAN = "clean"
    VULNERABLE = "vulnerable"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class DependencyScanCommand:
    """A single dependency scan command."""

    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class DependencyVulnerabilityFinding:
    """A single vulnerable dependency finding."""

    package: str
    installed_version: str
    advisory_id: str
    summary: str
    source: str
    fix_versions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Serialize the finding for JSON output."""
        return {
            "package": self.package,
            "installed_version": self.installed_version,
            "advisory_id": self.advisory_id,
            "summary": self.summary,
            "source": self.source,
            "fix_versions": list(self.fix_versions),
        }


@dataclass(frozen=True)
class DependencyScanResult:
    """Typed result for one scheduled dependency scan run."""

    scan_id: str
    scanned_at: float
    status: DependencyScanStatus
    summary: str
    findings: tuple[DependencyVulnerabilityFinding, ...] = ()
    scanners_run: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    created_task_titles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Serialize the scan result for JSONL and status payloads."""
        return {
            "scan_id": self.scan_id,
            "scanned_at": self.scanned_at,
            "status": self.status.value,
            "summary": self.summary,
            "finding_count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
            "scanners_run": list(self.scanners_run),
            "errors": list(self.errors),
            "created_task_titles": list(self.created_task_titles),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> DependencyScanResult:
        """Deserialize a persisted scan result."""
        findings_raw = _as_list(raw.get("findings", []))
        findings: list[DependencyVulnerabilityFinding] = []
        for item_raw in findings_raw:
            item = _as_mapping(item_raw)
            if item is None:
                continue
            findings.append(
                DependencyVulnerabilityFinding(
                    package=_as_str(item.get("package", "")),
                    installed_version=_as_str(item.get("installed_version", "")),
                    advisory_id=_as_str(item.get("advisory_id", "")),
                    summary=_as_str(item.get("summary", "")),
                    source=_as_str(item.get("source", "")),
                    fix_versions=_as_str_tuple(item.get("fix_versions", [])),
                )
            )

        return cls(
            scan_id=_as_str(raw.get("scan_id", "")),
            scanned_at=_as_float(raw.get("scanned_at", 0.0)),
            status=DependencyScanStatus(_as_str(raw.get("status", DependencyScanStatus.SKIPPED.value))),
            summary=_as_str(raw.get("summary", "")),
            findings=tuple(findings),
            scanners_run=_as_str_tuple(raw.get("scanners_run", [])),
            errors=_as_str_tuple(raw.get("errors", [])),
            created_task_titles=_as_str_tuple(raw.get("created_task_titles", [])),
        )


@dataclass(frozen=True)
class CommandExecution:
    """Captured output from running a dependency scan command."""

    returncode: int
    stdout: str
    stderr: str


class DependencyCommandRunner(Protocol):
    """Protocol for injected dependency scan command runners."""

    def __call__(self, command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution: ...


def _as_mapping(value: object) -> dict[str, object] | None:
    """Return a JSON object as a typed mapping when possible."""
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _as_list(value: object) -> list[object]:
    """Return a JSON array as a typed list when possible."""
    return cast("list[object]", value) if isinstance(value, list) else []


def _as_str(value: object) -> str:
    """Coerce a JSON scalar into a string."""
    return value if isinstance(value, str) else ""


def _as_float(value: object) -> float:
    """Coerce a JSON scalar into a float."""
    return float(value) if isinstance(value, int | float) else 0.0


def _as_str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a JSON array into a tuple of strings."""
    return tuple(str(item) for item in _as_list(value))


def read_latest_dependency_scan(sdd_dir: Path) -> DependencyScanResult | None:
    """Read the latest persisted dependency scan result."""
    latest_path = sdd_dir / "runtime" / "dependency_scan_latest.json"
    if not latest_path.exists():
        return None
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    payload_map = _as_mapping(payload)
    if payload_map is None:
        return None
    return DependencyScanResult.from_dict(payload_map)


class DependencyVulnerabilityScanner:
    """Run weekly dependency vulnerability scans and persist their results."""

    def __init__(
        self,
        workdir: Path,
        *,
        interval_s: int = DEFAULT_DEPENDENCY_SCAN_INTERVAL_S,
        timeout_s: int = DEFAULT_DEPENDENCY_SCAN_TIMEOUT_S,
        runner: DependencyCommandRunner | None = None,
    ) -> None:
        self._workdir = workdir
        self._sdd_dir = workdir / ".sdd"
        self._runtime_dir = self._sdd_dir / "runtime"
        self._metrics_dir = self._sdd_dir / "metrics"
        self._state_path = self._runtime_dir / "dependency_scan_state.json"
        self._latest_path = self._runtime_dir / "dependency_scan_latest.json"
        self._metrics_path = self._metrics_dir / "dependency_vulnerability_scans.jsonl"
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._runner = runner or _run_dependency_command
        self._commands = (
            DependencyScanCommand(name="pip-audit", argv=("pip-audit", "--format=json")),
            DependencyScanCommand(name="safety", argv=("safety", "check", "--json")),
        )

    def is_due(self, *, now: float | None = None) -> bool:
        """Return True when the next scheduled scan should run."""
        last_scan_at = self._read_last_scan_at()
        current_time = time.time() if now is None else now
        return last_scan_at <= 0 or (current_time - last_scan_at) >= self._interval_s

    def run_if_due(
        self,
        *,
        create_fix_task: Callable[[DependencyVulnerabilityFinding], str | None] | None = None,
        audit_log: AuditLog | None = None,
        now: float | None = None,
    ) -> DependencyScanResult | None:
        """Run the dependency scan if its weekly schedule is due."""
        current_time = time.time() if now is None else now
        if not self.is_due(now=current_time):
            return None
        result = self.run_scan(create_fix_task=create_fix_task, audit_log=audit_log, now=current_time)
        self._write_state(current_time)
        return result

    def _run_scanners(self) -> tuple[list[DependencyVulnerabilityFinding], list[str], list[str], int, int]:
        """Execute all scanner commands. Returns (findings, errors, scanners_run, successful, unavailable)."""
        findings: list[DependencyVulnerabilityFinding] = []
        errors: list[str] = []
        scanners_run: list[str] = []
        successful_scanners = 0
        unavailable_scanners = 0

        for command in self._commands:
            execution = self._runner(command, cwd=self._workdir, timeout_s=self._timeout_s)
            if _command_unavailable(command, execution):
                unavailable_scanners += 1
                errors.append(f"{command.name}: unavailable")
                continue

            scanners_run.append(command.name)
            parsed_findings = _parse_findings(command.name, execution.stdout)
            if parsed_findings is None:
                errors.append(f"{command.name}: unable to parse output")
                continue

            successful_scanners += 1
            findings.extend(parsed_findings)
            if execution.returncode not in (0, 1, 64):
                errors.append(f"{command.name}: exited with code {execution.returncode}")

        return findings, errors, scanners_run, successful_scanners, unavailable_scanners

    @staticmethod
    def _create_fix_tasks(
        deduped_findings: list[DependencyVulnerabilityFinding],
        create_fix_task: Callable[[DependencyVulnerabilityFinding], str | None],
    ) -> list[str]:
        """Create fix tasks for unique packages, returning created task titles."""
        created: list[str] = []
        seen_packages: set[str] = set()
        for finding in deduped_findings:
            if finding.package in seen_packages:
                continue
            seen_packages.add(finding.package)
            title = create_fix_task(finding)
            if title:
                created.append(title)
        return created

    def run_scan(
        self,
        *,
        create_fix_task: Callable[[DependencyVulnerabilityFinding], str | None] | None = None,
        audit_log: AuditLog | None = None,
        now: float | None = None,
    ) -> DependencyScanResult:
        """Run one dependency vulnerability scan immediately."""
        current_time = time.time() if now is None else now

        findings, errors, scanners_run, successful_scanners, unavailable_scanners = self._run_scanners()

        deduped_findings = _dedupe_findings(findings)
        created_task_titles: list[str] = []
        if create_fix_task is not None:
            created_task_titles = self._create_fix_tasks(deduped_findings, create_fix_task)

        status = _determine_scan_status(
            findings=deduped_findings,
            successful_scanners=successful_scanners,
            unavailable_scanners=unavailable_scanners,
            total_scanners=len(self._commands),
            errors=errors,
        )
        summary = _build_summary(status, deduped_findings, scanners_run, errors)
        result = DependencyScanResult(
            scan_id=uuid.uuid4().hex[:12],
            scanned_at=current_time,
            status=status,
            summary=summary,
            findings=tuple(deduped_findings),
            scanners_run=tuple(scanners_run),
            errors=tuple(errors),
            created_task_titles=tuple(created_task_titles),
        )
        self._persist_result(result)
        if audit_log is not None:
            audit_log.log(
                "security.dependency_scan",
                "orchestrator",
                "dependency_scan",
                result.scan_id,
                details={
                    "status": result.status.value,
                    "finding_count": len(result.findings),
                    "scanners_run": list(result.scanners_run),
                    "created_task_titles": list(result.created_task_titles),
                    "errors": list(result.errors),
                },
            )
        return result

    def _persist_result(self, result: DependencyScanResult) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result.to_dict(), sort_keys=True)
        self._latest_path.write_text(payload, encoding="utf-8")
        with self._metrics_path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")

    def _read_last_scan_at(self) -> float:
        if not self._state_path.exists():
            return 0.0
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0.0
        payload_map = _as_mapping(payload)
        if payload_map is None:
            return 0.0
        return _as_float(payload_map.get("last_scan_at", 0.0))

    def _write_state(self, scanned_at: float) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"last_scan_at": scanned_at}, sort_keys=True), encoding="utf-8")


def _run_dependency_command(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
    """Run a dependency scan command with captured output."""
    if shutil.which(command.argv[0]) is None:
        return CommandExecution(returncode=127, stdout="", stderr=f"{command.argv[0]} not installed")
    completed = subprocess.run(
        list(command.argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout_s,
    )
    return CommandExecution(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _command_unavailable(command: DependencyScanCommand, execution: CommandExecution) -> bool:
    """Return True when a scan command is unavailable on this machine."""
    if execution.returncode == 127:
        return True
    stderr = execution.stderr.lower()
    return execution.stdout.strip() == "" and any(
        marker in stderr
        for marker in (
            f"{command.argv[0].lower()} not installed",
            "command not found",
            "no such file or directory",
        )
    )


def _parse_findings(command_name: str, stdout: str) -> list[DependencyVulnerabilityFinding] | None:
    """Parse findings for a supported dependency scanner."""
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return None

    if command_name == "pip-audit":
        return _parse_pip_audit_findings(payload)
    if command_name == "safety":
        return _parse_safety_findings(payload)
    return []


def _parse_pip_audit_findings(payload: object) -> list[DependencyVulnerabilityFinding]:
    """Parse JSON findings produced by ``pip-audit --format=json``."""
    payload_map = _as_mapping(payload)
    if payload_map is not None:
        dependencies = _as_list(payload_map.get("dependencies", []))
    elif isinstance(payload, list):
        dependencies = cast("list[object]", payload)
    else:
        return []

    findings: list[DependencyVulnerabilityFinding] = []
    for dependency_raw in dependencies:
        dependency = _as_mapping(dependency_raw)
        if dependency is None:
            continue
        package = _as_str(dependency.get("name", "")).strip()
        installed_version = _as_str(dependency.get("version", "")).strip()
        vulnerabilities = _as_list(dependency.get("vulns", []))
        for vulnerability_raw in vulnerabilities:
            vulnerability = _as_mapping(vulnerability_raw)
            if vulnerability is None:
                continue
            findings.append(
                DependencyVulnerabilityFinding(
                    package=package,
                    installed_version=installed_version,
                    advisory_id=_as_str(vulnerability.get("id", "unknown")),
                    summary=_as_str(vulnerability.get("description", "") or vulnerability.get("summary", "")),
                    source="pip-audit",
                    fix_versions=_as_str_tuple(vulnerability.get("fix_versions", [])),
                )
            )
    return findings


def _parse_safety_findings(payload: object) -> list[DependencyVulnerabilityFinding]:
    """Parse JSON findings produced by ``safety check --json``."""
    payload_map = _as_mapping(payload)
    vulnerabilities = (
        _as_list(payload_map.get("vulnerabilities", payload_map.get("issues", [])))
        if payload_map is not None
        else _as_list(payload)
    )

    findings: list[DependencyVulnerabilityFinding] = []
    for vulnerability_raw in vulnerabilities:
        vulnerability = _as_mapping(vulnerability_raw)
        if vulnerability is None:
            continue
        findings.append(
            DependencyVulnerabilityFinding(
                package=_as_str(vulnerability.get("package_name", vulnerability.get("package", ""))),
                installed_version=_as_str(vulnerability.get("installed_version", vulnerability.get("version", ""))),
                advisory_id=_as_str(vulnerability.get("vulnerability_id", vulnerability.get("id", "unknown"))),
                summary=_as_str(vulnerability.get("advisory", vulnerability.get("description", ""))),
                source="safety",
                fix_versions=_as_str_tuple(vulnerability.get("fixed_versions", [])),
            )
        )
    return findings


def _dedupe_findings(findings: list[DependencyVulnerabilityFinding]) -> list[DependencyVulnerabilityFinding]:
    """Deduplicate findings by package, advisory id, and source."""
    deduped: dict[tuple[str, str, str], DependencyVulnerabilityFinding] = {}
    for finding in findings:
        deduped[(finding.package, finding.advisory_id, finding.source)] = finding
    return sorted(deduped.values(), key=lambda finding: (finding.package, finding.source, finding.advisory_id))


def _determine_scan_status(
    *,
    findings: list[DependencyVulnerabilityFinding],
    successful_scanners: int,
    unavailable_scanners: int,
    total_scanners: int,
    errors: list[str],
) -> DependencyScanStatus:
    """Determine the overall result status for the scan."""
    if findings:
        return DependencyScanStatus.VULNERABLE
    if successful_scanners > 0 and not errors:
        return DependencyScanStatus.CLEAN
    if unavailable_scanners == total_scanners:
        return DependencyScanStatus.SKIPPED
    if successful_scanners > 0:
        return DependencyScanStatus.CLEAN
    return DependencyScanStatus.ERROR


def _build_summary(
    status: DependencyScanStatus,
    findings: list[DependencyVulnerabilityFinding],
    scanners_run: list[str],
    errors: list[str],
) -> str:
    """Build a short human-readable summary for status surfaces."""
    scanner_label = ", ".join(scanners_run) if scanners_run else "no scanners"
    if status == DependencyScanStatus.VULNERABLE:
        return f"{len(findings)} vulnerable dependency finding(s) from {scanner_label}"
    if status == DependencyScanStatus.CLEAN:
        suffix = f" ({'; '.join(errors)})" if errors else ""
        return f"No vulnerable dependencies found via {scanner_label}{suffix}"
    if status == DependencyScanStatus.SKIPPED:
        return "Dependency scan skipped because pip-audit and safety are unavailable"
    return f"Dependency scan failed: {'; '.join(errors) or 'unknown error'}"
