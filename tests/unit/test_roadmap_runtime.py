from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.roadmap_runtime import emit_roadmap_wave

if TYPE_CHECKING:
    from pathlib import Path


def _seed_scenario(root: Path) -> None:
    scenarios_dir = root / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    (scenarios_dir / "scenario.yaml").write_text(
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
    backlog_files = list((tmp_path / ".sdd" / "backlog" / "open").glob("*.md"))
    assert len(backlog_files) == 2


def test_emit_roadmap_wave_respects_open_ticket_cap(tmp_path: Path) -> None:
    backlog_open = tmp_path / ".sdd" / "backlog" / "open"
    backlog_open.mkdir(parents=True, exist_ok=True)
    (backlog_open / "existing.md").write_text("# Existing\n", encoding="utf-8")
    _seed_scenario(tmp_path)
    _seed_roadmap(tmp_path)

    emitted = emit_roadmap_wave(tmp_path, max_open_tickets=1)
    assert emitted == []
