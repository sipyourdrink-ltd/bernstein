"""Regression tests for AutoSpawnGuard (recursion/dedupe/cap for meta-tasks).

Covers the production incident in
work/agent-reports/2026-07-02-run9-attempt9-audit.md: unbounded
"Upgrade: ..." duplicates and recursive "Watchdog triage of watchdog
triage" tasks with no depth/dedupe/cap guard.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bernstein.core.tasks.auto_spawn_guard import AutoSpawnGuard, compute_ancestry_depth, meta_task_kind


def test_allowed_spawn_passes_all_guards_and_persists_count(tmp_path: Path) -> None:
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=3)

    decision = guard.evaluate(
        kind="upgrade_proposal",
        title="Upgrade: Improve task success rate",
        source_title=None,
        existing_open_titles=[],
    )

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.ancestry_depth == 1
    assert decision.current_count == 1
    assert decision.cap == 3

    state_path = tmp_path / ".sdd" / "runtime" / "auto_spawn_guard.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["count"] == 1


def test_depth_refusal_blocks_meta_task_about_a_meta_task(tmp_path: Path) -> None:
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=10)

    # Source task is itself a watchdog-triage meta-task -> would be depth 2.
    decision = guard.evaluate(
        kind="watchdog_triage",
        title="Watchdog triage: Heartbeat stale for task Watchdog triage: Heartbeat stale for task X",
        source_title="Watchdog triage: Heartbeat stale for task X",
        existing_open_titles=[],
    )

    assert decision.allowed is False
    assert decision.reason == "depth"
    assert decision.ancestry_depth == 2

    # Refused spawns must not consume the cap.
    state_path = tmp_path / ".sdd" / "runtime" / "auto_spawn_guard.json"
    assert not state_path.exists()


def test_compute_ancestry_depth_baseline_and_nested() -> None:
    assert compute_ancestry_depth(None) == 1
    assert compute_ancestry_depth("Fix the login bug") == 1
    assert compute_ancestry_depth("Upgrade: Improve task success rate") == 2
    assert compute_ancestry_depth("Watchdog triage: Heartbeat stale for task X") == 2


def test_dedupe_refusal_blocks_duplicate_title(tmp_path: Path) -> None:
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=10)

    decision = guard.evaluate(
        kind="upgrade_proposal",
        title="Upgrade: Improve task success rate",
        source_title=None,
        existing_open_titles=["Upgrade: Improve task success rate"],
    )

    assert decision.allowed is False
    assert decision.reason == "dedupe"


def test_dedupe_refusal_matches_close_enough_titles(tmp_path: Path) -> None:
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=10)

    decision = guard.evaluate(
        kind="upgrade_proposal",
        title="Upgrade: Improve task success rate  ",
        source_title=None,
        existing_open_titles=["upgrade: improve task success rate (abc123)"],
    )

    assert decision.allowed is False
    assert decision.reason == "dedupe"


def test_cap_refusal_blocks_once_limit_reached(tmp_path: Path) -> None:
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=2)

    first = guard.evaluate(kind="k", title="Upgrade: A", source_title=None, existing_open_titles=[])
    second = guard.evaluate(kind="k", title="Upgrade: B", source_title=None, existing_open_titles=[])
    third = guard.evaluate(kind="k", title="Upgrade: C", source_title=None, existing_open_titles=[])

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.reason == "cap"
    assert third.current_count == 2


def test_cap_state_is_shared_across_guard_instances(tmp_path: Path) -> None:
    """Two call sites (e.g. evolution loop + watchdog) sharing one workdir
    must share the same cap counter."""
    guard_a = AutoSpawnGuard(tmp_path, max_auto_spawns_per_run=1)
    guard_b = AutoSpawnGuard(tmp_path, max_auto_spawns_per_run=1)

    first = guard_a.evaluate(kind="a", title="Upgrade: A", source_title=None, existing_open_titles=[])
    second = guard_b.evaluate(kind="b", title="Watchdog triage: B", source_title=None, existing_open_titles=[])

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "cap"


def test_meta_task_kind_matches_known_prefixes_only() -> None:
    assert meta_task_kind("Upgrade: Improve task success rate") == "Upgrade:"
    assert meta_task_kind("Watchdog triage: Heartbeat stale for task X") == "Watchdog triage:"
    assert meta_task_kind("Implement hello subcommand in cli.py") is None
    assert meta_task_kind(None) is None
    assert meta_task_kind("") is None


def test_dedupe_does_not_collide_distinct_titles_sharing_a_prefix(tmp_path: Path) -> None:
    """Regression (2026-07-02, fix/claim-conflict-churn): an earlier version
    of the dedupe rule treated ANY two titles sharing a known meta-task
    prefix (e.g. both "Upgrade: ...") as automatic duplicates, regardless of
    content. That was too coarse -- it silently rejected genuinely distinct,
    independently-generated proposals from the same evolution cycle, e.g.
    "Upgrade: Proposal One" and "Upgrade: Proposal Two", which must both be
    allowed to spawn (see tests/unit/test_orchestrator.py::
    TestRunEvolutionCycle::test_happy_path_creates_http_task_per_proposal).
    Dedupe is now purely content-based (normalized-title exact/containment
    match), so two proposals with unrelated remainders must NOT collide even
    though they share the "Upgrade:" category.
    """
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=10)

    decision = guard.evaluate(
        kind="upgrade_proposal",
        title="Upgrade: Improve token budget accounting",
        source_title=None,
        existing_open_titles=["Upgrade: Improve task success rate"],
    )

    assert decision.allowed is True
    assert decision.reason == "allowed"


def test_dedupe_still_catches_true_resurrection_duplicates(tmp_path: Path) -> None:
    """Control: a true resurrection (identical title, or identical modulo a
    trailing id-like suffix) must still be refused -- the content-based
    dedupe key is stricter than the old prefix-class rule, not a no-op."""
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=10)

    decision = guard.evaluate(
        kind="upgrade_proposal",
        title="Upgrade: Improve task success rate",
        source_title=None,
        existing_open_titles=["Upgrade: Improve task success rate (abc123)"],
    )

    assert decision.allowed is False
    assert decision.reason == "dedupe"


def test_dedupe_check_logs_key_verdict_and_reason_for_every_candidate(tmp_path: Path, caplog) -> None:
    """Every candidate/existing-title comparison must be logged with the
    dedupe key and verdict, not just the final allow/refuse decision."""
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=10)

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.auto_spawn_guard"):
        decision = guard.evaluate(
            kind="upgrade_proposal",
            title="Upgrade: Improve task success rate",
            source_title=None,
            existing_open_titles=["Upgrade: Proposal One", "Upgrade: Improve task success rate"],
        )

    assert decision.allowed is False
    assert decision.reason == "dedupe"
    check_lines = [r.getMessage() for r in caplog.records if "auto_spawn_dedupe_check" in r.getMessage()]
    assert any("existing='Upgrade: Proposal One'" in line and "match=False" in line for line in check_lines)
    assert any("existing='Upgrade: Improve task success rate'" in line and "match=True" in line for line in check_lines)


def test_evaluate_logs_info_decision_line_for_allowed_spawn(tmp_path: Path, caplog) -> None:
    """Every decision -- including an ALLOWED spawn -- must emit an INFO-level
    line carrying reason, dedupe key, and ancestry depth (logging IS the
    debugging interface)."""
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=3)

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.auto_spawn_guard"):
        decision = guard.evaluate(
            kind="upgrade_proposal",
            title="Upgrade: Improve task success rate",
            source_title=None,
            existing_open_titles=[],
        )

    assert decision.allowed is True
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    decision_lines = [r.getMessage() for r in info_records if "auto_spawn_decision" in r.getMessage()]
    assert decision_lines, f"no auto_spawn_decision INFO line found in {[r.getMessage() for r in info_records]}"
    assert any("allowed=True" in line and "reason=allowed" in line for line in decision_lines)
    assert any("dedupe_key='upgrade: improve task success rate'" in line for line in decision_lines)
    assert any("ancestry_depth=1" in line for line in decision_lines)


def test_evaluate_logs_info_decision_line_for_refused_spawn(tmp_path: Path, caplog) -> None:
    """A refused (depth) decision must ALSO emit the uniform INFO decision
    line, not just the WARNING alert -- the audit-3 evidence flagged that
    "no auto-spawn/ancestry depth log lines" were found for the run that
    produced the junk tasks, so suppressed decisions must be just as
    visible as allowed ones."""
    guard = AutoSpawnGuard(tmp_path, max_ancestry_depth=1, max_auto_spawns_per_run=10)

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.auto_spawn_guard"):
        decision = guard.evaluate(
            kind="retry:Upgrade",
            title="Upgrade: Improve task success rate",
            source_title="Upgrade: Improve task success rate",
            existing_open_titles=[],
        )

    assert decision.allowed is False
    assert decision.reason == "depth"
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    decision_lines = [r.getMessage() for r in info_records if "auto_spawn_decision" in r.getMessage()]
    assert decision_lines, f"no auto_spawn_decision INFO line found in {[r.getMessage() for r in info_records]}"
    assert any(
        "allowed=False" in line and "reason=depth" in line and "ancestry_depth=2" in line for line in decision_lines
    )
