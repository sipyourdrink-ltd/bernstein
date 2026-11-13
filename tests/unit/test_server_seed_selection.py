"""Selected seed identity must survive local server recovery (#6643)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.core.config.seed import SeedError, resolve_seed_path
from bernstein.core.orchestration import bootstrap
from bernstein.core.server import server_launch, server_supervisor


@dataclass
class _Launch:
    argv: list[str]
    env: dict[str, str] = field(repr=False)
    cwd: str


@dataclass
class _Harness:
    launches: list[_Launch] = field(default_factory=list)
    states: list[server_supervisor._SupervisorState] = field(default_factory=list)
    spawner: MagicMock = field(default_factory=lambda: MagicMock(return_value=222))
    watchdog: MagicMock = field(default_factory=lambda: MagicMock(return_value=333))

    def popen(self, argv: list[str], *, env: dict[str, str], cwd: str, **kwargs: Any) -> SimpleNamespace:
        del kwargs
        self.launches.append(_Launch(list(argv), dict(env), cwd))
        return SimpleNamespace(pid=1000 + len(self.launches))

    def thread(self, *, args: tuple[server_supervisor._SupervisorState], **kwargs: Any) -> MagicMock:
        del kwargs
        if args[0] not in self.states:
            self.states.append(args[0])
        return MagicMock()


@pytest.fixture
def launch_harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Run bootstrap and launch plumbing, replacing process/IO integrations."""
    harness = _Harness()
    for name in (
        "_acquire_pid_lock",
        "_run_git_hygiene",
        "preflight_checks",
        "check_config_paths",
        "_record_team_manifest_lineage",
        "ensure_sdd",
        "_reconcile_dead_owner_before_runtime_cleanup",
        "_clean_stale_runtime",
        "_discover_catalog",
        "_index_codebase_with_timeout",
        "_check_safety_invariants",
        "_apply_storage_env",
        "_apply_compliance_env",
        "_register_ci_parsers",
        "_load_secrets_provider",
        "_register_mcp_discovery",
    ):
        monkeypatch.setattr(bootstrap, name, MagicMock())
    monkeypatch.setattr(bootstrap, "console", MagicMock())
    monkeypatch.setattr(bootstrap, "_resolve_bind_host", lambda: "127.0.0.1")
    monkeypatch.setattr(bootstrap, "_resolve_auth_token", lambda workdir: "synthetic-auth")
    monkeypatch.setattr(bootstrap, "_resolve_server_url", lambda port: "http://127.0.0.1:8052")
    monkeypatch.setattr(bootstrap, "_wait_for_server", MagicMock(return_value=True))
    monkeypatch.setattr(bootstrap, "_sync_and_plan_tasks", MagicMock(return_value=(0, "manager", None)))
    monkeypatch.setattr(bootstrap, "_describe_cost_estimate", MagicMock(return_value="synthetic estimate"))
    monkeypatch.setattr(bootstrap, "_start_spawner", harness.spawner)
    monkeypatch.setattr(bootstrap, "_start_watchdog", harness.watchdog)
    monkeypatch.setattr(server_supervisor.subprocess, "Popen", harness.popen)
    monkeypatch.setattr(server_supervisor.threading, "Thread", harness.thread)
    monkeypatch.setattr(server_supervisor, "_is_alive", lambda pid: False)
    monkeypatch.setattr(server_supervisor.time, "sleep", lambda seconds: None)
    # No process-liveness probes or import-time server app construction on the
    # watchdog path: only its real _start_server env/argv plumbing is exercised.
    app = ModuleType("bernstein.core.server.server_app")
    app.preflight_multi_worker_guard = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, app.__name__, app)
    monkeypatch.setattr(server_launch, "_read_pid", lambda path: None)
    monkeypatch.setenv("BERNSTEIN_WORKERS", "1")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("BERNSTEIN_CONFIG_OVERRIDE", raising=False)
    return harness


def _seed(path: Path, goal: str = "Selected synthetic policy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"goal: {goal}\ncli: codex\n", encoding="utf-8")
    return path.resolve()


def _bootstrap(selected: Path, workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    bootstrap.bootstrap_from_seed(selected, workdir)


def _restart(state: server_supervisor._SupervisorState, monkeypatch: pytest.MonkeyPatch) -> None:
    launch = server_supervisor._launch_server

    def launch_once(current: server_supervisor._SupervisorState) -> int:
        current.stopped = True
        return launch(current)

    with monkeypatch.context() as patch:
        patch.setattr(server_supervisor, "_launch_server", launch_once)
        server_supervisor._supervisor_loop(state)


def test_explicit_seed_reaches_initial_server_without_changing_parent_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
) -> None:
    workdir = tmp_path / "project"
    selected = _seed(tmp_path / "selected policy" / "policy file.yaml")
    ambient = _seed(tmp_path / "ambient.yaml", "Ambient policy")
    _seed(workdir / "bernstein.yaml", "Discovery policy")
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(ambient))
    before = dict(os.environ)

    _bootstrap(selected, workdir)

    assert dict(os.environ) == before
    assert len(launch_harness.launches) == 1
    child = launch_harness.launches[0]
    assert child.env["BERNSTEIN_SEED_PATH"] == str(selected)
    with monkeypatch.context() as child_env:
        child_env.setenv("BERNSTEIN_SEED_PATH", child.env["BERNSTEIN_SEED_PATH"])
        assert resolve_seed_path(workdir) == selected
    assert child.env["BERNSTEIN_AUTH_TOKEN"] == "synthetic-auth"
    assert "--reload" not in child.argv


def test_selected_seed_survives_supervisor_restart_after_parent_environment_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
) -> None:
    selected = _seed(tmp_path / "selected.yaml")
    _bootstrap(selected, tmp_path / "project")
    changed = _seed(tmp_path / "different.yaml", "Later parent policy")
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(changed))
    before = dict(os.environ)

    _restart(launch_harness.states[0], monkeypatch)

    assert dict(os.environ) == before
    assert len(launch_harness.launches) == 2
    assert [launch.env.get("BERNSTEIN_SEED_PATH") for launch in launch_harness.launches] == [
        str(selected),
        str(selected),
    ]
    assert launch_harness.states[0].restart_count == 1


def test_independent_supervisors_keep_their_selected_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
) -> None:
    selected = [_seed(tmp_path / f"policy {index}.yaml", f"Policy {index}") for index in range(2)]
    monkeypatch.delenv("BERNSTEIN_SEED_PATH", raising=False)
    before = dict(os.environ)
    for index, path in enumerate(selected):
        _bootstrap(path, tmp_path / f"project-{index}")
    for state in launch_harness.states:
        _restart(state, monkeypatch)

    assert dict(os.environ) == before
    assert len(launch_harness.states) == 2
    assert [launch.env.get("BERNSTEIN_SEED_PATH") for launch in launch_harness.launches] == [
        str(selected[0]),
        str(selected[1]),
        str(selected[0]),
        str(selected[1]),
    ]


@pytest.mark.parametrize("replacement", [None, "goal: [synthetic-secret-marker"])
def test_unavailable_selected_seed_refuses_relaunch_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
    replacement: str | None,
) -> None:
    selected = _seed(tmp_path / "selected.yaml")
    fallback = _seed(tmp_path / "project" / "bernstein.yaml", "Fallback must not run")
    _bootstrap(selected, fallback.parent)
    if replacement is None:
        selected.unlink()
    else:
        selected.write_text(replacement, encoding="utf-8")
    before = dict(os.environ)

    with pytest.raises(SeedError) as error:
        server_supervisor._launch_server(launch_harness.states[0])

    assert len(launch_harness.launches) == 1
    assert dict(os.environ) == before
    assert str(selected) in str(error.value)
    assert "synthetic-secret-marker" not in str(error.value)


def test_goal_free_selected_seed_is_accepted_and_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
) -> None:
    """A selected seed with no goal key is valid and launches normally (#6604)."""
    selected = _seed(tmp_path / "selected.yaml")
    selected.write_text("cli: codex\n", encoding="utf-8")
    _bootstrap(selected, tmp_path / "project")
    before = dict(os.environ)

    server_supervisor._launch_server(launch_harness.states[0])

    assert len(launch_harness.launches) == 2
    assert dict(os.environ) == before


def test_selected_seed_provenance_records_path_without_seed_contents(
    tmp_path: Path,
    launch_harness: _Harness,
) -> None:
    selected = _seed(tmp_path / "selected.yaml", "synthetic-secret-marker")
    workdir = tmp_path / "project"
    _bootstrap(selected, workdir)

    raw = (workdir / ".sdd/runtime/supervisor_state.json").read_text(encoding="utf-8")
    assert json.loads(raw).get("seed_path") == str(selected)
    assert "synthetic-secret-marker" not in raw


def test_bootstrap_already_preserves_selected_seed_for_spawner_and_watchdog(
    tmp_path: Path,
    launch_harness: _Harness,
) -> None:
    selected = _seed(tmp_path / "policy with spaces.yaml")
    _bootstrap(selected, tmp_path / "project")

    assert launch_harness.spawner.call_args.kwargs["seed_path"] == selected
    assert launch_harness.watchdog.call_args.kwargs["seed_path"] == selected


@pytest.mark.parametrize("ambient", [False, True])
def test_unselected_supervisor_preserves_existing_seed_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
    ambient: bool,
) -> None:
    (tmp_path / ".sdd/runtime").mkdir(parents=True)
    if ambient:
        path = _seed(tmp_path / "ambient.yaml")
        monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(path))
    else:
        monkeypatch.delenv("BERNSTEIN_SEED_PATH", raising=False)
    before = dict(os.environ)

    server_supervisor.supervised_server(tmp_path, 8052)

    assert dict(os.environ) == before
    child = launch_harness.launches[0]
    if ambient:
        assert child.env["BERNSTEIN_SEED_PATH"] == str(path)
    else:
        assert "BERNSTEIN_SEED_PATH" not in child.env
        assert resolve_seed_path(tmp_path) == (tmp_path / "bernstein.yaml").resolve()


class _WatchdogCycleFinished(Exception):
    pass


def test_watchdog_server_recovery_keeps_selected_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
) -> None:
    (tmp_path / ".sdd/runtime").mkdir(parents=True)
    selected = _seed(tmp_path / "selected policy.yaml")
    ambient = _seed(tmp_path / "ambient.yaml", "Wrong ambient policy")
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(ambient))
    monkeypatch.setattr(bootstrap, "_supervisor_should_stand_down", lambda workdir: None)
    monkeypatch.setattr(bootstrap, "classify_pidfile_liveness", lambda path: (bootstrap.LIVENESS_GONE, None))

    def recover_server(**kwargs: Any) -> None:
        assert kwargs["name"] == "Server"
        kwargs["restart_fn"]()
        raise _WatchdogCycleFinished

    monkeypatch.setattr(bootstrap, "_watchdog_check_process", recover_server)
    before = dict(os.environ)
    with pytest.raises(_WatchdogCycleFinished):
        bootstrap.run_watchdog(tmp_path, 8052, poll_s=0, seed_path=selected)

    assert dict(os.environ) == before
    assert len(launch_harness.launches) == 1
    assert launch_harness.launches[0].env["BERNSTEIN_SEED_PATH"] == str(selected)


@pytest.mark.parametrize("launcher", ["supervisor", "direct"])
@pytest.mark.parametrize("valid", [False, True])
def test_selected_initial_launch_validates_before_spawning(
    tmp_path: Path,
    launch_harness: _Harness,
    launcher: str,
    valid: bool,
) -> None:
    (tmp_path / ".sdd/runtime").mkdir(parents=True)
    selected = _seed(tmp_path / "selected.yaml")
    if not valid:
        selected.write_text("goal: [synthetic-secret-marker", encoding="utf-8")
    launch = server_supervisor.supervised_server if launcher == "supervisor" else server_launch._start_server
    if valid:
        launch(tmp_path, 8052, seed_path=selected)
        assert launch_harness.launches[0].env["BERNSTEIN_SEED_PATH"] == str(selected)
    else:
        with pytest.raises(SeedError, match="Cannot load selected seed") as error:
            launch(tmp_path, 8052, seed_path=selected)
        assert str(selected) in str(error.value)
        assert "synthetic-secret-marker" not in str(error.value)
        assert launch_harness.launches == []


def test_launch_validation_preserves_parent_tuning_while_public_parser_still_applies_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
) -> None:
    from bernstein.core import defaults
    from bernstein.core.config.seed import parse_seed

    (tmp_path / ".sdd/runtime").mkdir(parents=True)
    selected = _seed(tmp_path / "tuned.yaml")
    changed_interval = defaults.ORCHESTRATOR.tick_interval_s + 17
    selected.write_text(
        selected.read_text(encoding="utf-8") + f"tuning:\n  orchestrator:\n    tick_interval_s: {changed_interval}\n",
        encoding="utf-8",
    )
    original = defaults.ORCHESTRATOR
    # Restore even if the public parser compatibility assertion fails.
    monkeypatch.setattr(defaults, "ORCHESTRATOR", original)
    server_supervisor.supervised_server(tmp_path, 8052, seed_path=selected)
    assert defaults.ORCHESTRATOR is original
    server_supervisor._launch_server(launch_harness.states[0])
    assert defaults.ORCHESTRATOR is original
    server_launch._start_server(tmp_path, 8053, seed_path=selected)
    assert defaults.ORCHESTRATOR is original
    parse_seed(selected)
    assert defaults.ORCHESTRATOR.tick_interval_s == changed_interval


def test_supervisor_snapshot_reads_old_state_and_round_trips_selected_path(tmp_path: Path) -> None:
    from bernstein.core.runtime_state import read_supervisor_state, write_supervisor_state

    runtime = tmp_path / ".sdd/runtime"
    runtime.mkdir(parents=True)
    (runtime / "supervisor_state.json").write_text(
        json.dumps({"started_at": 1.0, "restart_count": 2, "current_pid": 123}), encoding="utf-8"
    )
    legacy = read_supervisor_state(tmp_path / ".sdd")
    assert legacy is not None
    assert legacy.seed_path is None
    state = server_supervisor._SupervisorState(
        tmp_path, 8052, "127.0.0.1", False, None, False, seed_path=tmp_path / "selected.yaml"
    )
    write_supervisor_state(tmp_path, state.snapshot())
    restored = read_supervisor_state(tmp_path / ".sdd")
    assert restored == state.snapshot()


def test_watchdog_spawner_recovery_preserves_selected_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_harness: _Harness,
) -> None:
    selected = _seed(tmp_path / "selected.yaml")
    monkeypatch.setattr(bootstrap, "_supervisor_should_stand_down", lambda workdir: None)
    monkeypatch.setattr(bootstrap, "classify_pidfile_liveness", lambda path, **kw: (bootstrap.LIVENESS_GONE, None))
    monkeypatch.setattr(bootstrap, "_read_pid", lambda path: 123)
    monkeypatch.setattr(bootstrap, "_is_alive", lambda pid: True)

    def recover_spawner(**kwargs: Any) -> tuple[None, int, bool]:
        if kwargs["name"] == "Server":
            return None, 0, False
        kwargs["restart_fn"]()
        raise _WatchdogCycleFinished

    monkeypatch.setattr(bootstrap, "_watchdog_check_process", recover_spawner)
    with pytest.raises(_WatchdogCycleFinished):
        bootstrap.run_watchdog(tmp_path, 8052, poll_s=0, seed_path=selected, adapter="codex", model="synthetic")
    assert launch_harness.spawner.call_args.kwargs == {
        "seed_path": selected,
        "adapter": "codex",
        "model": "synthetic",
    }
