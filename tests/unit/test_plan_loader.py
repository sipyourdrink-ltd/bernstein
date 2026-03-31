"""Tests for YAML plan loader (plan_loader.py)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from bernstein.core.plan_loader import PlanLoadError, load_plan_from_yaml

if TYPE_CHECKING:
    from bernstein.core.models import Task


def test_load_plan_valid(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_content = {
        "name": "Test Plan",
        "stages": [
            {
                "name": "Infrastructure",
                "steps": [
                    {"goal": "Setup DB", "role": "backend"},
                    {"goal": "Setup Cache", "role": "backend"},
                ],
            },
            {
                "name": "App",
                "depends_on": ["Infrastructure"],
                "steps": [
                    {"goal": "API", "role": "backend"},
                ],
            },
        ],
    }
    plan_file.write_text(yaml.dump(plan_content))

    tasks = load_plan_from_yaml(plan_file)
    assert len(tasks) == 3
    
    # Task titles
    titles = [t.title for t in tasks]
    assert "Setup DB" in titles
    assert "Setup Cache" in titles
    assert "API" in titles

    # Dependencies
    api_task = next(t for t in tasks if t.title == "API")
    assert "Setup DB" in api_task.depends_on
    assert "Setup Cache" in api_task.depends_on


def test_load_plan_missing_file() -> None:
    with pytest.raises(PlanLoadError, match="Plan file not found"):
        load_plan_from_yaml(Path("nonexistent.yaml"))


def test_load_plan_invalid_yaml(tmp_path: Path) -> None:
    plan_file = tmp_path / "invalid.yaml"
    plan_file.write_text("invalid: yaml: [")
    with pytest.raises(PlanLoadError, match="Failed to parse YAML plan"):
        load_plan_from_yaml(plan_file)


def test_load_plan_missing_stages(tmp_path: Path) -> None:
    plan_file = tmp_path / "no_stages.yaml"
    plan_file.write_text(yaml.dump({"name": "No Stages"}))
    with pytest.raises(PlanLoadError, match="Plan file must contain a 'stages' list"):
        load_plan_from_yaml(plan_file)


def test_load_plan_stage_missing_name(tmp_path: Path) -> None:
    plan_file = tmp_path / "no_name.yaml"
    plan_content = {
        "stages": [
            {"steps": [{"goal": "Step"}]}
        ]
    }
    plan_file.write_text(yaml.dump(plan_content))
    with pytest.raises(PlanLoadError, match="Stage 0 is missing a name"):
        load_plan_from_yaml(plan_file)
