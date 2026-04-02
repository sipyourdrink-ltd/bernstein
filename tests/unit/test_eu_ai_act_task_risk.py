"""Tests for task-level EU AI Act risk assessment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from bernstein.cli.compliance_cmd import compliance_group
from bernstein.core.eu_ai_act import RiskLevel, assess_risk, summarize_assessments
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _TaskProbe:
    def __init__(self, title: str, description: str, role: str = "backend") -> None:
        self.title = title
        self.description = description
        self.role = role


@dataclass(frozen=True)
class _ClientHarness:
    client: AsyncClient
    sdd_dir: Path


@pytest_asyncio.fixture()
async def client(tmp_path: Path) -> AsyncGenerator[_ClientHarness, None]:
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    app = create_app(jsonl_path=runtime_dir / "tasks.jsonl")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield _ClientHarness(client=async_client, sdd_dir=tmp_path / ".sdd")


class TestAssessRisk:
    def test_all_four_levels(self) -> None:
        minimal = _TaskProbe("Fix docs typo", "Update README wording")
        limited = _TaskProbe("Improve dashboard layout", "Refine the user-facing analytics panel")
        high = _TaskProbe("Strengthen auth middleware", "Harden authentication checks for login flows")
        unacceptable = _TaskProbe(
            "Implement social scoring engine",
            "Build citizen social scoring and real-time biometric surveillance support",
        )

        assert assess_risk(minimal) is RiskLevel.MINIMAL
        assert assess_risk(limited) is RiskLevel.LIMITED
        assert assess_risk(high) is RiskLevel.HIGH
        assert assess_risk(unacceptable) is RiskLevel.UNACCEPTABLE


class TestTaskCreationIntegration:
    @pytest.mark.asyncio
    async def test_create_task_applies_assessment_and_logs_metric(self, client: _ClientHarness) -> None:
        response = await client.client.post(
            "/tasks",
            json={
                "title": "Harden payment auth checks",
                "description": "Update payment authentication and authorization flows for checkout.",
                "role": "security",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["eu_ai_act_risk"] == "high"
        assert body["approval_required"] is True
        assert body["risk_level"] == "high"

        metrics_path = client.sdd_dir / "metrics" / "eu_ai_act_assessments.jsonl"
        lines = metrics_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["risk_level"] == "high"
        assert record["approval_required"] is True
        assert "authentication" in record["reasons"]


class TestComplianceCli:
    def test_cli_shows_summary(self, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        metrics_dir = sdd_dir / "metrics"
        metrics_dir.mkdir(parents=True)
        target = metrics_dir / "eu_ai_act_assessments.jsonl"
        target.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "task_id": "t1",
                            "title": "Docs cleanup",
                            "role": "docs",
                            "risk_level": "minimal",
                            "approval_required": False,
                            "bernstein_risk_level": "low",
                            "reasons": ["low-impact maintenance or documentation work"],
                            "assessed_at": "2026-04-02T00:00:00+00:00",
                        }
                    ),
                    json.dumps(
                        {
                            "task_id": "t2",
                            "title": "Audit payment auth",
                            "role": "security",
                            "risk_level": "high",
                            "approval_required": True,
                            "bernstein_risk_level": "high",
                            "reasons": ["payments", "authentication"],
                            "assessed_at": "2026-04-02T00:01:00+00:00",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(compliance_group, ["eu-ai-act", "--workdir", str(tmp_path)])

        assert result.exit_code == 0
        assert "Total assessments: 2" in result.output
        assert "high" in result.output
        assert "Audit payment auth" in result.output
        summary = summarize_assessments(sdd_dir)
        assert summary.counts["high"] == 1
