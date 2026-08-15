"""Tests for fleet bulk-action dispatch."""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from bernstein.cli.main import cli
from bernstein.core.fleet.aggregator import ProjectSnapshot, ProjectState
from bernstein.core.fleet.bulk import (
    bulk_cost_report,
    bulk_pause,
    bulk_resume,
    bulk_stop,
    evaluate_filter,
    select_projects,
)
from bernstein.core.fleet.config import ProjectConfig


def _project(tmp_path: Path, name: str) -> ProjectConfig:
    return ProjectConfig(
        name=name,
        path=tmp_path,
        task_server_url="http://127.0.0.1:8052",
        sdd_dir=tmp_path / ".sdd",
    )


def test_evaluate_filter_basic(tmp_path: Path) -> None:
    snap = ProjectSnapshot(
        name="alpha",
        state=ProjectState.ONLINE,
        agents=3,
        cost_usd=10.0,
        pending_approvals=2,
    )
    assert evaluate_filter(snap, "cost>5")
    assert not evaluate_filter(snap, "cost<5")
    assert evaluate_filter(snap, "agents==3")
    assert evaluate_filter(snap, "approvals>=2")


def test_select_projects_with_names_and_filter(tmp_path: Path) -> None:
    projects = [_project(tmp_path, "alpha"), _project(tmp_path, "bravo")]
    snapshots = [
        ProjectSnapshot(name="alpha", cost_usd=10.0),
        ProjectSnapshot(name="bravo", cost_usd=2.0),
    ]
    selected = select_projects(projects, snapshots, filter_expression="cost>5")
    assert {p.name for p in selected} == {"alpha"}
    selected = select_projects(projects, snapshots, names=["bravo"], filter_expression="cost<5")
    assert {p.name for p in selected} == {"bravo"}


def test_select_projects_invalid_filter_raises(tmp_path: Path) -> None:
    projects = [_project(tmp_path, "alpha")]
    snapshots = [ProjectSnapshot(name="alpha")]
    with pytest.raises(ValueError):
        select_projects(projects, snapshots, filter_expression="nope")


@pytest.mark.asyncio
async def test_bulk_stop_dispatches_to_each_project(tmp_path: Path) -> None:
    """``bulk_stop`` calls the runner once per project with ``["stop"]``."""
    projects = [_project(tmp_path, "alpha"), _project(tmp_path, "bravo")]
    calls: list[tuple[str, list[str], Path]] = []

    async def runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
        # Capture the trailing arg ("stop") - we don't care about the python prefix.
        calls.append((env["BERNSTEIN_TASK_SERVER_URL"], cmd[-1:], cwd))
        return 0, "ok", ""

    result = await bulk_stop(projects, runner=runner)
    assert sorted(result.succeeded) == ["alpha", "bravo"]
    assert result.failed == {}
    assert len(calls) == 2
    assert all(call[1] == ["stop"] for call in calls)


@pytest.mark.asyncio
async def test_bulk_pause_uses_daemon_stop(tmp_path: Path) -> None:
    projects = [_project(tmp_path, "alpha")]
    captured: list[list[str]] = []

    async def runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
        captured.append(cmd[-2:])
        return 0, "", ""

    result = await bulk_pause(projects, runner=runner)
    assert result.succeeded == ["alpha"]
    assert captured == [["daemon", "stop"]]


@pytest.mark.asyncio
async def test_bulk_resume_uses_daemon_start(tmp_path: Path) -> None:
    projects = [_project(tmp_path, "alpha")]
    captured: list[list[str]] = []

    async def runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
        captured.append(cmd[-2:])
        return 0, "", ""

    result = await bulk_resume(projects, runner=runner)
    assert result.succeeded == ["alpha"]
    assert captured == [["daemon", "start"]]


@pytest.mark.asyncio
async def test_bulk_records_failure_per_project(tmp_path: Path) -> None:
    projects = [_project(tmp_path, "alpha"), _project(tmp_path, "bravo")]

    async def runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
        if "alpha" in env["BERNSTEIN_TASK_SERVER_URL"]:
            return 0, "ok", ""
        # Both projects have the same default URL; instead distinguish by cwd.
        if "bravo" in str(cwd):
            return 1, "", "boom"
        return 0, "ok", ""

    # Force per-project paths to be unique:
    bravo_root = tmp_path / "bravo"
    bravo_root.mkdir(parents=True, exist_ok=True)
    projects = [
        ProjectConfig(
            name="alpha",
            path=tmp_path,
            task_server_url="http://127.0.0.1:8052",
            sdd_dir=tmp_path / ".sdd",
        ),
        ProjectConfig(
            name="bravo",
            path=bravo_root,
            task_server_url="http://127.0.0.1:8052",
            sdd_dir=bravo_root / ".sdd",
        ),
    ]
    result = await bulk_stop(projects, runner=runner)
    assert "alpha" in result.succeeded
    assert "bravo" in result.failed
    assert "boom" in result.failed["bravo"]


def _resolve(path: str) -> click.Command | None:
    """Walk ``path`` through the real CLI, returning None at the first miss.

    Same walk ``tests/unit/test_cli_command_registration.py`` uses on doc
    tables; kept local so this file has no cross-test import.
    """
    node: click.Command | None = cli
    for word in path.split():
        if not isinstance(node, click.Group):
            return None
        node = node.get_command(click.Context(node), word)
        if node is None:
            return None
    return node


async def _capture_dispatch(project: ProjectConfig) -> list[str]:
    """Run ``bulk_cost_report`` against a stub runner and return the argv it dispatched."""
    captured: list[list[str]] = []

    async def runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
        captured.append(list(cmd))
        return 0, "ok", ""

    await bulk_cost_report([project], runner=runner)
    assert len(captured) == 1
    # Drop the `python -m bernstein` prefix chosen by `_bernstein_command()`.
    cmd = captured[0]
    return cmd[cmd.index("bernstein") + 1 :]


@pytest.mark.asyncio
async def test_bulk_cost_report_dispatches_a_command_that_resolves(tmp_path: Path) -> None:
    """The argv ``bulk_cost_report`` spawns is a spelling the CLI actually has.

    Guards issue #3755: the dispatch asked for ``bernstein cost report``, which
    has never been a registered subcommand, so every project in the fleet came
    back with click's exit 2 and the whole sweep reported failure.  Asserting on
    the argv rather than on a subprocess keeps the check hermetic; the exit code
    is pinned separately below.
    """
    args = await _capture_dispatch(_project(tmp_path, "alpha"))

    assert _resolve(" ".join(args)) is not None, (
        f"fleet bulk-cost-report dispatches `bernstein {' '.join(args)}`, "
        "which does not resolve through the top-level CLI"
    )


@pytest.mark.asyncio
async def test_bulk_cost_report_passes_extra_args_through(tmp_path: Path) -> None:
    """``extra_args`` land after the command, so ``--json`` still reaches it."""
    captured: list[list[str]] = []

    async def runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
        captured.append(list(cmd))
        return 0, "ok", ""

    await bulk_cost_report([_project(tmp_path, "alpha")], runner=runner, extra_args=["--json"])
    assert captured[0][-1] == "--json"


@pytest.mark.asyncio
async def test_bulk_cost_report_exits_zero_against_a_fixture_project(tmp_path: Path) -> None:
    """The real subprocess returns 0 for a project that has run bernstein before.

    The argv test above cannot see click's exit code, and "resolves" is not the
    same claim as "succeeds".  This one spawns the actual interpreter against a
    fixture project, which is what a fleet operator gets.

    The fixture carries an (empty) ``.sdd/metrics`` directory on purpose: a
    project that has never run bernstein has no such directory and ``bernstein
    cost`` exits 1 there.  That asymmetry is a separate defect and is reported
    on #3755 rather than fixed here.
    """
    (tmp_path / ".sdd" / "metrics").mkdir(parents=True)
    project = _project(tmp_path, "alpha")

    result = await bulk_cost_report([project])

    assert result.succeeded == ["alpha"], f"expected a clean sweep, got failures: {result.failed}"
