"""Tests for bernstein.core.trigger_sources.gitlab — GitLab trigger adapters."""

from __future__ import annotations

from bernstein.core.trigger_sources.gitlab import normalize_job, normalize_pipeline


class TestNormalizePipeline:
    """Test GitLab pipeline webhook normalization."""

    def _sample_payload(self) -> dict:
        return {
            "object_kind": "pipeline",
            "object_attributes": {
                "id": 12345,
                "ref": "main",
                "sha": "abc123def456",
                "status": "failed",
                "url": "https://gitlab.example.com/org/project/-/pipelines/12345",
            },
            "project": {
                "name": "my-project",
                "path_with_namespace": "org/my-project",
            },
            "user": {"name": "alice"},
            "builds": [
                {"name": "lint", "stage": "quality", "status": "failed"},
                {"name": "test", "stage": "test", "status": "passed"},
            ],
        }

    def test_basic_fields(self) -> None:
        event = normalize_pipeline(self._sample_payload(), "alice", "org/my-project")
        assert event.source == "gitlab_pipeline"
        assert event.branch == "main"
        assert event.sha == "abc123def456"
        assert event.sender == "alice"
        assert "Pipeline 12345 failed" in event.message

    def test_metadata(self) -> None:
        event = normalize_pipeline(self._sample_payload(), "alice", "org/my-project")
        meta = event.metadata
        assert meta["status"] == "failed"
        assert meta["pipeline_id"] == 12345
        assert "pipelines/12345" in meta["pipeline_url"]


class TestNormalizeJob:
    """Test GitLab job webhook normalization."""

    def _sample_payload(self) -> dict:
        return {
            "object_kind": "build",
            "build_name": "lint",
            "build_stage": "quality",
            "build_status": "failed",
            "build_ref": "refs/heads/feature-x",
            "build_url": "https://gitlab.example.com/org/project/-/jobs/9876",
            "pipeline_id": 12345,
            "commit": {"sha": "def789"},
        }

    def test_basic_fields(self) -> None:
        event = normalize_job(self._sample_payload(), "bob", "org/project")
        assert event.source == "gitlab_job"
        assert event.branch == "refs/heads/feature-x"
        assert event.sha == "def789"
        assert event.sender == "bob"
        assert "lint" in event.message
        assert "failed" in event.message

    def test_metadata(self) -> None:
        event = normalize_job(self._sample_payload(), "bob", "org/project")
        meta = event.metadata
        assert meta["build_name"] == "lint"
        assert meta["build_stage"] == "quality"
        assert meta["pipeline_id"] == 12345