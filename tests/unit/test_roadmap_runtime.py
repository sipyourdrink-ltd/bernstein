from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from bernstein.core.roadmap_runtime import (
    RoadmapWaveOutcome,
    emit_roadmap_wave,
    emit_roadmap_wave_outcome,
)

import bernstein.core.planning.roadmap_runtime as roadmap_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_scenario_skip_warning_state() -> None:
    roadmap_runtime._reset_scenario_skip_warning_state_for_tests()


def _seed_scenario(root: Path, *, suffix: str = ".yaml") -> None:
    scenarios_dir = root / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    (scenarios_dir / f"scenario{suffix}").write_text(
        """id: scenario-a
name: Scenario A
description: Demo
tasks:
  - title: Task one
    description: first
  - title: Task two
    description: second
""",
        encoding="utf-8",
    )


def _seed_roadmap(root: Path) -> None:
    roadmaps_dir = root / ".sdd" / "roadmaps" / "open"
    roadmaps_dir.mkdir(parents=True, exist_ok=True)
    (roadmaps_dir / "roadmap.yaml").write_text(
        """id: rm1
title: Roadmap One
wave_size: 1
scenarios:
  - scenario-a
""",
        encoding="utf-8",
    )


def test_emit_roadmap_wave_emits_bounded_batch(tmp_path: Path) -> None:
    (tmp_path / ".sdd" / "backlog" / "open").mkdir(parents=True, exist_ok=True)
    _seed_scenario(tmp_path)
    _seed_roadmap(tmp_path)

    first = emit_roadmap_wave(tmp_path, max_open_tickets=10)
    second = emit_roadmap_wave(tmp_path, max_open_tickets=10)

    assert len(first) == 1
    assert len(second) == 1
    backlog_files = list((tmp_path / ".sdd" / "backlog" / "open").glob("*.yaml"))
    assert len(backlog_files) == 2


def test_emit_roadmap_wave_respects_open_ticket_cap(tmp_path: Path) -> None:
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    backlog_open.mkdir(parents=True, exist_ok=True)
    (backlog_open / "existing.yaml").write_text("---\nid: existing\ntitle: Existing\n---\n", encoding="utf-8")
    _seed_scenario(tmp_path)
    _seed_roadmap(tmp_path)

    emitted = emit_roadmap_wave(tmp_path, max_open_tickets=1)
    assert emitted == []


# --- The wave says why it emitted nothing (issue #5573) --------------------


def _seed_backlog(root: Path) -> None:
    (root / ".sdd" / "backlog" / "open").mkdir(parents=True, exist_ok=True)


def test_scenarios_without_a_roadmap_are_reported_not_swallowed(tmp_path: Path) -> None:
    """The fresh-workspace case: scenarios exist, nothing sequences them.

    This is the defect. A workspace with a populated ``.bernstein/scenarios``
    and no ``.sdd/roadmaps/open`` used to be indistinguishable from one with
    no scenarios at all, because the roadmap guard returned before the
    library was ever loaded.
    """
    _seed_backlog(tmp_path)
    _seed_scenario(tmp_path)

    outcome = emit_roadmap_wave_outcome(tmp_path)

    assert outcome.emitted == ()
    assert outcome.reason == "roadmap-dir-missing"
    assert outcome.scenarios_found == 1, "the library must be read before the roadmap guard"
    assert "roadmaps" in outcome.detail and "skipped" in outcome.detail
    assert not (tmp_path / ".sdd" / "runtime" / "roadmaps").exists()


def test_an_empty_library_is_distinguishable_from_an_unreachable_one(tmp_path: Path) -> None:
    """Zero scenarios and one unreachable scenario are different facts."""
    _seed_backlog(tmp_path)

    empty = emit_roadmap_wave_outcome(tmp_path)
    _seed_scenario(tmp_path)
    unreachable = emit_roadmap_wave_outcome(tmp_path)

    assert (empty.reason, empty.scenarios_found) == ("roadmap-dir-missing", 0)
    assert (unreachable.reason, unreachable.scenarios_found) == ("roadmap-dir-missing", 1)


@pytest.mark.parametrize("suffix", [".yaml", ".yml"])
def test_a_missing_backlog_directory_uses_a_candidate_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    """No capacity to write a ticket is still a stated reason, not silence."""
    _seed_scenario(tmp_path, suffix=suffix)
    _seed_roadmap(tmp_path)

    def fail_load(_root: Path) -> object:
        raise AssertionError("capacity guards must not parse the scenario library")

    monkeypatch.setattr(roadmap_runtime, "load_scenario_library", fail_load)
    outcome = emit_roadmap_wave_outcome(tmp_path)

    assert outcome.reason == "backlog-missing"
    assert outcome.scenarios_found == 1
    assert str(tmp_path / ".sdd" / "backlog" / "open") in outcome.detail
    assert not (tmp_path / ".sdd" / "runtime" / "roadmaps").exists()


def test_the_ticket_ceiling_uses_a_candidate_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    backlog_open.mkdir(parents=True, exist_ok=True)
    (backlog_open / "existing.yaml").write_text("---\nid: existing\n---\n", encoding="utf-8")
    _seed_scenario(tmp_path, suffix=".yml")
    _seed_roadmap(tmp_path)

    def fail_load(_root: Path) -> object:
        raise AssertionError("ticket ceiling must not parse the scenario library")

    monkeypatch.setattr(roadmap_runtime, "load_scenario_library", fail_load)
    outcome = emit_roadmap_wave_outcome(tmp_path, max_open_tickets=1)

    assert outcome.reason == "ticket-ceiling"
    assert outcome.scenarios_found == 1
    assert "1" in outcome.detail
    assert not (tmp_path / ".sdd" / "runtime" / "roadmaps").exists()


def test_an_empty_roadmaps_directory_reports_no_roadmap_without_runtime_state(tmp_path: Path) -> None:
    _seed_backlog(tmp_path)
    _seed_scenario(tmp_path)
    (tmp_path / ".sdd" / "roadmaps" / "open").mkdir(parents=True, exist_ok=True)

    outcome = emit_roadmap_wave_outcome(tmp_path)

    assert outcome.reason == "no-roadmap-files"
    assert outcome.scenarios_found == 1
    assert "contains no roadmap YAML definitions" in outcome.detail
    assert outcome.emitted == ()
    assert not (tmp_path / ".sdd" / "runtime" / "roadmaps").exists()


def test_a_roadmap_with_no_library_reports_no_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roadmaps present, library empty: the roadmaps have nothing to sequence."""
    _seed_backlog(tmp_path)
    _seed_roadmap(tmp_path)
    roadmaps_dir = tmp_path / ".sdd" / "roadmaps" / "open"
    path_type = type(tmp_path)
    original_glob = path_type.glob

    def guarded_glob(path: Path, pattern: str) -> Iterator[Path]:
        if path == roadmaps_dir:
            raise AssertionError("empty scenario libraries must return before roadmap enumeration")
        return original_glob(path, pattern)

    monkeypatch.setattr(path_type, "glob", guarded_glob)
    outcome = emit_roadmap_wave_outcome(tmp_path)

    assert outcome.reason == "no-scenarios"
    assert outcome.scenarios_found == 0


def test_invalid_scenario_yaml_is_still_excluded_after_capacity_guards(tmp_path: Path) -> None:
    _seed_backlog(tmp_path)
    _seed_roadmap(tmp_path)
    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True)
    (scenarios_dir / "invalid.yaml").write_text("tasks: [", encoding="utf-8")

    outcome = emit_roadmap_wave_outcome(tmp_path)

    assert outcome.reason == "no-scenarios"
    assert outcome.scenarios_found == 0


def test_a_roadmap_naming_an_absent_scenario_reports_no_eligible_scenarios(tmp_path: Path) -> None:
    """Both directories populated, but the ids do not meet in the middle."""
    _seed_backlog(tmp_path)
    _seed_scenario(tmp_path)
    roadmaps_dir = tmp_path / ".sdd" / "roadmaps" / "open"
    roadmaps_dir.mkdir(parents=True, exist_ok=True)
    (roadmaps_dir / "roadmap.yaml").write_text(
        "id: rm1\ntitle: Roadmap One\nwave_size: 1\nscenarios:\n  - scenario-absent\n",
        encoding="utf-8",
    )

    outcome = emit_roadmap_wave_outcome(tmp_path)

    assert outcome.reason == "no-eligible-scenarios"
    assert outcome.scenarios_found == 1
    runtime_dir = tmp_path / ".sdd" / "runtime" / "roadmaps"
    assert not runtime_dir.exists()

    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    (scenarios_dir / "scenario-absent.yaml").write_text(
        """id: scenario-absent
name: Scenario Absent
description: Added after the first wave
tasks:
  - title: Recovered task
    description: now available
""",
        encoding="utf-8",
    )

    recovered = emit_roadmap_wave_outcome(tmp_path)

    assert recovered.reason == "emitted"
    assert len(recovered.emitted) == 1
    assert recovered.emitted[0].exists()
    assert (runtime_dir / "rm1.json").exists()


def test_a_successful_wave_reports_emitted_with_the_paths(tmp_path: Path) -> None:
    _seed_backlog(tmp_path)
    _seed_scenario(tmp_path)
    _seed_roadmap(tmp_path)

    outcome = emit_roadmap_wave_outcome(tmp_path)

    assert outcome.reason == "emitted"
    assert len(outcome.emitted) == 1
    assert outcome.emitted[0].exists()
    assert outcome.scenarios_found == 1


def test_every_reason_the_function_can_return_is_in_the_closed_set(tmp_path: Path) -> None:
    """A reason string that drifts out of the set fails at construction."""
    with pytest.raises(ValueError) as excinfo:
        RoadmapWaveOutcome(emitted=(), reason="made-up", scenarios_found=0, detail="")
    assert "made-up" in str(excinfo.value)


def test_the_wrapper_still_returns_a_plain_list_of_paths(tmp_path: Path) -> None:
    """Existing call sites keep the contract they were written against."""
    _seed_backlog(tmp_path)
    _seed_scenario(tmp_path)
    _seed_roadmap(tmp_path)

    emitted = emit_roadmap_wave(tmp_path)

    assert isinstance(emitted, list)
    assert len(emitted) == 1


def test_roadmap_reason_change_warns_without_a_process_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_backlog(tmp_path)
    _seed_scenario(tmp_path)

    with caplog.at_level(logging.WARNING):
        missing = emit_roadmap_wave_outcome(tmp_path)
        assert missing.reason == "roadmap-dir-missing"
        assert roadmap_runtime.report_outcome(tmp_path, missing)

        (tmp_path / ".sdd" / "roadmaps" / "open").mkdir(parents=True)
        empty = emit_roadmap_wave_outcome(tmp_path)
        assert empty.reason == "no-roadmap-files"
        assert roadmap_runtime.report_outcome(tmp_path, empty)

    messages = [record.getMessage() for record in caplog.records if "Found workspace scenarios" in record.getMessage()]
    assert len(messages) == 2
    assert "roadmap-dir-missing" in messages[0]
    assert "does not exist" in messages[0]
    assert "no-roadmap-files" in messages[1]
    assert "contains no roadmap YAML definitions" in messages[1]


def test_scenario_skip_warning_repeats_after_cooldown_and_reason_changes_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [100.0]
    monkeypatch.setattr(roadmap_runtime.time, "monotonic", lambda: now[0])
    first = RoadmapWaveOutcome((), "roadmap-dir-missing", 1, "roadmap directory missing")
    changed = RoadmapWaveOutcome((), "no-roadmap-files", 1, "no roadmap files")

    with caplog.at_level(logging.WARNING):
        assert roadmap_runtime.report_outcome(tmp_path, first)
        now[0] = 399.9
        assert roadmap_runtime.report_outcome(tmp_path, first)
        now[0] = 400.0
        assert roadmap_runtime.report_outcome(tmp_path, first)
        assert roadmap_runtime.report_outcome(tmp_path, changed)

    messages = [record.getMessage() for record in caplog.records if "Found workspace scenarios" in record.getMessage()]
    assert messages == [
        "Found workspace scenarios but skipped roadmap emission: roadmap-dir-missing: roadmap directory missing",
        "Found workspace scenarios but skipped roadmap emission: roadmap-dir-missing: roadmap directory missing",
        "Found workspace scenarios but skipped roadmap emission: no-roadmap-files: no roadmap files",
    ]


def test_report_outcome_does_not_warn_without_scenarios(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = RoadmapWaveOutcome((), "no-scenarios", 0, "nothing to report")

    with caplog.at_level(logging.WARNING):
        assert not roadmap_runtime.report_outcome(tmp_path, outcome)

    assert "Found workspace scenarios" not in caplog.text
