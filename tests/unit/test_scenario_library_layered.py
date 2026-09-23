"""Tests for layered scenario library loading."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.planning.scenario_library import (
    load_layered_scenario_library,
)


def test_layered_library_prefers_workspace_over_packaged(tmp_path: Path) -> None:
    """Workspace scenarios should shadow packaged scenarios of the same ID."""
    # Setup directories
    workspace_dir = tmp_path / "workspace" / ".bernstein" / "scenarios"
    packaged_dir = tmp_path / "packaged" / ".bernstein" / "scenarios"
    workspace_dir.mkdir(parents=True)
    packaged_dir.mkdir(parents=True)

    # Create packaged scenario
    (packaged_dir / "shadow_me.yaml").write_text(
        """\
id: shadow_me
name: Packaged Scenario
description: From packaged source
tasks:
  - title: Packaged task
    description: Do packaged thing
    role: backend
""",
        encoding="utf-8",
    )

    # Create workspace scenario with same ID
    (workspace_dir / "shadow_me.yaml").write_text(
        """\
id: shadow_me
name: Workspace Scenario
description: From workspace source
tasks:
  - title: Workspace task
    description: Do workspace thing
    role: frontend
""",
        encoding="utf-8",
    )

    # Load layered library
    library = load_layered_scenario_library(
        workspace_root=tmp_path / "workspace",
        packaged_root=tmp_path / "packaged",
    )

    # Should get the workspace version
    scenario = library.get("shadow_me")
    assert scenario is not None
    assert scenario.name == "Workspace Scenario"
    assert scenario.description == "From workspace source"
    assert len(scenario.tasks) == 1
    assert scenario.tasks[0].title == "Workspace task"
    assert scenario.tasks[0].role == "frontend"
    assert scenario.source_root == "workspace"


def test_layered_library_falls_back_to_packaged(tmp_path: Path) -> None:
    """If no workspace scenario exists, should use packaged version."""
    # Setup directories
    workspace_dir = tmp_path / "workspace" / ".bernstein" / "scenarios"
    packaged_dir = tmp_path / "packaged" / ".bernstein" / "scenarios"
    workspace_dir.mkdir(parents=True)
    packaged_dir.mkdir(parents=True)

    # Create only packaged scenario
    (packaged_dir / "only_here.yaml").write_text(
        """\
id: only_here
name: Packaged Only
description: Only in packaged source
tasks:
  - title: Packaged task
    description: Do packaged thing
    role: backend
""",
        encoding="utf-8",
    )

    # Load layered library
    library = load_layered_scenario_library(
        workspace_root=tmp_path / "workspace",
        packaged_root=tmp_path / "packaged",
    )

    # Should get the packaged version
    scenario = library.get("only_here")
    assert scenario is not None
    assert scenario.name == "Packaged Only"
    assert scenario.description == "Only in packaged source"
    assert len(scenario.tasks) == 1
    assert scenario.tasks[0].title == "Packaged task"
    assert scenario.tasks[0].role == "backend"
    assert scenario.source_root == "packaged"


def test_layered_library_source_root_tracking(tmp_path: Path) -> None:
    """Each scenario should track whether it came from workspace or packaged."""
    # Setup directories
    workspace_dir = tmp_path / "workspace" / ".bernstein" / "scenarios"
    packaged_dir = tmp_path / "packaged" / ".bernstein" / "scenarios"
    workspace_dir.mkdir(parents=True)
    packaged_dir.mkdir(parents=True)

    # Create scenarios in both locations with different IDs
    (workspace_dir / "ws_only.yaml").write_text(
        """\
id: ws_only
name: Workspace Only
description: Only in workspace
tasks:
  - title: WS task
    description: Do workspace thing
    role: backend
""",
        encoding="utf-8",
    )

    (packaged_dir / "pkg_only.yaml").write_text(
        """\
id: pkg_only
name: Packaged Only
description: Only in packaged
tasks:
  - title: PKG task
    description: Do packaged thing
    role: frontend
""",
        encoding="utf-8",
    )

    # Load layered library
    library = load_layered_scenario_library(
        workspace_root=tmp_path / "workspace",
        packaged_root=tmp_path / "packaged",
    )

    # Check workspace scenario
    ws_scenario = library.get("ws_only")
    assert ws_scenario is not None
    assert ws_scenario.name == "Workspace Only"
    assert ws_scenario.source_root == "workspace"

    # Check packaged scenario
    pkg_scenario = library.get("pkg_only")
    assert pkg_scenario is not None
    assert pkg_scenario.name == "Packaged Only"
    assert pkg_scenario.source_root == "packaged"


def test_layered_library_lists_correct_roots(tmp_path: Path) -> None:
    """The library should list scenarios with correct source roots."""
    # Setup directories
    workspace_dir = tmp_path / "workspace" / ".bernstein" / "scenarios"
    packaged_dir = tmp_path / "packaged" / ".bernstein" / "scenarios"
    workspace_dir.mkdir(parents=True)
    packaged_dir.mkdir(parents=True)

    # Create workspace scenario that shadows packaged one
    (packaged_dir / "shadow_me.yaml").write_text(
        """\
id: shadow_me
name: Packaged Version
description: From packaged
tasks:
  - title: PKG task
    description: Do packaged thing
    role: backend
""",
        encoding="utf-8",
    )

    (workspace_dir / "shadow_me.yaml").write_text(
        """\
id: shadow_me
name: Workspace Version
description: From workspace
tasks:
  - title: WS task
    description: Do workspace thing
    role: frontend
""",
        encoding="utf-8",
    )

    # Create pure packaged scenario
    (packaged_dir / "pkg_only.yaml").write_text(
        """\
id: pkg_only
name: Packaged Only
description: Only packaged
tasks:
  - title: PKG only task
    description: Do packaged only thing
    role: backend
""",
        encoding="utf-8",
    )

    # Load layered library
    library = load_layered_scenario_library(
        workspace_root=tmp_path / "workspace",
        packaged_root=tmp_path / "packaged",
    )

    # Check that shadowed scenario shows workspace root
    shadow_scenario = library.get("shadow_me")
    assert shadow_scenario is not None
    assert shadow_scenario.source_root == "workspace"
    assert shadow_scenario.name == "Workspace Version"

    # Check that packaged-only scenario shows packaged root
    pkg_scenario = library.get("pkg_only")
    assert pkg_scenario is not None
    assert pkg_scenario.source_root == "packaged"
    assert pkg_scenario.name == "Packaged Only"
