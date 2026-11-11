"""Tests for the filesystem-as-service-registry directory loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import bernstein.core.fleet as fleet
from bernstein.core.fleet.config import DEFAULT_TASK_SERVER_PORT, FleetConfigError
from bernstein.core.fleet.directory_registry import (
    DISABLED_FLAG_FILENAME,
    MANIFEST_FILENAME,
    DirectoryRegistry,
    InstanceSpec,
    default_fleet_root,
    load_directory_registry,
)
from bernstein.core.tasks.models import DispatchTier, Task, TaskType


def _write_instance(
    root: Path,
    name: str,
    *,
    manifest: str | None = "",
    disabled: bool = False,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (directory / MANIFEST_FILENAME).write_text(manifest, encoding="utf-8")
    if disabled:
        (directory / DISABLED_FLAG_FILENAME).write_text("", encoding="utf-8")
    return directory


def test_default_fleet_root_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BERNSTEIN_FLEET_ROOT", str(tmp_path / "custom"))
    assert default_fleet_root() == tmp_path / "custom"


def test_default_fleet_root_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_FLEET_ROOT", raising=False)
    root = default_fleet_root()
    assert root == Path("~/.bernstein/fleet").expanduser()


def test_scan_missing_root_reports_error(tmp_path: Path) -> None:
    registry = DirectoryRegistry(tmp_path / "nope")
    result = registry.scan()
    assert result.instances == []
    assert any("does not exist" in err.message for err in result.errors)


def test_scan_root_must_be_directory(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("", encoding="utf-8")
    registry = DirectoryRegistry(target)
    result = registry.scan()
    assert result.instances == []
    assert any("not a directory" in err.message for err in result.errors)


def test_scan_returns_empty_for_empty_root(tmp_path: Path) -> None:
    registry = DirectoryRegistry(tmp_path)
    result = registry.scan()
    assert result.instances == []
    assert result.errors == []
    assert result.disabled == []
    assert result.root == tmp_path


def test_scan_picks_up_minimal_instance(tmp_path: Path) -> None:
    _write_instance(tmp_path, "alpha", manifest="")
    result = DirectoryRegistry(tmp_path).scan()
    assert len(result.instances) == 1
    spec = result.instances[0]
    assert spec.name == "alpha"
    assert spec.directory == tmp_path / "alpha"
    assert spec.project_path == tmp_path / "alpha"
    assert spec.task_server_url == f"http://127.0.0.1:{DEFAULT_TASK_SERVER_PORT}"
    assert spec.disabled is False


def test_scan_honours_manifest_overrides(tmp_path: Path) -> None:
    project = tmp_path / "elsewhere"
    project.mkdir()
    manifest = f"""
name: beta
path: {project}
task_server_url: http://127.0.0.1:9099
"""
    _write_instance(tmp_path, "raw-dir-name", manifest=manifest)
    result = DirectoryRegistry(tmp_path).scan()
    assert len(result.instances) == 1
    spec = result.instances[0]
    assert spec.name == "beta"
    assert spec.project_path == project.resolve()
    assert spec.task_server_url == "http://127.0.0.1:9099"


def test_scan_resolves_relative_path_against_directory(tmp_path: Path) -> None:
    _write_instance(tmp_path, "gamma", manifest="path: nested-root\n")
    nested = tmp_path / "gamma" / "nested-root"
    nested.mkdir()
    result = DirectoryRegistry(tmp_path).scan()
    assert len(result.instances) == 1
    assert result.instances[0].project_path == nested.resolve()


def test_scan_skips_disabled_instance(tmp_path: Path) -> None:
    _write_instance(tmp_path, "active", manifest="")
    _write_instance(tmp_path, "asleep", manifest="", disabled=True)
    result = DirectoryRegistry(tmp_path).scan()
    assert [s.name for s in result.instances] == ["active"]
    assert [s.name for s in result.disabled] == ["asleep"]


def test_scan_ignores_subdirs_without_manifest(tmp_path: Path) -> None:
    _write_instance(tmp_path, "real", manifest="")
    (tmp_path / "stray").mkdir()
    result = DirectoryRegistry(tmp_path).scan()
    assert [s.name for s in result.instances] == ["real"]
    assert result.errors == []


def test_scan_ignores_hidden_dirs(tmp_path: Path) -> None:
    _write_instance(tmp_path, ".hidden", manifest="")
    _write_instance(tmp_path, "shown", manifest="")
    result = DirectoryRegistry(tmp_path).scan()
    assert [s.name for s in result.instances] == ["shown"]


def test_scan_reports_yaml_parse_error(tmp_path: Path) -> None:
    _write_instance(tmp_path, "broken", manifest="this: is: invalid: yaml: ::\n")
    result = DirectoryRegistry(tmp_path).scan()
    assert result.instances == []
    assert any("YAML parse error" in err.message for err in result.errors)


def test_scan_rejects_non_mapping_manifest(tmp_path: Path) -> None:
    _write_instance(tmp_path, "listy", manifest="- one\n- two\n")
    result = DirectoryRegistry(tmp_path).scan()
    assert result.instances == []
    assert any("YAML mapping" in err.message for err in result.errors)


def test_scan_rejects_empty_name(tmp_path: Path) -> None:
    _write_instance(tmp_path, "empty-name", manifest="name: ''\n")
    result = DirectoryRegistry(tmp_path).scan()
    assert result.instances == []
    assert any("non-empty string" in err.message for err in result.errors)


def test_scan_rejects_non_string_url(tmp_path: Path) -> None:
    _write_instance(tmp_path, "badurl", manifest="task_server_url: 1234\n")
    result = DirectoryRegistry(tmp_path).scan()
    assert result.instances == []
    assert any("task_server_url" in err.message for err in result.errors)


def test_scan_rejects_duplicate_names(tmp_path: Path) -> None:
    _write_instance(tmp_path, "one", manifest="name: shared\n")
    _write_instance(tmp_path, "two", manifest="name: shared\n")
    result = DirectoryRegistry(tmp_path).scan()
    assert len(result.instances) == 1
    assert result.instances[0].directory.name == "one"
    assert any("duplicate" in err.message for err in result.errors)


def test_load_directory_registry_convenience(tmp_path: Path) -> None:
    _write_instance(tmp_path, "delta", manifest="")
    result = load_directory_registry(tmp_path)
    assert [s.name for s in result.instances] == ["delta"]


def test_to_project_config_round_trip(tmp_path: Path) -> None:
    spec = InstanceSpec(
        name="zeta",
        directory=tmp_path,
        project_path=tmp_path,
        task_server_url="http://127.0.0.1:8052",
    )
    project = spec.to_project_config()
    assert project.name == "zeta"
    assert project.path == tmp_path
    assert project.sdd_dir == tmp_path / ".sdd"
    assert project.task_server_url == "http://127.0.0.1:8052"


def test_as_fleet_config_projects_disabled_as_errors(tmp_path: Path) -> None:
    _write_instance(tmp_path, "active", manifest="")
    _write_instance(tmp_path, "asleep", manifest="", disabled=True)
    config = DirectoryRegistry(tmp_path).as_fleet_config()
    assert [p.name for p in config.projects] == ["active"]
    assert any("asleep" in err.message and ".disabled" in err.message for err in config.errors)
    assert config.source_path == tmp_path


def test_registry_uses_env_root_when_root_not_passed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BERNSTEIN_FLEET_ROOT", str(tmp_path))
    _write_instance(tmp_path, "eta", manifest="")
    registry = DirectoryRegistry()
    assert registry.root == tmp_path
    result = registry.scan()
    assert [s.name for s in result.instances] == ["eta"]


def test_unreadable_manifest_reports_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":  # pragma: no cover - permission semantics differ on Windows
        pytest.skip("chmod-based permission tests are POSIX-only")
    _write_instance(tmp_path, "locked", manifest="")
    manifest_path = tmp_path / "locked" / MANIFEST_FILENAME
    manifest_path.chmod(0o000)
    try:
        if os.access(manifest_path, os.R_OK):
            pytest.skip("filesystem ignored chmod (likely running as root)")
        result = DirectoryRegistry(tmp_path).scan()
        assert result.instances == []
        assert any("cannot read" in err.message for err in result.errors)
    finally:
        manifest_path.chmod(0o644)


def _worker(**overrides: object) -> dict[str, object]:
    return {"worker_id": "w", "task_types": ["standard"], "dispatch_tiers": ["local"], **overrides}


@pytest.mark.parametrize(
    "workers,requires,task_type,tier,disabled,match",
    [
        ([_worker(capabilities=["python", "test"])], ["python", "test"], "standard", "local", False, True),
        ([_worker()], [], "standard", "local", False, True),
        ([_worker(capabilities=["extra"])], [], "standard", "local", False, True),
        (
            [_worker(task_types=["fix", "research"], dispatch_tiers=["edge", "research"])],
            [],
            "research",
            "edge",
            False,
            True,
        ),
        ([_worker()], [], "fix", "local", False, False),
        ([_worker()], [], "standard", "edge", False, False),
        ([_worker(capabilities=["python"])], ["python", "test"], "standard", "local", False, False),
        ([_worker(capabilities=["Python"])], ["python"], "standard", "local", False, False),
        ([_worker()], ["python"], "standard", "local", False, False),
        (
            [_worker(capabilities=["python"]), _worker(worker_id="w2", capabilities=["test"])],
            ["python", "test"],
            "standard",
            "local",
            False,
            False,
        ),
        ([_worker()], [], "standard", "local", True, False),
    ],
)
def test_select_fleet_by_worker_matrix(tmp_path, workers, requires, task_type, tier, disabled, match):
    for directory, name in [("01-first", "zulu"), ("02-last", "alpha")]:
        _write_instance(
            tmp_path, directory, manifest=yaml.safe_dump({"name": name, "workers": workers}), disabled=disabled
        )
    scan = DirectoryRegistry(tmp_path).scan()
    assert scan.errors == []
    selector = getattr(fleet, "select_fleet_instance", None)
    assert callable(selector), "fleet must expose its worker-matrix selector"
    task = Task(
        "t", "test", "", "backend", task_type=TaskType(task_type), dispatch_tier=DispatchTier(tier), requires=requires
    )
    selected = selector(task, [*scan.instances, *scan.disabled])
    assert (selected.name if selected else None) == ("alpha" if match else None)
    specs = scan.instances or scan.disabled
    assert specs[0].workers[0].capabilities == workers[0].get("capabilities", [])
    assert specs[0].workers[0].task_types == [TaskType(value) for value in workers[0]["task_types"]]
    assert specs[0].workers[0].dispatch_tiers == [DispatchTier(value) for value in workers[0]["dispatch_tiers"]]


@pytest.mark.parametrize("manifest", [{}, {"workers": []}])
def test_workers_dashboard_only(tmp_path, manifest):
    _write_instance(tmp_path, "dashboard", manifest=yaml.safe_dump(manifest))
    registry = DirectoryRegistry(tmp_path)
    scan = registry.scan()
    assert [spec.name for spec in scan.instances] == ["dashboard"]
    assert scan.errors == []
    assert getattr(scan.instances[0], "workers", None) == []
    assert [project.name for project in registry.as_fleet_config().projects] == ["dashboard"]
    assert fleet.select_fleet_instance(Task("t", "test", "", "backend"), scan.instances) is None


@pytest.mark.parametrize(
    "workers",
    [None, {}, "worker", 1, True]
    + [[_worker(), row] for row in [None, [], "worker", 1, True]]
    + [
        [_worker(), {key: value for key, value in _worker(worker_id="other").items() if key != missing}]
        for missing in ["worker_id", "task_types", "dispatch_tiers"]
    ]
    + [
        [_worker(), _worker(**{"worker_id": "other", key: value})]
        for key, values in {
            "worker_id": ["", "  ", None, 1, True, []],
            "task_types": [[], None, "standard", {}, 1, [1], [True], [None], ["unknown"]],
            "dispatch_tiers": [[], None, "local", {}, 1, [1], [True], [None], ["unknown"]],
            "capabilities": [None, "python", {}, 1, [1], [True], [None], [""], ["  "]],
            "unexpected": ["field"],
        }.items()
        for value in values
    ]
    + [[_worker(), _worker()]],
)
@pytest.mark.parametrize("disabled", [False, True])
def test_invalid_worker_matrix_retains_dashboard(tmp_path, workers, disabled):
    _write_instance(tmp_path, "broken-matrix", manifest=yaml.safe_dump({"workers": workers}), disabled=disabled)
    registry = DirectoryRegistry(tmp_path)
    scan = registry.scan()
    specs = scan.disabled if disabled else scan.instances
    assert [spec.name for spec in specs] == ["broken-matrix"]
    assert len(scan.errors) == 1
    assert isinstance(scan.errors[0], FleetConfigError)
    assert scan.errors[0].index == 0 and "workers" in scan.errors[0].message
    assert specs[0].workers == []
    assert fleet.select_fleet_instance(Task("t", "test", "", "backend"), specs) is None
    config = registry.as_fleet_config()
    assert [project.name for project in config.projects] == ([] if disabled else ["broken-matrix"])
    assert scan.errors[0] in config.errors
