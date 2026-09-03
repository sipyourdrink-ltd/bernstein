"""Unit tests for compliance coverage assessment."""

from __future__ import annotations

from dataclasses import dataclass

from bernstein.core.compliance.coverage import (
    CONTROL_EVENT_MAP,
    ControlCoverageStatus,
    assess_control_coverage,
    get_required_events,
)


# Helper for creating test lineage entries
@dataclass(frozen=True)
class FakeEntry:
    """Mock LineageEntry for testing."""

    artefact_path: str
    artefact_kind: str
    content_hash: str = ""


def _make_lineage_entry(artefact_path: str, artefact_kind: str = "lineage-entry") -> FakeEntry:
    """Create a mock lineage entry."""
    return FakeEntry(
        artefact_path=artefact_path,
        artefact_kind=artefact_kind,
        content_hash=f"sha256:{artefact_path}",
    )


# Test get_required_events
class TestGetRequiredEvents:
    def test_returns_required_behavior(self) -> None:
        assert get_required_events("eu-ai-act-art-12") == {"task-dispatch"}
        assert get_required_events("soc2-cc6-1") == {"access-control"}
        assert get_required_events("soc2-cc8-1") == {"change-management"}

    def test_returns_empty_for_unknown_policy(self) -> None:
        assert get_required_events("unknown-policy") == set()


# Test assess_control_coverage
class TestAssessControlCoverage:
    def test_evidenced_controls(self) -> None:
        """Test controls that have matching events in the chain."""
        entries = [
            _make_lineage_entry("task/some-task-id"),
            _make_lineage_entry("auth/login-event"),
            _make_lineage_entry("config/some-change"),
        ]
        results = assess_control_coverage(entries)

        # Find results for each policy
        results_by_policy = {r.policy_id: r for r in results}

        # EU AI Act Art 12 should be evidenced (has task/ entries)
        assert results_by_policy["eu-ai-act-art-12"].status == ControlCoverageStatus.EVIDENCED
        assert "task-dispatch" in results_by_policy["eu-ai-act-art-12"].evidence_summary

        # SOC2 CC6.1 should be evidenced (has auth/ entries)
        assert results_by_policy["soc2-cc6-1"].status == ControlCoverageStatus.EVIDENCED
        assert "access-control" in results_by_policy["soc2-cc6-1"].evidence_summary

        # SOC2 CC8.1 should be evidenced (has config/ entries)
        assert results_by_policy["soc2-cc8-1"].status == ControlCoverageStatus.EVIDENCED
        assert "change-management" in results_by_policy["soc2-cc8-1"].evidence_summary

    def test_evidenced_control_names_the_entries_that_evidenced_it(self) -> None:
        """An evidenced control names the chain events that satisfied it."""
        entries = [
            _make_lineage_entry("task/some-task-id"),
            _make_lineage_entry("auth/login-event"),
            _make_lineage_entry("docs/unrelated"),
        ]
        results_by_policy = {r.policy_id: r for r in assess_control_coverage(entries)}

        assert results_by_policy["eu-ai-act-art-12"].evidence_refs == ("sha256:task/some-task-id",)
        assert results_by_policy["soc2-cc6-1"].evidence_refs == ("sha256:auth/login-event",)
        # A control with no evidence names none.
        assert results_by_policy["eu-ai-act-art-73"].evidence_refs == ()

    def test_partially_evidenced_wrong_artifact_kind(self) -> None:
        """Test controls where artifact kind is present but wrong behaviour."""
        entries = [
            _make_lineage_entry("report/some-report", "report"),  # Wrong artefact kind
            _make_lineage_entry("dataset/some-data", "dataset"),  # Wrong artefact kind
        ]
        results = assess_control_coverage(entries)
        results_by_policy = {r.policy_id: r for r in results}

        # Should be partially evidenced for all since wrong artefact kind
        assert results_by_policy["eu-ai-act-art-12"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert "lineage-entry" in results_by_policy["eu-ai-act-art-12"].evidence_summary
        assert "task-dispatch" in results_by_policy["eu-ai-act-art-12"].missing_inputs

        assert results_by_policy["soc2-cc6-1"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert "audit-event" in results_by_policy["soc2-cc6-1"].evidence_summary
        assert "access-control" in results_by_policy["soc2-cc6-1"].missing_inputs

    def test_partially_evidenced_correct_artifact_wrong_behaviour(self) -> None:
        """Test controls with correct artifact kind but wrong behaviour."""
        entries = [
            _make_lineage_entry("lineage/wrong-task", "lineage-entry"),  # Wrong path prefix
            _make_lineage_entry("audit/wrong-auth", "audit-event"),  # Wrong path prefix
            _make_lineage_entry("lineage/wrong-config", "lineage-entry"),  # Wrong path prefix
        ]
        results = assess_control_coverage(entries)
        results_by_policy = {r.policy_id: r for r in results}

        # Should be partially evidenced since artefact kind present but wrong behaviour
        assert results_by_policy["eu-ai-act-art-12"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert results_by_policy["eu-ai-act-art-12"].missing_inputs == ["task-dispatch"]

        assert results_by_policy["soc2-cc6-1"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert results_by_policy["soc2-cc6-1"].missing_inputs == ["access-control"]

        assert results_by_policy["soc2-cc8-1"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert results_by_policy["soc2-cc8-1"].missing_inputs == ["change-management"]

    def test_not_evidenceable_unknown_policy(self) -> None:
        """Test that unknown policies are not evidenceable."""
        # Add a fake policy to CONTROL_EVENT_MAP for this test
        original_map = dict(CONTROL_EVENT_MAP)
        try:
            CONTROL_EVENT_MAP["fake-policy"] = {
                "required_artefact_kind": "fake-kind",
                "required_agent_behavior": "fake-behavior",
                "partial_evidence_hint": "fake hint",
            }

            entries = [_make_lineage_entry("task/some-task")]
            results = assess_control_coverage(entries)
            results_by_policy = {r.policy_id: r for r in results}

            # Unknown policy should be not evidenceable
            assert results_by_policy["fake-policy"].status == ControlCoverageStatus.NOT_EVIDENCEABLE
            assert "No required behaviour mapped" in results_by_policy["fake-policy"].evidence_summary
        finally:
            # Restore original map
            CONTROL_EVENT_MAP.clear()
            CONTROL_EVENT_MAP.update(original_map)

    def test_empty_entries(self) -> None:
        """Test with no entries in the chain."""
        results = assess_control_coverage([])
        results_by_policy = {r.policy_id: r for r in results}

        # All should be partially evidenced (no artefact kind observed)
        assert results_by_policy["eu-ai-act-art-12"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert "No entries of kind 'lineage-entry'" in results_by_policy["eu-ai-act-art-12"].evidence_summary

        assert results_by_policy["soc2-cc6-1"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert "No entries of kind 'audit-event'" in results_by_policy["soc2-cc6-1"].evidence_summary

        assert results_by_policy["soc2-cc8-1"].status == ControlCoverageStatus.PARTIALLY_EVIDENCED
        assert "No entries of kind 'lineage-entry'" in results_by_policy["soc2-cc8-1"].evidence_summary

    def test_case_insensitive_matching(self) -> None:
        """Test that path matching is case insensitive."""
        entries = [
            _make_lineage_entry("TASK/Some-Task"),  # Uppercase
            _make_lineage_entry("AUTH/Login-Event"),  # Uppercase
        ]
        results = assess_control_coverage(entries)
        results_by_policy = {r.policy_id: r for r in results}

        # Should still be evidenced
        assert results_by_policy["eu-ai-act-art-12"].status == ControlCoverageStatus.EVIDENCED
        assert results_by_policy["soc2-cc6-1"].status == ControlCoverageStatus.EVIDENCED


# Test _behaviour_from_entry helper (indirectly via assess_control_coverage)
class TestBehaviourFromEntry:
    def test_task_dispatch_matching(self) -> None:
        entry = _make_lineage_entry("task/something")
        # This is tested indirectly through assess_control_coverage
        assert assess_control_coverage([entry])[0].status == ControlCoverageStatus.EVIDENCED

    def test_access_control_matching(self) -> None:
        entry = _make_lineage_entry("auth/something")
        results = assess_control_coverage([entry])
        # Find SOC2 CC6.1 result
        soc2_result = next(r for r in results if r.policy_id == "soc2-cc6-1")
        assert soc2_result.status == ControlCoverageStatus.EVIDENCED

    def test_change_management_matching(self) -> None:
        entry = _make_lineage_entry("config/something")
        results = assess_control_coverage([entry])
        # Find SOC2 CC8.1 result
        soc2_result = next(r for r in results if r.policy_id == "soc2-cc8-1")
        assert soc2_result.status == ControlCoverageStatus.EVIDENCED

    def test_non_matching_paths(self) -> None:
        entry = _make_lineage_entry("report/something")
        results = assess_control_coverage([entry])
        # All should be partially evidenced (wrong behaviour)
        for r in results:
            assert r.status == ControlCoverageStatus.PARTIALLY_EVIDENCED
