"""Tests for finding reference resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.evidence.run_artifacts import (
    ArtifactPayload,
    post_run_artifact,
    read_artifact_rows,
)
from bernstein.core.quality.finding_resolver import (
    extract_finding_references,
    verify_finding_references,
)

_KEY = b"artifact-test-hmac-key-0123456789"


def _sdd(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    return sdd


def _sarif_result(
    *,
    start_line: int = 8,
    snippet: str = "eval(user_input)",
    uri: str = "./src/app.py",
    rule_id: str = "PY-TAINT-001",
) -> dict:
    return {
        "ruleId": rule_id,
        "message": {"text": "Untrusted input reaches eval"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {
                        "startLine": start_line,
                        "endLine": start_line,
                        "startColumn": 5,
                        "endColumn": 21,
                        "snippet": {"text": snippet},
                    },
                }
            }
        ],
    }


def _finding(result: dict | None = None, **overrides: str) -> ArtifactPayload:
    provenance = {
        "tool": "semgrep",
        "tool_version": "1.131.0",
        "pinned_ruleset_or_feed_digest": "sha256:" + "a" * 64,
        "invocation_argv_hash": "sha256:" + "b" * 64,
        "target": "git:0123456789abcdef",
    }
    provenance.update(overrides)
    return ArtifactPayload.finding(result or _sarif_result(), **provenance)


class TestExtractFindingReferences:
    def test_extract_versioned_reference(self) -> None:
        text = "See [FINDING:task-123:finding:1] for details"
        refs = extract_finding_references(text)
        assert len(refs) == 1
        assert refs[0].task_id == "task-123"
        assert refs[0].key == "finding"
        assert refs[0].version == 1
        assert refs[0].value == "[FINDING:task-123:finding:1]"

    def test_extract_latest_reference(self) -> None:
        text = "See [FINDING:task-123:finding] for details"
        refs = extract_finding_references(text)
        assert len(refs) == 1
        assert refs[0].task_id == "task-123"
        assert refs[0].key == "finding"
        assert refs[0].version is None
        assert refs[0].value == "[FINDING:task-123:finding]"

    def test_extract_multiple_references(self) -> None:
        text = "See [FINDING:task-1:finding:1] and [FINDING:task-2:finding:2] and [FINDING:task-3:finding]"
        refs = extract_finding_references(text)
        assert len(refs) == 3
        assert refs[0].task_id == "task-1"
        assert refs[1].task_id == "task-2"
        assert refs[2].task_id == "task-3"

    def test_versioned_takes_precedence_over_latest(self) -> None:
        # Versioned reference is more specific, should be matched first
        text = "[FINDING:task-1:finding:1] [FINDING:task-1:finding]"
        refs = extract_finding_references(text)
        # Both should be found
        assert len(refs) == 2


class TestVerifyFindingReferences:
    def test_verify_resolved_versioned(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        # Post a finding artifact
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-123",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )

        text = "See [FINDING:task-123:finding:1] for the issue"
        report = verify_finding_references(text, sdd)
        assert report.ok
        assert report.total == 1
        assert len(report.resolved) == 1
        assert len(report.unresolved) == 0

    def test_verify_resolved_latest(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        # Post a finding artifact
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-123",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )

        text = "See [FINDING:task-123:finding] for the issue"
        report = verify_finding_references(text, sdd)
        assert report.ok
        assert report.total == 1
        assert len(report.resolved) == 1

    def test_verify_unresolved_missing_task(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        text = "See [FINDING:missing-task:finding:1] for the issue"
        report = verify_finding_references(text, sdd)
        assert not report.ok
        assert report.total == 1
        assert len(report.unresolved) == 1

    def test_verify_unresolved_missing_key(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-123",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )
        text = "See [FINDING:task-123:missing-key:1] for the issue"
        report = verify_finding_references(text, sdd)
        assert not report.ok
        assert report.total == 1
        assert len(report.unresolved) == 1

    def test_verify_unresolved_wrong_version(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-123",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )
        text = "See [FINDING:task-123:finding:2] for the issue"
        report = verify_finding_references(text, sdd)
        assert not report.ok
        assert report.total == 1
        assert len(report.unresolved) == 1

    def test_verify_skips_in_offline_mode(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        text = "See [FINDING:task-123:finding:1] for the issue"
        report = verify_finding_references(text, sdd, offline=True)
        # In offline mode, all references are skipped, not failed
        assert report.ok
        assert report.total == 1
        assert len(report.resolved) == 0
        assert len(report.unresolved) == 0


class TestVerifyFindingReferencesInReport:
    def test_verify_report_with_finding_references_passes(self, tmp_path: Path) -> None:
        """Test that a report artifact with finding_references passes verification."""
        sdd = _sdd(tmp_path)
        # Post a finding artifact first
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-123",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )

        # Post a report referencing the finding
        report_payload = ArtifactPayload.report(
            "# Audit Report\n\nSee finding [FINDING:task-123:finding:1]",
            finding_references=[
                {
                    "task_id": "task-123",
                    "key": "finding",
                    "version": 1,
                    # Without the receipt hash there is nothing to re-check the
                    # finding against, and verification refuses the reference.
                    "finding_hash": read_artifact_rows(tmp_path / ".sdd", "task-123", verify=False)[0].content_hash,
                }
            ],
        )
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-report",
            key="audit-report",
            payload=report_payload,
            actor="reviewer",
            hmac_key=_KEY,
        )

        # Verify all artifacts
        from bernstein.core.evidence.run_artifacts import verify_run_artifacts

        results = verify_run_artifacts(sdd, "task-report", hmac_key=_KEY)
        assert len(results) == 1
        assert results[0].ok, results[0].reason

    def test_verify_report_with_missing_finding_reference_fails(self, tmp_path: Path) -> None:
        """Test that a report artifact with missing finding_references fails verification."""
        sdd = _sdd(tmp_path)
        # Post a report referencing a non-existent finding
        report_payload = ArtifactPayload.report(
            "# Audit Report\n\nSee finding [FINDING:missing:finding:1]",
            finding_references=[{"task_id": "missing", "key": "finding", "version": 1}],
        )
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-report",
            key="audit-report",
            payload=report_payload,
            actor="reviewer",
            hmac_key=_KEY,
        )

        from bernstein.core.evidence.run_artifacts import verify_run_artifacts

        results = verify_run_artifacts(sdd, "task-report", hmac_key=_KEY)
        assert len(results) == 1
        assert not results[0].ok
        assert "referenced finding" in results[0].reason

    def test_verify_report_with_wrong_version_fails(self, tmp_path: Path) -> None:
        """Test that a report artifact with wrong version fails verification."""
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-123",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )

        # Post a report referencing the finding with wrong version
        report_payload = ArtifactPayload.report(
            "# Audit Report\n\nSee finding [FINDING:task-123:finding:2]",
            finding_references=[{"task_id": "task-123", "key": "finding", "version": 2}],
        )
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-report",
            key="audit-report",
            payload=report_payload,
            actor="reviewer",
            hmac_key=_KEY,
        )

        from bernstein.core.evidence.run_artifacts import verify_run_artifacts

        results = verify_run_artifacts(sdd, "task-report", hmac_key=_KEY)
        assert len(results) == 1
        assert not results[0].ok
        assert "version 2 not found" in results[0].reason

    def test_verify_report_with_latest_version_passes(self, tmp_path: Path) -> None:
        """Test that a report artifact with no version (latest) passes."""
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-123",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )

        # Post a report referencing the finding without version (latest)
        report_payload = ArtifactPayload.report(
            "# Audit Report\n\nSee finding [FINDING:task-123:finding]",
            finding_references=[
                {
                    "task_id": "task-123",
                    "key": "finding",
                    "finding_hash": read_artifact_rows(sdd, "task-123", verify=False)[0].content_hash,
                }
            ],
        )
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-report",
            key="audit-report",
            payload=report_payload,
            actor="reviewer",
            hmac_key=_KEY,
        )

        from bernstein.core.evidence.run_artifacts import verify_run_artifacts

        results = verify_run_artifacts(sdd, "task-report", hmac_key=_KEY)
        assert len(results) == 1
        assert results[0].ok, results[0].reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
