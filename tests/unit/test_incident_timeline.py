"""Tests for incident timeline — correlating incidents with metrics and traces."""

from __future__ import annotations

import json
import time

import pytest

from bernstein.core.incident_timeline import (
    TimelineEvent,
    build_incident_timeline,
    list_incidents,
)


@pytest.fixture()
def sdd_dir(tmp_path):
    """Create a temp .sdd/ with metrics, traces, and incidents dirs."""
    sdd = tmp_path / ".sdd"
    (sdd / "metrics").mkdir(parents=True)
    (sdd / "traces").mkdir(parents=True)
    (sdd / "runtime" / "incidents").mkdir(parents=True)
    return sdd


@pytest.fixture()
def sample_incident(sdd_dir):
    """Write a sample incident JSON."""
    now = time.time()
    incident = {
        "id": "INC-TEST-001",
        "severity": "sev2",
        "title": "High failure rate detected",
        "description": "5 of 8 tasks failed in the last 10 minutes.",
        "status": "resolved",
        "created_at": now - 300,
        "mitigated_at": now - 120,
        "resolved_at": now - 30,
        "blast_radius": ["task-aaa", "task-bbb"],
        "root_cause": "Provider outage on openrouter",
        "remediation": "Switched to fallback provider",
        "post_mortem_task_id": None,
        "snapshot": None,
    }
    path = sdd_dir / "runtime" / "incidents" / "INC-TEST-001.json"
    path.write_text(json.dumps(incident), encoding="utf-8")
    return incident


@pytest.fixture()
def sample_error_metrics(sdd_dir, sample_incident):
    """Write sample error_rate JSONL metrics."""
    now = sample_incident["created_at"]
    lines = [
        json.dumps(
            {
                "timestamp": now - 400,
                "metric_type": "error_rate",
                "value": 1.0,
                "labels": {"error_type": "timeout", "provider": "openrouter", "role": "backend"},
            }
        ),
        json.dumps(
            {
                "timestamp": now - 200,
                "metric_type": "error_rate",
                "value": 1.0,
                "labels": {"error_type": "rate_limit", "provider": "openrouter", "role": "qa"},
            }
        ),
        json.dumps(
            {
                "timestamp": now + 100,
                "metric_type": "error_rate",
                "value": 0.5,
                "labels": {"error_type": "llm_failed", "provider": "anthropic", "role": "backend"},
            }
        ),
    ]
    today = time.strftime("%Y-%m-%d")
    path = sdd_dir / "metrics" / f"error_rate_{today}.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture()
def sample_task_metrics(sdd_dir, sample_incident):
    """Write sample task_completion_time JSONL metrics."""
    now = sample_incident["created_at"]
    lines = [
        json.dumps(
            {
                "timestamp": now - 500,
                "metric_type": "task_completion_time",
                "value": 45.2,
                "labels": {"task_id": "task-aaa", "role": "backend", "success": False},
            }
        ),
        json.dumps(
            {
                "timestamp": now - 100,
                "metric_type": "task_completion_time",
                "value": 30.1,
                "labels": {"task_id": "task-bbb", "role": "qa", "success": True},
            }
        ),
    ]
    today = time.strftime("%Y-%m-%d")
    path = sdd_dir / "metrics" / f"task_completion_time_{today}.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture()
def sample_trace(sdd_dir, sample_incident):
    """Write a sample trace JSON."""
    now = sample_incident["created_at"]
    trace = {
        "trace_id": "trace-001",
        "session_id": "session-001",
        "task_ids": ["task-aaa"],
        "agent_role": "backend",
        "model": "sonnet",
        "effort": "high",
        "spawn_ts": now - 500,
        "end_ts": now - 100,
        "steps": [
            {
                "type": "spawn",
                "timestamp": now - 500,
                "detail": "Agent spawned",
                "files": [],
                "tokens": 0,
                "duration_ms": 0,
            },
            {
                "type": "edit",
                "timestamp": now - 450,
                "detail": "Edited src/main.py",
                "files": ["src/main.py"],
                "tokens": 200,
                "duration_ms": 1500,
            },
            {
                "type": "verify",
                "timestamp": now - 400,
                "detail": "Tests failed",
                "files": [],
                "tokens": 50,
                "duration_ms": 3000,
            },
            {
                "type": "fail",
                "timestamp": now - 350,
                "detail": "Agent crashed: timeout",
                "files": [],
                "tokens": 0,
                "duration_ms": 0,
            },
        ],
        "outcome": "failed",
    }
    path = sdd_dir / "traces" / "trace-001.json"
    path.write_text(json.dumps(trace), encoding="utf-8")
    return path


@pytest.fixture()
def sample_api_metrics(sdd_dir, sample_incident):
    """Write sample api_usage JSONL with a failure."""
    now = sample_incident["created_at"]
    lines = [
        json.dumps(
            {
                "timestamp": now - 250,
                "metric_type": "api_usage",
                "value": 1.0,
                "labels": {"provider": "openrouter", "model": "sonnet", "success": False, "latency_ms": 5000},
            }
        ),
    ]
    today = time.strftime("%Y-%m-%d")
    path = sdd_dir / "metrics" / f"api_usage_{today}.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --- Tests ---


class TestTimelineEvent:
    def test_to_dict(self) -> None:
        ev = TimelineEvent(
            timestamp=1700000000.0,
            kind="error",
            source="metrics",
            summary="Test error",
            details={"key": "value"},
        )
        d = ev.to_dict()
        assert d["kind"] == "error"
        assert d["source"] == "metrics"
        assert d["summary"] == "Test error"
        assert d["details"] == {"key": "value"}
        assert "time" in d


class TestBuildIncidentTimeline:
    def test_full_timeline(
        self,
        sdd_dir,
        sample_incident,
        sample_error_metrics,
        sample_task_metrics,
        sample_trace,
        sample_api_metrics,
    ) -> None:
        result = build_incident_timeline(
            incident_id="INC-TEST-001",
            workdir=sdd_dir.parent,
            window_before_s=600,
            window_after_s=600,
        )

        assert "error" not in result
        assert result["incident_id"] == "INC-TEST-001"
        assert result["severity"] == "sev2"
        assert result["title"] == "High failure rate detected"
        assert result["event_count"] > 0

        # Check that events are sorted by timestamp
        events = result["events"]
        for i in range(1, len(events)):
            assert events[i]["timestamp"] >= events[i - 1]["timestamp"]

        # Check that we have incident lifecycle events
        kinds = [e["kind"] for e in events]
        assert "incident_created" in kinds
        assert "incident_mitigated" in kinds
        assert "incident_resolved" in kinds

        # Check that we have error events
        assert "error" in kinds

        # Check that we have task events
        assert "task_failed" in kinds or "task_completed" in kinds

        # Check that we have trace events
        assert "trace_step" in kinds or "agent_spawned" in kinds or "agent_crashed" in kinds

    def test_incident_not_found(self, sdd_dir) -> None:
        result = build_incident_timeline(
            incident_id="INC-NONEXISTENT",
            workdir=sdd_dir.parent,
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_timeline_persisted(
        self,
        sdd_dir,
        sample_incident,
        sample_error_metrics,
    ) -> None:
        _result = build_incident_timeline(
            incident_id="INC-TEST-001",
            workdir=sdd_dir.parent,
            window_before_s=600,
            window_after_s=600,
        )

        # Check that the timeline was persisted
        timeline_path = sdd_dir / "runtime" / "incidents" / "INC-TEST-001-timeline.json"
        assert timeline_path.exists()
        saved = json.loads(timeline_path.read_text(encoding="utf-8"))
        assert saved["incident_id"] == "INC-TEST-001"

    def test_empty_metrics_dir(self, sdd_dir, sample_incident) -> None:
        """Timeline should still work with no metric files."""
        result = build_incident_timeline(
            incident_id="INC-TEST-001",
            workdir=sdd_dir.parent,
        )
        assert "error" not in result
        # Should have at least the lifecycle events
        assert result["event_count"] >= 3

    def test_blast_radius_in_timeline(
        self,
        sdd_dir,
        sample_incident,
    ) -> None:
        result = build_incident_timeline(
            incident_id="INC-TEST-001",
            workdir=sdd_dir.parent,
        )
        assert result["blast_radius"] == ["task-aaa", "task-bbb"]
        assert result["root_cause"] == "Provider outage on openrouter"
        assert result["remediation"] == "Switched to fallback provider"


class TestListIncidents:
    def test_list_incidents(self, sdd_dir, sample_incident) -> None:
        incidents = list_incidents(sdd_dir.parent)
        assert len(incidents) == 1
        assert incidents[0]["id"] == "INC-TEST-001"
        assert incidents[0]["severity"] == "sev2"

    def test_list_empty(self, sdd_dir) -> None:
        incidents = list_incidents(sdd_dir.parent)
        assert incidents == []

    def test_skips_timeline_files(self, sdd_dir, sample_incident) -> None:
        # Write a timeline file that should be skipped
        timeline_path = sdd_dir / "runtime" / "incidents" / "INC-TEST-001-timeline.json"
        timeline_path.write_text('{"incident_id": "INC-TEST-001"}', encoding="utf-8")

        incidents = list_incidents(sdd_dir.parent)
        assert len(incidents) == 1  # Should not double-count
