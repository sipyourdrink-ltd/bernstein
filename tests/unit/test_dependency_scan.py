"""Tests for scheduled dependency vulnerability scans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.cli.status import render_status_plain
from bernstein.core.dependency_scan import (
    CommandExecution,
    DependencyScanCommand,
    DependencyScanResult,
    DependencyScanStatus,
    DependencyVulnerabilityScanner,
)
from bernstein.core.server import create_app


def test_dependency_scan_creates_fix_tasks_for_vulnerable_packages(tmp_path: Path) -> None:
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
    assert scanner.is_due(now=1_000.0) is True

    scanner.run_if_due(
        now=1_000.0,
        create_fix_task=lambda finding: None,
    )

    assert scanner.is_due(now=1_050.0) is False
    assert scanner.is_due(now=1_101.0) is True


def test_dependency_scan_records_skipped_when_tools_unavailable(tmp_path: Path) -> None:
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
