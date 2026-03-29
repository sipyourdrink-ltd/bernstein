"""Tests for bernstein.core.incident — Incident response."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.incident import (
    Incident,
    IncidentManager,
    IncidentSeverity,
    IncidentStatus,
    StateSnapshot,
)

# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------


class TestIncident:
    def test_create_incident(self) -> None:
        inc = Incident(
            id="INC-001",
            severity=IncidentSeverity.SEV1,
            title="Test incident",
            description="Something bad happened",
        )
        assert inc.status == IncidentStatus.OPEN
        assert inc.mitigated_at is None

    def test_mitigate(self) -> None:
        inc = Incident(
            id="INC-001",
            severity=IncidentSeverity.SEV2,
            title="Test",
            description="desc",
        )
        inc.mitigate(remediation="Reduced agent count")
        assert inc.status == IncidentStatus.MITIGATED
        assert inc.mitigated_at is not None
        assert inc.remediation == "Reduced agent count"

    def test_resolve(self) -> None:
        inc = Incident(
            id="INC-001",
            severity=IncidentSeverity.SEV3,
            title="Test",
            description="desc",
        )
        inc.resolve(root_cause="Bad config")
        assert inc.status == IncidentStatus.RESOLVED
        assert inc.resolved_at is not None
        assert inc.root_cause == "Bad config"

    def test_to_dict(self) -> None:
        inc = Incident(
            id="INC-001",
            severity=IncidentSeverity.SEV1,
            title="Test",
            description="desc",
            blast_radius=["T-001", "T-002"],
        )
        d = inc.to_dict()
        assert d["id"] == "INC-001"
        assert d["severity"] == "sev1"
        assert d["blast_radius"] == ["T-001", "T-002"]

    def test_to_markdown(self) -> None:
        inc = Incident(
            id="INC-001",
            severity=IncidentSeverity.SEV1,
            title="Critical failure",
            description="All agents crashed",
            blast_radius=["T-001"],
        )
        md = inc.to_markdown()
        assert "# Incident Report: INC-001" in md
        assert "SEV1" in md
        assert "All agents crashed" in md
        assert "T-001" in md

    def test_to_markdown_with_snapshot(self) -> None:
        snapshot = StateSnapshot(
            timestamp=1000.0,
            active_agents=[{"id": "agent-1"}],
            open_tasks=[],
            failed_tasks=[{"id": "T-001"}],
            error_budget_state={},
            slo_dashboard={},
            recent_errors=["Error 1", "Error 2"],
        )
        inc = Incident(
            id="INC-002",
            severity=IncidentSeverity.SEV2,
            title="Test",
            description="desc",
            snapshot=snapshot,
        )
        md = inc.to_markdown()
        assert "Active agents: 1" in md
        assert "Error 1" in md


# ---------------------------------------------------------------------------
# IncidentManager
# ---------------------------------------------------------------------------


class TestIncidentManager:
    def test_create_incident(self) -> None:
        mgr = IncidentManager()
        inc = mgr.create_incident(
            severity=IncidentSeverity.SEV3,
            title="Minor issue",
            description="A small problem",
        )
        assert inc.id.startswith("INC-")
        assert len(mgr.incidents) == 1

    def test_sev1_triggers_pause(self) -> None:
        mgr = IncidentManager(auto_pause=True)
        mgr.create_incident(
            severity=IncidentSeverity.SEV1,
            title="Critical",
            description="Critical failure",
        )
        assert mgr.should_pause is True

    def test_sev2_triggers_pause(self) -> None:
        mgr = IncidentManager(auto_pause=True)
        mgr.create_incident(
            severity=IncidentSeverity.SEV2,
            title="Major",
            description="Major failure",
        )
        assert mgr.should_pause is True

    def test_sev3_does_not_trigger_pause(self) -> None:
        mgr = IncidentManager(auto_pause=True)
        mgr.create_incident(
            severity=IncidentSeverity.SEV3,
            title="Minor",
            description="Minor failure",
        )
        assert mgr.should_pause is False

    def test_clear_pause(self) -> None:
        mgr = IncidentManager(auto_pause=True)
        mgr.create_incident(
            severity=IncidentSeverity.SEV1,
            title="Critical",
            description="Critical failure",
        )
        assert mgr.should_pause is True
        mgr.clear_pause()
        assert mgr.should_pause is False

    def test_open_incidents(self) -> None:
        mgr = IncidentManager()
        inc1 = mgr.create_incident(IncidentSeverity.SEV3, "A", "a")
        inc2 = mgr.create_incident(IncidentSeverity.SEV3, "B", "b")
        inc1.resolve()
        assert len(mgr.open_incidents) == 1
        assert mgr.open_incidents[0].id == inc2.id

    def test_check_for_incidents_critical_failure_rate(self) -> None:
        mgr = IncidentManager()
        inc = mgr.check_for_incidents(
            failed_task_count=8,
            total_task_count=10,
            consecutive_failures=0,
            error_budget_depleted=False,
        )
        assert inc is not None
        assert inc.severity == IncidentSeverity.SEV1

    def test_check_for_incidents_error_budget_depleted(self) -> None:
        mgr = IncidentManager()
        inc = mgr.check_for_incidents(
            failed_task_count=3,
            total_task_count=10,
            consecutive_failures=0,
            error_budget_depleted=True,
        )
        assert inc is not None
        assert inc.severity == IncidentSeverity.SEV2

    def test_check_for_incidents_consecutive_failures(self) -> None:
        mgr = IncidentManager()
        inc = mgr.check_for_incidents(
            failed_task_count=5,
            total_task_count=50,
            consecutive_failures=5,
            error_budget_depleted=False,
        )
        assert inc is not None
        assert inc.severity == IncidentSeverity.SEV3

    def test_check_for_incidents_no_incident(self) -> None:
        mgr = IncidentManager()
        inc = mgr.check_for_incidents(
            failed_task_count=1,
            total_task_count=50,
            consecutive_failures=1,
            error_budget_depleted=False,
        )
        assert inc is None

    def test_generate_post_mortem_task(self) -> None:
        mgr = IncidentManager()
        inc = mgr.create_incident(
            severity=IncidentSeverity.SEV1,
            title="System down",
            description="All agents crashed",
        )
        task = mgr.generate_post_mortem_task(inc)
        assert "Post-mortem" in task["title"]
        assert task["role"] == "qa"
        assert task["priority"] == "1"

    def test_save(self, tmp_path: Path) -> None:
        mgr = IncidentManager()
        inc = mgr.create_incident(
            severity=IncidentSeverity.SEV2,
            title="Test save",
            description="Testing persistence",
        )
        runtime_dir = tmp_path / "runtime"
        mgr.save(runtime_dir)

        incidents_dir = runtime_dir / "incidents"
        assert incidents_dir.exists()
        json_files = list(incidents_dir.glob("*.json"))
        assert len(json_files) == 1
        md_files = list(incidents_dir.glob("*.md"))
        assert len(md_files) == 1

        data = json.loads(json_files[0].read_text())
        assert data["title"] == "Test save"

    def test_get_summary(self) -> None:
        mgr = IncidentManager()
        mgr.create_incident(IncidentSeverity.SEV1, "A", "a")
        mgr.create_incident(IncidentSeverity.SEV2, "B", "b")
        inc3 = mgr.create_incident(IncidentSeverity.SEV3, "C", "c")
        inc3.resolve()

        summary = mgr.get_summary()
        assert summary["total"] == 3
        assert summary["open"] == 2
        assert summary["by_severity"]["sev1"] == 1
        assert summary["by_status"]["open"] == 2
        assert summary["by_status"]["resolved"] == 1
