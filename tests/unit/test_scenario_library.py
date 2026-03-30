from __future__ import annotations

from pathlib import Path

from bernstein.core.scenario_library import load_scenario_library


def test_load_scenario_library_reads_valid_yaml(tmp_path: Path) -> None:
    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True)
    (scenarios_dir / "example.yaml").write_text(
        """id: example
name: Example Scenario
description: Sample
tasks:
  - title: First task
    description: Do something
    role: backend
""",
        encoding="utf-8",
    )

    library = load_scenario_library(scenarios_dir)
    scenario = library.get("example")
    assert scenario is not None
    assert scenario.name == "Example Scenario"
    assert len(scenario.tasks) == 1
    assert scenario.tasks[0].title == "First task"


def test_load_scenario_library_ignores_invalid_files(tmp_path: Path) -> None:
    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True)
    (scenarios_dir / "broken.yaml").write_text("not: [valid", encoding="utf-8")

    library = load_scenario_library(scenarios_dir)
    assert library.scenarios == {}
