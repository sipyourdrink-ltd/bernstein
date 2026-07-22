"""Tests for scheduled dependency vulnerability scans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.dependency_scan import (
    CommandExecution,
    DependencyScanCommand,
    DependencyScanResult,
    DependencyScanStatus,
    DependencyVulnerabilityScanner,
    discover_dependency_scan_targets,
)
from httpx import ASGITransport, AsyncClient

from bernstein.cli.status import render_status_plain
from bernstein.core.server import create_app
from bernstein.core.tasks.models import OrchestratorConfig


def _vulnerable_pip_audit_output(package: str) -> CommandExecution:
    """A pip-audit payload reporting one vulnerable package."""
    return CommandExecution(
        returncode=1,
        stdout=json.dumps(
            {
                "dependencies": [
                    {
                        "name": package,
                        "version": "1.0.0",
                        "vulns": [{"id": "PYSEC-X", "description": "boom", "fix_versions": ["2.0.0"]}],
                    }
                ]
            }
        ),
        stderr="",
    )


def test_dependency_scan_creates_fix_tasks_for_vulnerable_packages(tmp_path: Path) -> None:
    # The scan only runs when the target repo declares Python dependencies.
    (tmp_path / "requirements.txt").write_text("jinja2==2.11.0\nurllib3==1.25.0\n", encoding="utf-8")
    outputs = {
        "pip-audit": CommandExecution(
            returncode=1,
            stdout=json.dumps(
                {
                    "dependencies": [
                        {
                            "name": "jinja2",
                            "version": "2.11.0",
                            "vulns": [
                                {
                                    "id": "PYSEC-1",
                                    "description": "Template injection issue",
                                    "fix_versions": ["3.1.0"],
                                }
                            ],
                        }
                    ]
                }
            ),
            stderr="",
        ),
        "safety": CommandExecution(
            returncode=64,
            stdout=json.dumps(
                [
                    {
                        "package_name": "urllib3",
                        "installed_version": "1.25.0",
                        "vulnerability_id": "SAFETY-1",
                        "advisory": "TLS verification issue",
                        "fixed_versions": ["1.26.18"],
                    }
                ]
            ),
            stderr="",
        ),
    }

    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        assert cwd == tmp_path
        assert timeout_s == 60
        return outputs[command.name]

    created_titles: list[str] = []
    scanner = DependencyVulnerabilityScanner(tmp_path, runner=runner)
    result = scanner.run_scan(create_fix_task=lambda finding: created_titles.append(finding.package) or finding.package)

    assert result.status == DependencyScanStatus.VULNERABLE
    assert len(result.findings) == 2
    assert sorted(created_titles) == ["jinja2", "urllib3"]
    latest = json.loads((tmp_path / ".sdd" / "runtime" / "dependency_scan_latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "vulnerable"
    assert latest["finding_count"] == 2
    metrics_lines = (tmp_path / ".sdd" / "metrics" / "dependency_vulnerability_scans.jsonl").read_text(encoding="utf-8")
    assert '"status": "vulnerable"' in metrics_lines


def test_dependency_scan_is_due_after_interval(tmp_path: Path) -> None:
    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        return CommandExecution(returncode=0, stdout=json.dumps([]), stderr="")

    scanner = DependencyVulnerabilityScanner(tmp_path, interval_s=100, runner=runner)
    # Fresh workspace: no baseline recorded yet, so the scan is not due on the
    # first tick (last_scan_at <= 0 means "no baseline", not "overdue").
    assert scanner.is_due(now=1_000.0) is False

    # First run_if_due records a baseline and performs no scan.
    assert (
        scanner.run_if_due(
            now=1_000.0,
            create_fix_task=lambda finding: None,
        )
        is None
    )

    # Baseline is now 1_000.0; the interval gate applies from there.
    assert scanner.is_due(now=1_050.0) is False
    assert scanner.is_due(now=1_101.0) is True


def test_dependency_scan_records_skipped_when_tools_unavailable(tmp_path: Path) -> None:
    # A declared manifest is required to reach the scanners at all.
    (tmp_path / "requirements.txt").write_text("jinja2==2.11.0\n", encoding="utf-8")

    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        return CommandExecution(returncode=127, stdout="", stderr=f"{command.name} not installed")

    scanner = DependencyVulnerabilityScanner(tmp_path, runner=runner)
    result = scanner.run_scan()

    assert result.status == DependencyScanStatus.SKIPPED
    assert "skipped" in result.summary.lower()


@pytest.mark.anyio
async def test_status_exposes_latest_dependency_scan(tmp_path: Path) -> None:
    from bernstein.core.routes import status as status_routes

    status_routes._runtime_cache = {}
    status_routes._runtime_cache_ts = 0.0
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=jsonl_path)
    latest_path = tmp_path / ".sdd" / "runtime" / "dependency_scan_latest.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(
        json.dumps(
            DependencyScanResult(
                scan_id="scan-1",
                scanned_at=1_000.0,
                status=DependencyScanStatus.VULNERABLE,
                summary="2 vulnerable dependency finding(s) from pip-audit, safety",
                scanners_run=("pip-audit", "safety"),
            ).to_dict()
        ),
        encoding="utf-8",
    )

    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dependency_scan"]["status"] == "vulnerable"
    plain = render_status_plain(payload)
    assert "Dependency scan: vulnerable" in plain


# --- issue #2788: scope the scan to the target repo, not the tool venv -----


def test_dependency_scan_skips_when_target_has_no_manifest(tmp_path: Path) -> None:
    """A target repo with no Python dependency manifest must not be scanned.

    Regression guard for issue #2788: without a manifest the scanners would
    audit the orchestrator's own tool venv and inject one foreign
    ``Upgrade vulnerable dependency`` task per installed package.
    """
    invoked: list[str] = []

    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        invoked.append(command.name)
        return _vulnerable_pip_audit_output("aiohttp")

    created: list[str] = []
    scanner = DependencyVulnerabilityScanner(tmp_path, runner=runner)
    result = scanner.run_scan(create_fix_task=lambda finding: created.append(finding.package) or finding.package)

    assert invoked == []  # scanners never executed against the active environment
    assert created == []  # zero foreign tasks injected
    assert result.status == DependencyScanStatus.SKIPPED
    assert result.findings == ()


def test_dependency_scan_first_run_records_baseline_without_scanning(tmp_path: Path) -> None:
    """A fresh workspace records a baseline instead of firing on the first tick."""
    (tmp_path / "requirements.txt").write_text("jinja2==2.11.0\n", encoding="utf-8")
    invoked: list[str] = []

    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        invoked.append(command.name)
        return _vulnerable_pip_audit_output("jinja2")

    scanner = DependencyVulnerabilityScanner(tmp_path, runner=runner)
    assert scanner.is_due(now=1_000.0) is False

    result = scanner.run_if_due(now=1_000.0, create_fix_task=lambda finding: finding.package)

    assert result is None  # no scan on the first tick
    assert invoked == []
    state = json.loads((tmp_path / ".sdd" / "runtime" / "dependency_scan_state.json").read_text(encoding="utf-8"))
    assert state["last_scan_at"] == 1_000.0


def test_dependency_scan_disabled_flag_skips_scan(tmp_path: Path) -> None:
    """When the scan is disabled, it never runs and never records state."""
    (tmp_path / "requirements.txt").write_text("jinja2==2.11.0\n", encoding="utf-8")
    invoked: list[str] = []

    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        invoked.append(command.name)
        return _vulnerable_pip_audit_output("jinja2")

    scanner = DependencyVulnerabilityScanner(tmp_path, enabled=False, runner=runner)

    assert scanner.is_due(now=5_000.0) is False
    assert scanner.run_if_due(now=5_000.0, create_fix_task=lambda finding: finding.package) is None
    assert invoked == []
    assert not (tmp_path / ".sdd" / "runtime" / "dependency_scan_state.json").exists()


def test_dependency_scan_targets_declared_requirements_file(tmp_path: Path) -> None:
    """When a manifest exists, the scanners are pointed at it, not the tool venv."""
    req = tmp_path / "requirements.txt"
    req.write_text("jinja2==2.11.0\n", encoding="utf-8")
    seen_argv: dict[str, tuple[str, ...]] = {}

    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        seen_argv[command.name] = command.argv
        return CommandExecution(returncode=0, stdout="[]", stderr="")

    scanner = DependencyVulnerabilityScanner(tmp_path, runner=runner)
    scanner.run_scan()

    assert "-r" in seen_argv["pip-audit"]
    assert str(req) in seen_argv["pip-audit"]
    assert "-r" in seen_argv["safety"]
    assert str(req) in seen_argv["safety"]


def test_dependency_scan_targets_pyproject_project_path(tmp_path: Path) -> None:
    """A pyproject-only project is audited as a project path (pip-audit), not the env."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    seen_argv: dict[str, tuple[str, ...]] = {}

    def runner(command: DependencyScanCommand, *, cwd: Path, timeout_s: int) -> CommandExecution:
        seen_argv[command.name] = command.argv
        return CommandExecution(returncode=0, stdout="[]", stderr="")

    scanner = DependencyVulnerabilityScanner(tmp_path, runner=runner)
    scanner.run_scan()

    # pip-audit can audit a project path; safety (which cannot target a bare
    # project) must not run against the active environment.
    assert str(tmp_path) in seen_argv["pip-audit"]
    assert "safety" not in seen_argv


def test_discover_targets_reports_no_declaration_for_empty_repo(tmp_path: Path) -> None:
    targets = discover_dependency_scan_targets(tmp_path)
    assert targets.declared is False
    assert targets.requirements_files == ()
    assert targets.project_path is None


def test_discover_targets_finds_requirements_and_pyproject(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("jinja2\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    targets = discover_dependency_scan_targets(tmp_path)

    assert targets.declared is True
    assert (tmp_path / "requirements.txt") in targets.requirements_files
    assert (tmp_path / "requirements-dev.txt") in targets.requirements_files
    assert targets.project_path == tmp_path
    assert targets.has_lockfile is True


def test_orchestrator_config_disables_dependency_scan_by_default() -> None:
    assert OrchestratorConfig().dependency_scan_enabled is False
