from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.scenario_library import load_scenario_library

if TYPE_CHECKING:
    from pathlib import Path


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


def test_load_scenario_library_parses_task_dependencies(tmp_path: Path) -> None:
    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True)
    (scenarios_dir / "sequenced.yaml").write_text(
        """id: sequenced
name: Sequenced Scenario
description: Ordered tasks
tasks:
  - id: step-1
    title: First step
    description: Prepare
    role: devops
  - id: step-2
    depends_on: [step-1]
    title: Second step
    description: Execute
    role: qa
""",
        encoding="utf-8",
    )

    scenario = load_scenario_library(scenarios_dir).get("sequenced")
    assert scenario is not None
    assert [t.id for t in scenario.tasks] == ["step-1", "step-2"]
    assert scenario.tasks[1].depends_on == ("step-1",)


def test_load_scenario_library_rejects_invalid_or_cyclic_task_dependencies(tmp_path: Path) -> None:
    scenarios_dir = tmp_path / ".bernstein" / "scenarios"
    scenarios_dir.mkdir(parents=True)
    (scenarios_dir / "unknown_dep.yaml").write_text(
        """id: unknown-dep
name: Unknown Dep
description: Bad ref
tasks:
  - id: a
    title: A
    depends_on: [missing]
""",
        encoding="utf-8",
    )
    (scenarios_dir / "cycle.yaml").write_text(
        """id: cycle
name: Cycle
description: Cyclic deps
tasks:
  - id: a
    title: A
    depends_on: [b]
  - id: b
    title: B
    depends_on: [a]
""",
        encoding="utf-8",
    )
    (scenarios_dir / "dup_id.yaml").write_text(
        """id: dup-id
name: Duplicate ID
description: Duplicate task id
tasks:
  - id: a
    title: A1
  - id: a
    title: A2
""",
        encoding="utf-8",
    )

    library = load_scenario_library(scenarios_dir)
    assert library.scenarios == {}
