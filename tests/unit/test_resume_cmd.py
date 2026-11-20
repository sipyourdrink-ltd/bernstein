"""Unit tests for ``bernstein resume`` and the per-task checkpoint store.

Covers the four AC paths from ``feat-resume-from-checkpoint``:

* Happy-path round-trip: write → read → resume_count bumped on disk.
* Mid-step kill: ``last_completed_step_id`` is what gets re-injected.
* Corrupt JSON / failed schema: actionable error + non-zero exit.
* Missing checkpoint: actionable error + non-zero exit.
* Adapter without ``resume()`` falls back to fresh + scratchpad block.
* ``task.resume`` lifecycle hook fires with the expected context.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from bernstein.adapters._contract import (
    RESUME_CAPABILITY_MATRIX,
    RESUME_FALLBACK_FRESH,
    RESUME_NATIVE,
    resume_capability,
)
from bernstein.adapters.base import CLIAdapter
from bernstein.cli.commands.resume_cmd import (
    EXIT_CORRUPT,
    EXIT_IO_ERROR,
    EXIT_NO_CHECKPOINT,
    EXIT_OK,
    prepare_resume,
    resume_cmd,
)
from bernstein.core.lifecycle.hooks import (
    HookRegistry,
    LifecycleContext,
    LifecycleEvent,
)
from bernstein.core.persistence.agent_checkpoint import AgentCheckpoint, find_checkpoint_for_task
from bernstein.core.persistence.agent_checkpoint import save_checkpoint as save_agent_checkpoint
from bernstein.core.persistence.resume_prompt import (
    RESUME_BANNER,
    build_resume_context,
    read_scratchpad,
)
from bernstein.core.persistence.task_resume import (
    CHECKPOINT_FILENAME,
    CheckpointCorruptError,
    CheckpointMissingError,
    TaskResumeCheckpoint,
    bump_resume_count,
    checkpoint_dir_for,
    checkpoint_path_for,
    load_checkpoint,
    save_checkpoint,
    scratchpad_sha256,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """Isolated workdir; ``.sdd/runtime/checkpoints`` is created lazily."""
    return tmp_path


def _make_checkpoint(task_id: str = "t-1", **overrides: object) -> TaskResumeCheckpoint:
    payload: dict[str, object] = {
        "task_id": task_id,
        "last_completed_step_id": "step-3",
        "trace_cursor": 1024,
        "scratchpad_path": None,
        "adapter": "claude",
        "adapter_session_id": "sess-abc",
        "worktree_path": "/tmp/wt",
    }
    payload.update(overrides)
    return TaskResumeCheckpoint(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(workdir: Path) -> None:
    cp = _make_checkpoint()
    path = save_checkpoint(workdir, cp)
    assert path == checkpoint_path_for(workdir, "t-1")
    assert path.is_file()

    loaded = load_checkpoint(workdir, "t-1")
    assert loaded.task_id == "t-1"
    assert loaded.last_completed_step_id == "step-3"
    assert loaded.trace_cursor == 1024
    assert loaded.adapter_session_id == "sess-abc"
    # updated_at must have been stamped by save_checkpoint -> touch()
    assert loaded.updated_at  # non-empty


def test_load_missing_raises_actionable_error(workdir: Path) -> None:
    with pytest.raises(CheckpointMissingError) as ei:
        load_checkpoint(workdir, "nope")
    msg = str(ei.value)
    assert "nope" in msg
    assert "bernstein run" in msg


def test_load_corrupt_json_raises_actionable_error(workdir: Path) -> None:
    target = checkpoint_dir_for(workdir, "broken")
    target.mkdir(parents=True)
    (target / CHECKPOINT_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckpointCorruptError) as ei:
        load_checkpoint(workdir, "broken")
    assert "broken" in str(ei.value)


def test_load_schema_violation_raises_actionable_error(workdir: Path) -> None:
    target = checkpoint_dir_for(workdir, "bad-schema")
    target.mkdir(parents=True)
    # missing required ``task_id`` field
    (target / CHECKPOINT_FILENAME).write_text(
        json.dumps({"schema_version": 1, "trace_cursor": 0}),
        encoding="utf-8",
    )
    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(workdir, "bad-schema")


def test_load_non_object_payload_raises(workdir: Path) -> None:
    target = checkpoint_dir_for(workdir, "list-root")
    target.mkdir(parents=True)
    (target / CHECKPOINT_FILENAME).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(workdir, "list-root")


def test_save_is_atomic_no_stale_tmp_files(workdir: Path) -> None:
    cp = _make_checkpoint(task_id="atomic")
    save_checkpoint(workdir, cp)
    target_dir = checkpoint_dir_for(workdir, "atomic")
    # No leftover .tmp files
    tmps = list(target_dir.glob(".checkpoint-*.tmp"))
    assert tmps == []


def test_bump_resume_count_increments_and_persists(workdir: Path) -> None:
    save_checkpoint(workdir, _make_checkpoint(task_id="bump"))
    first = bump_resume_count(workdir, "bump")
    assert first.resume_count == 1
    second = bump_resume_count(workdir, "bump")
    assert second.resume_count == 2

    # Persisted to disk: a fresh load sees the bumped value.
    persisted = load_checkpoint(workdir, "bump")
    assert persisted.resume_count == 2


def test_bump_resume_count_missing_raises(workdir: Path) -> None:
    with pytest.raises(CheckpointMissingError):
        bump_resume_count(workdir, "missing")


def test_scratchpad_sha256_handles_missing_and_present(tmp_path: Path) -> None:
    assert scratchpad_sha256(None) is None
    assert scratchpad_sha256(tmp_path / "absent.md") is None

    scratchpad = tmp_path / "pad.md"
    scratchpad.write_bytes(b"hello world")
    digest = scratchpad_sha256(scratchpad)
    assert digest is not None
    assert len(digest) == 64  # SHA-256 hex


def test_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TaskResumeCheckpoint.model_validate(
            {
                "task_id": "t-1",
                "uninvited_guest": True,
            }
        )


# ---------------------------------------------------------------------------
# Prompt-builder fallback path
# ---------------------------------------------------------------------------


def test_build_resume_context_includes_banner_and_metadata(workdir: Path) -> None:
    scratchpad = workdir / "pad.md"
    scratchpad.write_text("note from before crash\n", encoding="utf-8")
    cp = _make_checkpoint(
        scratchpad_path=str(scratchpad),
        scratchpad_sha256=scratchpad_sha256(scratchpad),
        resume_count=2,
    )
    block = build_resume_context(cp)
    assert RESUME_BANNER in block
    assert "step-3" in block  # last_completed_step_id
    assert "resume_attempt: 2" in block
    assert "sess-abc" in block
    assert "note from before crash" in block


def test_build_resume_context_handles_missing_scratchpad() -> None:
    cp = _make_checkpoint(scratchpad_path="/nonexistent/scratchpad.md")
    block = build_resume_context(cp)
    assert "no scratchpad was captured" in block


def test_read_scratchpad_returns_empty_on_none() -> None:
    assert read_scratchpad(None) == ""


def test_read_scratchpad_returns_empty_on_missing_file(tmp_path: Path) -> None:
    assert read_scratchpad(tmp_path / "nope.md") == ""


# ---------------------------------------------------------------------------
# Adapter capability matrix + default ``resume()`` fallback
# ---------------------------------------------------------------------------


def test_default_resume_returns_none_for_fallback(workdir: Path) -> None:
    class _Stub(CLIAdapter):
        def spawn(self, **_kwargs: object) -> object:  # type: ignore[override]
            raise AssertionError("not called")

        def name(self) -> str:
            return "stub"

    adapter = _Stub()
    assert adapter.resume("sess-1", {}) is None


def test_resume_capability_matrix_known_natives() -> None:
    assert resume_capability("claude") == RESUME_NATIVE
    assert resume_capability("openai_agents") == RESUME_NATIVE


def test_resume_capability_matrix_unknown_defaults_fallback() -> None:
    assert resume_capability("this-adapter-does-not-exist") == RESUME_FALLBACK_FRESH


def test_resume_capability_matrix_explicit_fallback() -> None:
    assert resume_capability("aider") == RESUME_FALLBACK_FRESH


def test_resume_capability_matrix_is_non_empty() -> None:
    # Sanity: matrix should cover at least the headline adapters
    expected_subset = {"claude", "codex", "aider", "openai_agents", "mock"}
    assert expected_subset.issubset(set(RESUME_CAPABILITY_MATRIX))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_resume_missing_checkpoint_exits_nonzero(workdir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        resume_cmd,
        ["nope", "--workdir", str(workdir)],
    )
    assert result.exit_code == EXIT_NO_CHECKPOINT
    assert "No checkpoint" in result.output

    result = runner.invoke(resume_cmd, ["nope", "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_NO_CHECKPOINT
    payload = json.loads(result.stdout)
    assert payload["dispatch_status"] == "not_dispatched"
    assert payload["runtime_acknowledged"] is False
    assert "No checkpoint" in payload["error"]


@pytest.mark.parametrize("contents", [b"not json", b"\xff"], ids=["invalid-json", "invalid-utf8"])
def test_cli_resume_corrupt_exits_nonzero(workdir: Path, contents: bytes) -> None:
    target = checkpoint_dir_for(workdir, "bad")
    target.mkdir(parents=True)
    (target / CHECKPOINT_FILENAME).write_bytes(contents)

    runner = CliRunner()
    result = runner.invoke(
        resume_cmd,
        ["bad", "--workdir", str(workdir)],
    )
    assert result.exit_code == EXIT_CORRUPT
    assert "Corrupt" in result.output

    result = runner.invoke(resume_cmd, ["bad", "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_CORRUPT
    payload = json.loads(result.stdout)
    assert payload["runtime_acknowledged"] is False
    assert "Corrupt" in payload["error"]


def test_missing_checkpoint_returns_json_when_git_is_unavailable(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(workdir))
    result = CliRunner().invoke(resume_cmd, ["missing", "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_NO_CHECKPOINT, result.output
    assert "No checkpoint" in json.loads(result.stdout)["error"]


def test_missing_checkpoint_survives_optional_git_timeout(workdir: Path) -> None:
    timeout = subprocess.TimeoutExpired(["git", "for-each-ref"], 30)
    with patch("bernstein.core.persistence.ledger_git.list_ledger_runs", side_effect=timeout):
        result = CliRunner().invoke(resume_cmd, ["missing", "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_NO_CHECKPOINT, result.exception
    report = json.loads(result.stdout)
    assert "No checkpoint" in report["error"]
    assert "hint" not in report
    assert report["signal_paths"] == []


@pytest.mark.parametrize(
    ("read_error", "exit_code"),
    [(PermissionError("checkpoint read denied"), EXIT_IO_ERROR), (FileNotFoundError(), EXIT_NO_CHECKPOINT)],
    ids=["unreadable", "disappeared"],
)
def test_checkpoint_read_failure_is_not_corruption(workdir: Path, read_error: OSError, exit_code: int) -> None:
    path = save_checkpoint(workdir, _make_checkpoint())
    before = path.read_bytes()
    with patch.object(Path, "read_text", side_effect=read_error):
        result = CliRunner().invoke(resume_cmd, ["t-1", "--workdir", str(workdir), "--json"])
    assert result.exit_code == exit_code, result.output
    report = json.loads(result.stdout)
    assert report["error"]
    assert "hint" not in report
    assert report["dispatch_status"] == "not_dispatched"
    assert report["signal_paths"] == []
    assert path.read_bytes() == before


def test_missing_checkpoint_returns_json_when_the_optional_ledger_root_is_a_file(workdir: Path) -> None:
    ledger_root = workdir / ".sdd" / "runtime" / "ledger"
    ledger_root.parent.mkdir(parents=True, exist_ok=True)
    ledger_root.write_text("unavailable")

    result = CliRunner().invoke(resume_cmd, ["missing", "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_NO_CHECKPOINT, result.exception
    report = json.loads(result.stdout)
    assert "No checkpoint" in report["error"]
    assert report["dispatch_status"] == "not_dispatched"


def test_cli_resume_happy_path_bumps_resume_count_and_writes_signal(workdir: Path) -> None:
    save_checkpoint(workdir, _make_checkpoint(task_id="ok"))
    runner = CliRunner()
    result = runner.invoke(
        resume_cmd,
        ["ok", "--workdir", str(workdir)],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "Resume plan for task" in result.output
    assert "Runtime acknowledgement: unavailable" in result.output

    # resume_count persisted to 1
    persisted = load_checkpoint(workdir, "ok")
    assert persisted.resume_count == 1

    # Signal file dropped for the worker watcher.
    signal = workdir / ".sdd" / "runtime" / "resume" / "ok.signal"
    assert signal.is_file()
    payload = json.loads(signal.read_text(encoding="utf-8"))
    assert payload["task_id"] == "ok"
    assert payload["resume_count"] == 1
    assert payload["capability"] == RESUME_NATIVE  # claude adapter


def test_cli_resume_dry_run_does_not_write_signal(workdir: Path) -> None:
    save_checkpoint(workdir, _make_checkpoint(task_id="dry"))
    runner = CliRunner()
    result = runner.invoke(
        resume_cmd,
        ["dry", "--workdir", str(workdir), "--dry-run"],
    )
    assert result.exit_code == EXIT_OK, result.output
    signal = workdir / ".sdd" / "runtime" / "resume" / "dry.signal"
    assert not signal.exists()
    # resume_count is still bumped in --dry-run; that's the contract.
    persisted = load_checkpoint(workdir, "dry")
    assert persisted.resume_count == 1


@pytest.mark.parametrize("dry_run", [False, True], ids=["queue", "dry-run"])
def test_cli_resume_json_output_emits_machine_readable(workdir: Path, dry_run: bool) -> None:
    save_checkpoint(workdir, _make_checkpoint(task_id="jsn"))
    args = ["jsn", "--workdir", str(workdir), "--json"]
    if dry_run:
        args.append("--dry-run")
    result = CliRunner().invoke(resume_cmd, args, color=True)
    assert result.exit_code == EXIT_OK, result.output
    parsed = json.loads(result.stdout)
    assert parsed["task_id"] == "jsn"
    assert parsed["capability"] == RESUME_NATIVE
    assert parsed["dry_run"] is dry_run
    assert parsed["runtime_acknowledged"] is False
    assert parsed["dispatch_status"] == ("not_dispatched" if dry_run else "queued")
    signal = workdir / ".sdd" / "runtime" / "resume" / "jsn.signal"
    assert parsed["signal_paths"] == ([] if dry_run else [str(signal)])
    assert signal.exists() is (not dry_run)
    assert load_checkpoint(workdir, "jsn").resume_count == 1


def test_cli_resume_discard_reports_result_in_json(workdir: Path) -> None:
    save_checkpoint(workdir, _make_checkpoint(task_id="discard"))
    runtime_dir = workdir / ".sdd" / "runtime"
    checkpoint = AgentCheckpoint(agent_id="agent", task_id="discard", worktree_path=str(workdir))
    save_agent_checkpoint(checkpoint, runtime_dir)

    result = CliRunner().invoke(resume_cmd, ["discard", "--workdir", str(workdir), "--discard", "--json"])

    assert result.exit_code == EXIT_OK, result.output
    payload = json.loads(result.stdout)
    assert payload["checkpoint_discarded"] is True
    assert payload["dispatch_status"] == "queued"
    assert payload["runtime_acknowledged"] is False
    assert find_checkpoint_for_task("discard", runtime_dir) is None

    again = CliRunner().invoke(resume_cmd, ["discard", "--workdir", str(workdir), "--discard", "--json"])
    assert again.exit_code == EXIT_OK, again.output
    assert json.loads(again.stdout)["checkpoint_discarded"] is False


def test_failed_discard_stops_before_preparing_or_signalling(workdir: Path) -> None:
    task = save_checkpoint(workdir, _make_checkpoint())
    runtime = workdir / ".sdd" / "runtime"
    agent = save_agent_checkpoint(AgentCheckpoint("agent", "t-1", str(workdir)), runtime)
    before = task.read_bytes()
    with patch.object(Path, "unlink", side_effect=PermissionError("checkpoint deletion denied")):
        result = CliRunner().invoke(resume_cmd, ["t-1", "--workdir", str(workdir), "--discard", "--json"])
    assert result.exit_code == EXIT_IO_ERROR, result.output
    report = json.loads(result.stdout)
    assert "checkpoint deletion denied" in report["error"]
    assert report["checkpoint_discarded"] is False
    assert report["dispatch_status"] == "not_dispatched"
    assert report["signal_paths"] == []
    assert report["runtime_acknowledged"] is False
    assert agent.is_file()
    assert task.read_bytes() == before
    assert not (runtime / "resume").exists()


def test_discard_cannot_report_absence_when_agent_directory_is_unreadable(workdir: Path) -> None:
    task = save_checkpoint(workdir, _make_checkpoint())
    runtime = workdir / ".sdd" / "runtime"
    (runtime / "agents").write_text("not a directory")
    before = task.read_bytes()
    result = CliRunner().invoke(resume_cmd, ["t-1", "--workdir", str(workdir), "--discard", "--json"])
    assert result.exit_code == EXIT_IO_ERROR, result.output
    report = json.loads(result.stdout)
    assert report["error"]
    assert report["checkpoint_discarded"] is False
    assert report["dispatch_status"] == "not_dispatched"
    assert task.read_bytes() == before
    assert not (runtime / "resume").exists()


def test_resume_queues_long_signal_name(workdir: Path) -> None:
    task_id = "t" * 240
    save_checkpoint(workdir, _make_checkpoint(task_id))
    result = CliRunner().invoke(resume_cmd, [task_id, "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_OK, result.output
    report = json.loads(result.stdout)
    signal = workdir / ".sdd" / "runtime" / "resume" / f"{task_id}.signal"
    assert report["dispatch_status"] == "queued"
    assert report["signal_paths"] == [str(signal)]
    assert json.loads(signal.read_bytes())["task_id"] == task_id
    assert list(signal.parent.iterdir()) == [signal]


def test_cli_resume_signal_write_failure_reports_error_in_json(workdir: Path) -> None:
    save_checkpoint(workdir, _make_checkpoint(task_id="blocked"))
    signal = workdir / ".sdd" / "runtime" / "resume" / "blocked.signal"
    signal.mkdir(parents=True)

    result = CliRunner().invoke(resume_cmd, ["blocked", "--workdir", str(workdir), "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["signal_paths"] == []
    assert payload["dispatch_status"] == "not_dispatched"
    assert payload["runtime_acknowledged"] is False
    assert "signal" in payload["error"].lower()


def test_discard_refuses_ambiguous_agent_checkpoints_before_deletion(workdir: Path) -> None:
    task = save_checkpoint(workdir, _make_checkpoint())
    before = task.read_bytes()
    runtime = workdir / ".sdd" / "runtime"
    agent_files = [
        save_agent_checkpoint(
            AgentCheckpoint(agent_id, "t-1", str(workdir), checkpointed_at=stamp, observations={"notes": "changed"}),
            runtime,
        )
        for agent_id, stamp in [("older", 1.0), ("newer", 2.0)]
    ]
    result = CliRunner().invoke(resume_cmd, ["t-1", "--discard", "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_CORRUPT, result.output
    report = json.loads(result.stdout)
    assert "multiple agent checkpoints" in report["error"].lower()
    assert report["checkpoint_discarded"] is False
    assert report["dispatch_status"] == "not_dispatched"
    assert report["signal_paths"] == []
    assert all(path.is_file() for path in agent_files)
    assert task.read_bytes() == before
    assert not (runtime / "resume").exists()


def test_non_utf8_scratchpad_uses_the_empty_fallback(workdir: Path) -> None:
    scratchpad = workdir / "notes.txt"
    scratchpad.write_bytes(b"\x96 previous step")
    save_checkpoint(workdir, _make_checkpoint(scratchpad_path=str(scratchpad)))
    result = CliRunner().invoke(resume_cmd, ["t-1", "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_OK, result.exception
    report = json.loads(result.stdout)
    assert report["dispatch_status"] == "queued"
    assert report["resume_count"] == load_checkpoint(workdir, "t-1").resume_count == 1
    assert json.loads(Path(report["signal_paths"][0]).read_bytes())["task_id"] == "t-1"
    assert read_scratchpad(scratchpad) == ""
    assert scratchpad.read_bytes() == b"\x96 previous step"


# ---------------------------------------------------------------------------
# Lifecycle hook integration
# ---------------------------------------------------------------------------


def test_prepare_resume_fires_task_resume_hook(workdir: Path) -> None:
    save_checkpoint(workdir, _make_checkpoint(task_id="hooky"))
    hooks = HookRegistry()
    seen: list[LifecycleContext] = []

    def _capture(ctx: LifecycleContext) -> None:
        seen.append(ctx)

    hooks.register_callable(LifecycleEvent.TASK_RESUME, _capture)
    plan = prepare_resume(workdir, "hooky", hooks=hooks)

    assert plan.checkpoint.resume_count == 1
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.event == LifecycleEvent.TASK_RESUME
    assert ctx.task == "hooky"
    assert ctx.env["BERNSTEIN_RESUME_COUNT"] == "1"
    assert ctx.env["BERNSTEIN_RESUME_CAPABILITY"] == RESUME_NATIVE


# ---------------------------------------------------------------------------
# Mid-step continuation invariant
# ---------------------------------------------------------------------------


def test_mid_step_kill_continues_from_next_step_boundary(workdir: Path) -> None:
    """The checkpoint we write after step N must surface as ``last_completed_step_id=N``.

    The orchestrator uses that field to know which step to begin from on
    re-spawn - the *next* boundary, never re-running step N. This test
    pins the contract: whatever we save is exactly what resume sees.
    """
    save_checkpoint(
        workdir,
        _make_checkpoint(
            task_id="midstep",
            last_completed_step_id="step-5",
            trace_cursor=4096,
        ),
    )
    plan = prepare_resume(workdir, "midstep")
    assert plan.checkpoint.last_completed_step_id == "step-5"
    assert plan.checkpoint.trace_cursor == 4096
    # And the resume_context block names the last completed step so the
    # adapter knows where to resume *from*.
    assert "step-5" in plan.resume_context
