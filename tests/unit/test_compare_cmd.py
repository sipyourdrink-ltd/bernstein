"""Tests for ``bernstein compare`` - side-by-side adapter comparison.

These tests use a synthetic in-process executor and a temp workspace.
No real adapter is spawned, no network is touched.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import compare_cmd as compare_cli
from bernstein.cli.main import cli
from bernstein.core.orchestration.compare_runner import (
    MAX_ADAPTERS,
    AdapterRun,
    CompareRun,
    CompareTaskSpec,
    parse_adapters_flag,
    render_markdown,
    run_compare,
    write_sidecar,
)

# ---------------------------------------------------------------------------
# Fixtures and synthetic executors
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Materialise a tiny workspace with two text files for diffing."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "hello.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return root


def _make_executor(
    *,
    file_to_write: str = "hello.py",
    new_content: str = "def hello():\n    return 'hello'\n",
    duration_ms: float = 12.0,
    exit_code: int = 0,
) -> Callable[[str, CompareTaskSpec, Path], AdapterRun]:
    """Build a deterministic executor that edits one file per adapter."""

    def _exec(adapter_name: str, task: CompareTaskSpec, worktree: Path) -> AdapterRun:
        target = worktree / file_to_write
        # Adapters differentiate by appending their name into the file so
        # the per-adapter diffs are distinct.
        target.write_text(new_content + f"# adapter:{adapter_name}\n", encoding="utf-8")
        return AdapterRun(
            adapter=adapter_name,
            worktree=worktree,
            exit_code=exit_code,
            duration_ms=duration_ms,
        )

    return _exec


def _noop_executor(adapter_name: str, _task: CompareTaskSpec, worktree: Path) -> AdapterRun:
    return AdapterRun(
        adapter=adapter_name,
        worktree=worktree,
        exit_code=0,
        duration_ms=1.0,
    )


# ---------------------------------------------------------------------------
# parse_adapters_flag
# ---------------------------------------------------------------------------


def test_parse_adapters_flag_splits_csv() -> None:
    assert parse_adapters_flag("claude,codex") == ["claude", "codex"]
    assert parse_adapters_flag("claude , codex , gemini ") == ["claude", "codex", "gemini"]
    assert parse_adapters_flag("") == []


def test_parse_adapters_flag_accepts_iterable() -> None:
    assert parse_adapters_flag(["claude", "  ", "codex"]) == ["claude", "codex"]


# ---------------------------------------------------------------------------
# run_compare - happy paths and edge cases
# ---------------------------------------------------------------------------


def test_run_compare_single_adapter_degenerate(workspace: Path) -> None:
    """1-adapter compare still works - same flow, no comparison."""
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    run = run_compare(task, ["claude"], workspace, executor=_make_executor())

    assert len(run.runs) == 1
    assert run.runs[0].adapter == "claude"
    assert run.runs[0].exit_code == 0
    assert "hello.py" in run.runs[0].changed_files
    assert run.runs[0].compare_run_id == run.compare_run_id


def test_run_compare_two_adapters_happy_path(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    run = run_compare(task, ["claude", "codex"], workspace, executor=_make_executor())

    assert [r.adapter for r in run.runs] == ["claude", "codex"]
    # Per-adapter diff content differs because the mock executor stamps
    # the adapter name into the produced file.
    diff_a = run.runs[0].changed_files["hello.py"]
    diff_b = run.runs[1].changed_files["hello.py"]
    assert "adapter:claude" in diff_a
    assert "adapter:codex" in diff_b
    assert diff_a != diff_b


def test_run_compare_rejects_too_many_adapters(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    with pytest.raises(ValueError, match="cannot compare more than"):
        run_compare(
            task,
            ["claude", "codex", "gemini", "aider", "cursor"],
            workspace,
            executor=_make_executor(),
        )


def test_run_compare_rejects_empty_adapters(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    with pytest.raises(ValueError, match="at least one adapter"):
        run_compare(task, [], workspace, executor=_make_executor())


def test_run_compare_rejects_duplicates(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    with pytest.raises(ValueError, match="duplicate adapter"):
        run_compare(task, ["claude", "claude"], workspace, executor=_make_executor())


def test_run_compare_rejects_missing_workspace(tmp_path: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    with pytest.raises(ValueError, match="must be an existing directory"):
        run_compare(task, ["claude"], tmp_path / "missing", executor=_make_executor())


def test_run_compare_cap_is_four(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    assert MAX_ADAPTERS == 4
    # Exactly four is allowed.
    run = run_compare(
        task,
        ["a1", "a2", "a3", "a4"],
        workspace,
        executor=_make_executor(),
    )
    assert len(run.runs) == 4


# ---------------------------------------------------------------------------
# Worktree lifecycle
# ---------------------------------------------------------------------------


def test_run_compare_cleans_worktrees_by_default(workspace: Path, tmp_path: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    root = tmp_path / "wt-root"
    run = run_compare(
        task,
        ["claude", "codex"],
        workspace,
        executor=_make_executor(),
        worktree_root=root,
    )
    assert not root.exists(), "worktree root should be removed by default"
    # AdapterRun.worktree paths must still be populated for the JSON sidecar.
    for r in run.runs:
        assert str(r.worktree).endswith(r.adapter)


def test_run_compare_keep_worktrees(workspace: Path, tmp_path: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="do stuff")
    root = tmp_path / "wt-root"
    run = run_compare(
        task,
        ["claude", "codex"],
        workspace,
        executor=_make_executor(),
        worktree_root=root,
        keep_worktrees=True,
    )
    assert root.exists()
    for r in run.runs:
        assert r.worktree.exists()
        # The synthetic executor wrote hello.py in each worktree.
        assert (r.worktree / "hello.py").exists()


def test_run_compare_handles_executor_exception(workspace: Path) -> None:
    def _boom(adapter_name: str, _task: CompareTaskSpec, worktree: Path) -> AdapterRun:
        del worktree
        raise RuntimeError(f"explode-{adapter_name}")

    task = CompareTaskSpec(task_id="t1", prompt="x")
    run = run_compare(task, ["claude", "codex"], workspace, executor=_boom)
    assert all(r.exit_code != 0 for r in run.runs)
    assert "explode-claude" in run.runs[0].error
    assert "explode-codex" in run.runs[1].error


# ---------------------------------------------------------------------------
# JSON schema
# ---------------------------------------------------------------------------


def test_compare_run_to_json_schema(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="hello", role="qa", seed=42)
    run = run_compare(task, ["claude", "codex"], workspace, executor=_make_executor())
    payload = json.loads(run.to_json())

    expected_top = {
        "compare_run_id",
        "task",
        "adapters",
        "runs",
        "started_at",
        "finished_at",
        "duration_ms",
    }
    assert expected_top.issubset(payload.keys())

    assert payload["adapters"] == ["claude", "codex"]
    assert payload["task"]["task_id"] == "t1"
    assert payload["task"]["role"] == "qa"
    assert payload["task"]["seed"] == 42
    assert "prompt_sha256" in payload["task"]

    run_keys = {
        "adapter",
        "worktree",
        "exit_code",
        "duration_ms",
        "changed_files",
        "stdout_tail",
        "error",
        "compare_run_id",
    }
    for entry in payload["runs"]:
        assert run_keys.issubset(entry.keys())
        assert entry["compare_run_id"] == payload["compare_run_id"]


def test_to_json_is_deterministic(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="hello")
    run = run_compare(task, ["claude"], workspace, executor=_make_executor())
    a = run.to_json()
    b = run.to_json()
    assert a == b


def test_write_sidecar_creates_file(workspace: Path, tmp_path: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="hello")
    run = run_compare(task, ["claude"], workspace, executor=_make_executor())
    out_dir = tmp_path / "traces"
    path = write_sidecar(run, out_dir)
    assert path.exists()
    assert path.name == f"compare-{run.compare_run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["compare_run_id"] == run.compare_run_id


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def test_render_markdown_contains_summary_table(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="hello")
    run = run_compare(task, ["claude", "codex"], workspace, executor=_make_executor())
    md = render_markdown(run)
    assert "# Compare run" in md
    assert "| adapter | exit | duration ms | files changed |" in md
    assert "`claude`" in md
    assert "`codex`" in md
    # Per-adapter section with diff fence.
    assert "```diff" in md
    assert "hello.py" in md


def test_render_markdown_no_changes_message(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="hello")
    run = run_compare(task, ["claude"], workspace, executor=_noop_executor)
    md = render_markdown(run)
    assert "_no files changed_" in md


def test_render_markdown_truncates_long_diff(workspace: Path) -> None:
    """Long diffs should be truncated with a marker for readability."""

    def _big_exec(adapter_name: str, _task: CompareTaskSpec, worktree: Path) -> AdapterRun:
        # Append many lines to force a >max-line diff.
        target = worktree / "hello.py"
        target.write_text("\n".join(f"line_{i}" for i in range(120)) + "\n", encoding="utf-8")
        return AdapterRun(
            adapter=adapter_name,
            worktree=worktree,
            exit_code=0,
            duration_ms=1.0,
        )

    task = CompareTaskSpec(task_id="t1", prompt="hello")
    run = run_compare(task, ["claude"], workspace, executor=_big_exec)
    md = render_markdown(run, max_diff_lines_per_file=10)
    assert "more lines truncated" in md


# ---------------------------------------------------------------------------
# CLI plumbing (Click)
# ---------------------------------------------------------------------------


def test_cli_compare_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["compare", "--help"])
    assert result.exit_code == 0, result.output
    assert "--adapters" in result.output
    assert "--keep-worktrees" in result.output


def test_cli_compare_explicit_traces_dir_overrides_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = tmp_path / "task.md"
    spec.write_text("Describe a garden.", encoding="utf-8")
    traces_dir = tmp_path / "explicit"
    monkeypatch.setenv("BERNSTEIN_TRACES_DIR", str(tmp_path / "environment"))

    run = SimpleNamespace(compare_run_id="fixture-run", runs=[SimpleNamespace(exit_code=0)])
    executor = Mock(side_effect=AssertionError("This test must not execute an adapter"))
    monkeypatch.setattr(compare_cli, "_parallel_executor_factory", Mock(return_value=(executor, Mock())))
    monkeypatch.setattr(compare_cli, "run_compare", Mock(return_value=run))
    monkeypatch.setattr(compare_cli, "render_markdown", Mock(return_value="Fixture comparison summary"))
    writer = Mock(return_value=traces_dir / f"compare-{run.compare_run_id}.json")
    monkeypatch.setattr(compare_cli, "write_sidecar", writer)

    result = CliRunner().invoke(
        cli,
        [
            "compare",
            str(spec),
            "--adapters",
            "mock",
            "--workspace",
            str(tmp_path),
            "--traces-dir",
            str(traces_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Fixture comparison summary" in result.stdout
    writer.assert_called_once_with(run, traces_dir)


def test_cli_compare_rejects_more_than_cap(workspace: Path, tmp_path: Path) -> None:
    spec = tmp_path / "task.md"
    spec.write_text("do thing", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "compare",
            str(spec),
            "--adapters",
            "a1,a2,a3,a4,a5",
            "--workspace",
            str(workspace),
            "--no-sidecar",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "cap" in result.output.lower()


def test_cli_compare_rejects_empty_adapters_flag(workspace: Path, tmp_path: Path) -> None:
    spec = tmp_path / "task.md"
    spec.write_text("do thing", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "compare",
            str(spec),
            "--adapters",
            "   ",
            "--workspace",
            str(workspace),
            "--no-sidecar",
        ],
    )
    assert result.exit_code == 2, result.output


def test_compare_run_started_finished_monotonic(workspace: Path) -> None:
    """Sanity: started_at <= finished_at and durations are non-negative."""
    task = CompareTaskSpec(task_id="t1", prompt="hello")
    before = time.time()
    run = run_compare(task, ["claude"], workspace, executor=_make_executor())
    after = time.time()
    assert before <= run.started_at <= run.finished_at <= after + 1
    assert all(r.duration_ms >= 0 for r in run.runs)


def test_compare_run_id_unique_across_runs(workspace: Path) -> None:
    task = CompareTaskSpec(task_id="t1", prompt="hello")
    r1 = run_compare(task, ["claude"], workspace, executor=_make_executor())
    r2 = run_compare(task, ["claude"], workspace, executor=_make_executor())
    assert r1.compare_run_id != r2.compare_run_id


def test_compare_run_dataclass_is_frozen() -> None:
    """``CompareRun`` is intended to be immutable for downstream caching."""
    task = CompareTaskSpec(task_id="t1", prompt="hello")
    run = CompareRun(
        compare_run_id="abc",
        task=task,
        adapters=("claude",),
        runs=(),
        started_at=0.0,
        finished_at=1.0,
    )
    with pytest.raises((AttributeError, TypeError)):
        run.compare_run_id = "other"  # type: ignore[misc]
