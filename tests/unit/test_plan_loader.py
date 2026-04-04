"""Tests for YAML plan loader (plan_loader.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.core.plan_loader import PlanConfig, PlanLoadError, RepoRef, load_plan, load_plan_from_yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan(tmp_path: Path, data: object) -> Path:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(yaml.dump(data))
    return plan_file


# ---------------------------------------------------------------------------
# load_plan_from_yaml — basic loading
# ---------------------------------------------------------------------------


def test_load_plan_valid(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
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
        },
    )

    tasks = load_plan_from_yaml(plan_file)
    assert len(tasks) == 3

    titles = [t.title for t in tasks]
    assert "Setup DB" in titles
    assert "Setup Cache" in titles
    assert "API" in titles

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
    plan_file = _write_plan(tmp_path, {"name": "No Stages"})
    with pytest.raises(PlanLoadError, match="Plan file must contain a 'stages' list"):
        load_plan_from_yaml(plan_file)


def test_load_plan_stage_missing_name(tmp_path: Path) -> None:
    plan_file = _write_plan(tmp_path, {"stages": [{"steps": [{"goal": "Step"}]}]})
    with pytest.raises(PlanLoadError, match="Stage 0 is missing a name"):
        load_plan_from_yaml(plan_file)


def test_load_plan_not_a_mapping(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("- item1\n- item2\n")
    with pytest.raises(PlanLoadError, match="Plan file must be a YAML mapping"):
        load_plan_from_yaml(plan_file)


# ---------------------------------------------------------------------------
# title vs goal field support
# ---------------------------------------------------------------------------


def test_step_title_field(tmp_path: Path) -> None:
    """Steps should accept 'title' as the primary field."""
    plan_file = _write_plan(
        tmp_path,
        {
            "name": "Title Test",
            "stages": [
                {
                    "name": "S1",
                    "steps": [{"title": "My Step Title", "role": "backend"}],
                }
            ],
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert len(tasks) == 1
    assert tasks[0].title == "My Step Title"


def test_step_goal_field_backward_compat(tmp_path: Path) -> None:
    """Legacy 'goal' field should still work as title."""
    plan_file = _write_plan(
        tmp_path,
        {
            "name": "Goal Test",
            "stages": [
                {
                    "name": "S1",
                    "steps": [{"goal": "My Goal Step", "role": "backend"}],
                }
            ],
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert len(tasks) == 1
    assert tasks[0].title == "My Goal Step"


def test_step_missing_title_and_goal_raises(tmp_path: Path) -> None:
    """A step with neither 'title' nor 'goal' should raise PlanLoadError."""
    plan_file = _write_plan(
        tmp_path,
        {
            "name": "Bad Plan",
            "stages": [
                {
                    "name": "S1",
                    "steps": [{"role": "backend"}],
                }
            ],
        },
    )
    with pytest.raises(PlanLoadError, match="missing a 'title'"):
        load_plan_from_yaml(plan_file)


def test_step_title_takes_precedence_over_goal(tmp_path: Path) -> None:
    """When both 'title' and 'goal' are present, 'title' wins."""
    plan_file = _write_plan(
        tmp_path,
        {
            "name": "Precedence Test",
            "stages": [
                {
                    "name": "S1",
                    "steps": [{"title": "Title Value", "goal": "Goal Value", "role": "backend"}],
                }
            ],
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].title == "Title Value"


# ---------------------------------------------------------------------------
# load_plan — PlanConfig extraction
# ---------------------------------------------------------------------------


def test_load_plan_returns_config(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
            "name": "My Plan",
            "description": "Builds something great",
            "constraints": ["Python 3.12+", "pytest"],
            "context_files": ["docs/spec.md"],
            "cli": "claude",
            "budget": "$10",
            "max_agents": 4,
            "stages": [
                {
                    "name": "S1",
                    "steps": [{"title": "Step A"}],
                }
            ],
        },
    )
    config, tasks = load_plan(plan_file)

    assert isinstance(config, PlanConfig)
    assert config.name == "My Plan"
    assert config.description == "Builds something great"
    assert config.constraints == ["Python 3.12+", "pytest"]
    assert config.context_files == ["docs/spec.md"]
    assert config.cli == "claude"
    assert config.budget == "$10"
    assert config.max_agents == 4
    assert len(tasks) == 1


def test_load_plan_config_defaults(tmp_path: Path) -> None:
    """Plan with no optional fields gives safe defaults on PlanConfig."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [{"name": "S1", "steps": [{"title": "X"}]}],
        },
    )
    config, _ = load_plan(plan_file)
    assert config.name == ""
    assert config.description == ""
    assert config.constraints == []
    assert config.context_files == []
    assert config.cli is None
    assert config.budget is None
    assert config.max_agents is None


# ---------------------------------------------------------------------------
# Step optional fields
# ---------------------------------------------------------------------------


def test_step_description_fallback(tmp_path: Path) -> None:
    """When description is omitted, it falls back to the title."""
    plan_file = _write_plan(
        tmp_path,
        {"stages": [{"name": "S", "steps": [{"title": "Do the thing"}]}]},
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].description == "Do the thing"


def test_step_with_description(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S",
                    "steps": [{"title": "T", "description": "Detailed instructions here"}],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].description == "Detailed instructions here"


def test_step_model_and_effort(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S",
                    "steps": [{"title": "Hard task", "model": "opus", "effort": "max"}],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].model == "opus"
    assert tasks[0].effort == "max"


def test_step_model_effort_default_none(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {"stages": [{"name": "S", "steps": [{"title": "T"}]}]},
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].model is None
    assert tasks[0].effort is None


def test_step_estimated_minutes(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S",
                    "steps": [{"title": "T", "estimated_minutes": 90}],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].estimated_minutes == 90


def test_step_files_maps_to_owned_files(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S",
                    "steps": [{"title": "T", "files": ["src/app.py", "tests/test_app.py"]}],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].owned_files == ["src/app.py", "tests/test_app.py"]


# ---------------------------------------------------------------------------
# Completion signals
# ---------------------------------------------------------------------------


def test_step_completion_signals(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S",
                    "steps": [
                        {
                            "title": "T",
                            "completion_signals": [
                                {"type": "path_exists", "path": "src/app.py"},
                                {"type": "test_passes", "command": "pytest -x"},
                                {"type": "file_contains", "path": "README.md", "contains": "Usage"},
                            ],
                        }
                    ],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    signals = tasks[0].completion_signals
    assert len(signals) == 3
    assert signals[0].type == "path_exists"
    assert signals[0].value == "src/app.py"
    assert signals[1].type == "test_passes"
    assert signals[1].value == "pytest -x"
    assert signals[2].type == "file_contains"
    assert signals[2].value == "README.md"


def test_step_invalid_completion_signal_skipped(tmp_path: Path) -> None:
    """Invalid signal types are skipped with a warning, not a hard failure."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S",
                    "steps": [
                        {
                            "title": "T",
                            "completion_signals": [
                                {"type": "invalid_type", "value": "x"},
                                {"type": "path_exists", "path": "src/app.py"},
                            ],
                        }
                    ],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    # Only the valid signal survives
    assert len(tasks[0].completion_signals) == 1
    assert tasks[0].completion_signals[0].type == "path_exists"


def test_step_empty_signal_value_skipped(tmp_path: Path) -> None:
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S",
                    "steps": [
                        {
                            "title": "T",
                            "completion_signals": [
                                {"type": "path_exists"},  # no path/value
                            ],
                        }
                    ],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].completion_signals == []


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------


def test_empty_stage_generates_no_tasks(tmp_path: Path) -> None:
    """An empty stage (no steps) is allowed and generates no tasks."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {"name": "Empty", "steps": []},
                {"name": "S2", "depends_on": ["Empty"], "steps": [{"title": "T"}]},
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert len(tasks) == 1
    assert tasks[0].depends_on == []  # Empty stage has no tasks to depend on


def test_depends_on_unknown_stage_is_warned(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Depending on a stage that doesn't exist logs a warning but doesn't crash."""
    import logging

    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "S1",
                    "depends_on": ["NonExistent"],
                    "steps": [{"title": "T"}],
                }
            ]
        },
    )
    with caplog.at_level(logging.WARNING, logger="bernstein.core.plan_loader"):
        tasks = load_plan_from_yaml(plan_file)
    assert len(tasks) == 1
    assert tasks[0].depends_on == []
    assert "NonExistent" in caplog.text


def test_multi_stage_dependency_chain(tmp_path: Path) -> None:
    """Three-stage chain: C depends on B which depends on A."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {"name": "A", "steps": [{"title": "A1"}]},
                {"name": "B", "depends_on": ["A"], "steps": [{"title": "B1"}]},
                {"name": "C", "depends_on": ["B"], "steps": [{"title": "C1"}]},
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    task_map = {t.title: t for t in tasks}
    assert task_map["A1"].depends_on == []
    assert task_map["B1"].depends_on == ["A1"]
    assert task_map["C1"].depends_on == ["B1"]


def test_parallel_steps_share_deps(tmp_path: Path) -> None:
    """Two parallel steps in stage B both depend on all steps from stage A."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {"name": "A", "steps": [{"title": "A1"}, {"title": "A2"}]},
                {
                    "name": "B",
                    "depends_on": ["A"],
                    "steps": [{"title": "B1"}, {"title": "B2"}],
                },
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    task_map = {t.title: t for t in tasks}
    assert set(task_map["B1"].depends_on) == {"A1", "A2"}
    assert set(task_map["B2"].depends_on) == {"A1", "A2"}


# ---------------------------------------------------------------------------
# Multi-repo plan support (GH#220)
# ---------------------------------------------------------------------------


def test_load_plan_repos_section(tmp_path: Path) -> None:
    """Top-level repos list is parsed into PlanConfig.repos."""
    plan_file = _write_plan(
        tmp_path,
        {
            "name": "Full Stack",
            "repos": [
                {"path": "../backend", "branch": "feat/user-auth"},
                {"path": "../frontend", "branch": "feat/user-auth", "name": "web"},
                {"path": "../shared-types"},
            ],
            "stages": [{"name": "S", "steps": [{"title": "T"}]}],
        },
    )
    config, _ = load_plan(plan_file)

    assert len(config.repos) == 3
    assert config.repos[0].path == "../backend"
    assert config.repos[0].branch == "feat/user-auth"
    assert config.repos[0].name == "backend"  # auto-derived from path

    assert config.repos[1].path == "../frontend"
    assert config.repos[1].name == "web"  # explicit name wins

    assert config.repos[2].path == "../shared-types"
    assert config.repos[2].branch == "main"  # default branch


def test_load_plan_repos_default_empty(tmp_path: Path) -> None:
    """Plans without a repos section have an empty repos list."""
    plan_file = _write_plan(
        tmp_path,
        {"stages": [{"name": "S", "steps": [{"title": "T"}]}]},
    )
    config, _ = load_plan(plan_file)
    assert config.repos == []


def test_stage_repo_propagates_to_tasks(tmp_path: Path) -> None:
    """Tasks inherit the repo declared on their stage."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "Backend",
                    "repo": "../backend",
                    "steps": [{"title": "Add endpoint"}, {"title": "Write tests"}],
                },
                {
                    "name": "Frontend",
                    "repo": "../frontend",
                    "steps": [{"title": "Update UI"}],
                },
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    task_map = {t.title: t for t in tasks}

    assert task_map["Add endpoint"].repo == "../backend"
    assert task_map["Write tests"].repo == "../backend"
    assert task_map["Update UI"].repo == "../frontend"


def test_step_repo_overrides_stage_repo(tmp_path: Path) -> None:
    """A step-level repo field takes priority over the stage-level repo."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "Mixed",
                    "repo": "../backend",
                    "steps": [
                        {"title": "Backend task"},
                        {"title": "Shared task", "repo": "../shared-types"},
                    ],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    task_map = {t.title: t for t in tasks}

    assert task_map["Backend task"].repo == "../backend"
    assert task_map["Shared task"].repo == "../shared-types"


def test_step_depends_on_repo(tmp_path: Path) -> None:
    """depends_on_repo is parsed from the step dict."""
    plan_file = _write_plan(
        tmp_path,
        {
            "stages": [
                {
                    "name": "App",
                    "steps": [
                        {
                            "title": "Build frontend",
                            "repo": "../frontend",
                            "depends_on_repo": "../shared-types",
                        }
                    ],
                }
            ]
        },
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].repo == "../frontend"
    assert tasks[0].depends_on_repo == "../shared-types"


def test_no_repo_on_stage_gives_none(tmp_path: Path) -> None:
    """Tasks from stages without a repo field have repo=None."""
    plan_file = _write_plan(
        tmp_path,
        {"stages": [{"name": "S", "steps": [{"title": "T"}]}]},
    )
    tasks = load_plan_from_yaml(plan_file)
    assert tasks[0].repo is None
    assert tasks[0].depends_on_repo is None


def test_repos_entry_missing_path_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A repos entry without a path is skipped with a warning."""
    import logging

    plan_file = _write_plan(
        tmp_path,
        {
            "repos": [{"branch": "main"}],  # no path
            "stages": [{"name": "S", "steps": [{"title": "T"}]}],
        },
    )
    with caplog.at_level(logging.WARNING, logger="bernstein.core.plan_loader"):
        config, _ = load_plan(plan_file)
    assert config.repos == []
    assert "missing 'path'" in caplog.text


def test_repo_ref_name_auto_derived() -> None:
    """RepoRef derives its name from the last path component when name is empty."""
    assert RepoRef(path="../backend").name == "backend"
    assert RepoRef(path="services/auth-service/").name == "auth-service"
    assert RepoRef(path="../shared-types", name="types").name == "types"
