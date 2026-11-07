"""Leaked sandbox sessions: listed by the owning task's recorded status (#5112).

The spawner provisions one sandbox session per agent spawn and destroys it
when the exec resolves, chaining ``sandbox.session_create`` and
``sandbox.session_destroy`` into the audit log either way. A session whose
create has no matching destroy outlived its spawn: the orchestrator crashed
mid-exec, or the backend's ``destroy`` raised. Nothing listed those.

The sweep pairs the audit records and asks the task store - not a liveness
probe - whether the task the session was provisioned for is finished. Only a
session whose every owning task is recorded terminal is listed as leaked;
everything else is reported with the reason it was left out.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.sandbox_cmd import sandbox_group
from bernstein.core.sandbox.leak_sweep import (
    SandboxLeakVerdict,
    SandboxSweepUnavailableError,
    sweep_leaked_sandboxes,
)
from bernstein.core.security.audit import (
    SANDBOX_SESSION_CREATE,
    SANDBOX_SESSION_DESTROY,
    AuditLog,
    load_or_create_audit_key,
)
from bernstein.core.tasks.models import TaskStatus

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    key_path = tmp_path / "keys" / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    return load_or_create_audit_key(key_path)


def _audit(workdir: Path, key: bytes) -> AuditLog:
    return AuditLog(audit_dir=workdir / ".sdd" / "audit", key=key)


def _record_create(
    workdir: Path,
    key: bytes,
    sandbox_id: str,
    *,
    agent_session_id: str = "backend-1",
    task_ids: list[str] | None = None,
    backend: str = "docker",
) -> None:
    """Chain a ``sandbox.session_create`` shaped the way the spawner writes it."""
    details: dict[str, object] = {
        "session_id": sandbox_id,
        "image": "bernstein-agent:latest",
        "backend": backend,
        "agent_session_id": agent_session_id,
    }
    if task_ids is not None:
        details["task_ids"] = task_ids
    _audit(workdir, key).log(
        event_type=SANDBOX_SESSION_CREATE,
        actor="spawner",
        resource_type="sandbox_session",
        resource_id=sandbox_id,
        details=details,
    )


def _record_destroy(workdir: Path, key: bytes, sandbox_id: str, *, agent_session_id: str = "backend-1") -> None:
    _audit(workdir, key).log(
        event_type=SANDBOX_SESSION_DESTROY,
        actor="spawner",
        resource_type="sandbox_session",
        resource_id=sandbox_id,
        details={"session_id": sandbox_id, "agent_session_id": agent_session_id},
    )


def _record_task(workdir: Path, task_id: str, status: str) -> None:
    """Append a task record to the live task log, the way ``TaskStore`` does."""
    log = workdir / ".sdd" / "runtime" / "tasks.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": task_id, "title": f"task {task_id}", "description": "", "role": "backend", "status": status}
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _by_id(candidates: list) -> dict[str, object]:
    return {c.session.sandbox_session_id: c for c in candidates}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_sweep_lists_sandbox_whose_task_is_terminal(tmp_path: Path, audit_key: bytes) -> None:
    """Load-bearing: nothing on ``main`` lists a sandbox whose task finished."""
    _record_task(tmp_path, "T-1", "open")
    _record_task(tmp_path, "T-1", "in_progress")
    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-leaked", task_ids=["T-1"])

    candidates = sweep_leaked_sandboxes(tmp_path)

    assert len(candidates) == 1
    leaked = candidates[0]
    assert leaked.verdict is SandboxLeakVerdict.LEAKED
    assert leaked.is_leaked
    assert leaked.session.sandbox_session_id == "sbx-leaked"
    assert leaked.session.backend == "docker"
    assert leaked.session.task_ids == ("T-1",)
    assert leaked.task_statuses == {"T-1": "done"}
    assert "T-1" in leaked.reason
    assert "done" in leaked.reason


@pytest.mark.parametrize("status", ["done", "failed", "closed", "cancelled", "abandoned", "refused"])
def test_every_terminal_status_counts(tmp_path: Path, audit_key: bytes, status: str) -> None:
    _record_task(tmp_path, "T-1", status)
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert candidate.is_leaked


def test_destroyed_session_is_not_listed(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-clean", task_ids=["T-1"])
    _record_destroy(tmp_path, audit_key, "sbx-clean")

    assert sweep_leaked_sandboxes(tmp_path) == []


@pytest.mark.parametrize("status", ["open", "claimed", "in_progress", "orphaned", "suspended", "blocked"])
def test_active_task_is_excluded_with_its_status(tmp_path: Path, audit_key: bytes, status: str) -> None:
    """A task that can still run keeps its sandbox, whatever a liveness probe says."""
    _record_task(tmp_path, "T-1", status)
    _record_create(tmp_path, audit_key, "sbx-live", task_ids=["T-1"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert candidate.verdict is SandboxLeakVerdict.EXCLUDED
    assert not candidate.is_leaked
    assert status in candidate.reason


def test_reopened_task_is_not_leaked(tmp_path: Path, audit_key: bytes) -> None:
    """The latest task record wins: a ``done`` task reopened to ``open`` is live again."""
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-1", "open")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert not candidate.is_leaked
    assert candidate.task_statuses == {"T-1": "open"}


def test_batch_with_one_unfinished_task_is_excluded(tmp_path: Path, audit_key: bytes) -> None:
    """A batch spawn owns its sandbox until *every* task in it is finished."""
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-2", "in_progress")
    _record_create(tmp_path, audit_key, "sbx-batch", task_ids=["T-1", "T-2"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert not candidate.is_leaked
    assert "T-2" in candidate.reason
    assert candidate.task_statuses == {"T-1": "done", "T-2": "in_progress"}


def test_session_without_recorded_owner_is_excluded(tmp_path: Path, audit_key: bytes) -> None:
    """Create records written before the owner was audited carry no task ids."""
    _record_create(tmp_path, audit_key, "sbx-legacy", task_ids=None)

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert not candidate.is_leaked
    assert "owning task" in candidate.reason


@pytest.mark.parametrize("task_ids", [[], [""], "T-1", [1, 2], [None]])
def test_malformed_owner_is_treated_as_unrecorded(tmp_path: Path, audit_key: bytes, task_ids: object) -> None:
    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-odd", task_ids=task_ids)  # type: ignore[arg-type]

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert not candidate.is_leaked


def test_task_missing_from_the_store_is_excluded(tmp_path: Path, audit_key: bytes) -> None:
    """No record is "unknown", never "finished"."""
    _record_task(tmp_path, "T-other", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-gone"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert not candidate.is_leaked
    assert candidate.task_statuses == {"T-gone": None}
    assert "T-gone" in candidate.reason


def test_no_task_log_at_all_excludes_everything(tmp_path: Path, audit_key: bytes) -> None:
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert not candidate.is_leaked


def test_the_finished_set_is_every_no_exit_status_plus_done_and_failed() -> None:
    """Pinned so a status added to the FSM is classified on purpose, not by accident."""
    from bernstein.core.tasks.lifecycle import LANE_FINISHED_STATUSES, TERMINAL_TASK_STATUSES
    from bernstein.core.tasks.models import TaskStatus

    assert {s.value for s in LANE_FINISHED_STATUSES} == {
        "abandoned",
        "cancelled",
        "closed",
        "done",
        "failed",
        "refused",
    }
    assert TERMINAL_TASK_STATUSES | {TaskStatus.DONE, TaskStatus.FAILED} == LANE_FINISHED_STATUSES


@pytest.mark.parametrize("status", sorted(s.value for s in TaskStatus))
def test_a_session_is_leaked_exactly_when_its_task_is_finished(tmp_path: Path, audit_key: bytes, status: str) -> None:
    """Every status the task store can record, one at a time."""
    from bernstein.core.tasks.lifecycle import LANE_FINISHED_STATUSES

    _record_task(tmp_path, "T-1", status)
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    finished = status in {s.value for s in LANE_FINISHED_STATUSES}
    assert candidate.is_leaked is finished, candidate.reason


def _tear_task_log(workdir: Path, *, at: int) -> Path:
    """Cut line *at* (1-based) of the task log in half, as an interleaved append can."""
    log = workdir / ".sdd" / "runtime" / "tasks.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[at - 1] = lines[at - 1][: len(lines[at - 1]) // 2] + "\n"
    log.write_text("".join(lines), encoding="utf-8")
    return log


@pytest.mark.parametrize("torn_line", [2, 3], ids=["torn-last", "torn-middle"])
def test_a_torn_task_record_fails_closed(tmp_path: Path, audit_key: bytes, torn_line: int) -> None:
    """A torn reopen would leave the task reading `done`: the sweep must not guess.

    The task store skips a line it cannot parse and replays the rest, so with
    the reopen torn the task reads back as `done` and its live session would be
    listed as leaked - the listing the docs tell an operator to `docker rm` on.
    """
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-1", "open")  # the reopen
    if torn_line == 3:
        _record_task(tmp_path, "T-2", "done")
    log = _tear_task_log(tmp_path, at=2)
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    with pytest.raises(SandboxSweepUnavailableError, match=r"tasks\.jsonl:2: task record does not parse"):
        sweep_leaked_sandboxes(tmp_path)
    assert "tasks.jsonl" in str(log)


def test_the_task_archive_feeds_neither_replay_nor_the_torn_line_scan(tmp_path: Path, audit_key: bytes) -> None:
    """The torn-line scan covers exactly the file replay reads: the live log.

    ``replay_jsonl`` never opens ``archive/tasks.jsonl``, so a torn archive line
    cannot revert a status and the scan rightly ignores it. This pins that
    coupling: if replay ever starts reading the archive, the archived record
    below would be found (and the torn line would matter), this test fails, and
    ``_check_task_log_is_whole`` has to widen with it.
    """
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive.parent.mkdir(parents=True)
    archived = {"id": "T-arch", "title": "t", "description": "", "role": "backend", "status": "done"}
    archive.write_text(json.dumps(archived) + "\n" + json.dumps(archived)[:20] + "\n", encoding="utf-8")
    _record_task(tmp_path, "T-live", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-arch"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert not candidate.is_leaked
    assert candidate.task_statuses == {"T-arch": None}


def test_blank_task_log_lines_are_not_torn(tmp_path: Path, audit_key: bytes) -> None:
    """Whitespace-only lines are skipped by replay, so they must not brick the sweep."""
    _record_task(tmp_path, "T-1", "done")
    log = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    log.write_text(log.read_text(encoding="utf-8") + "\n   \n", encoding="utf-8")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert candidate.is_leaked


def test_a_task_record_that_is_not_an_object_fails_closed(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-1", "done")
    log = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    log.write_text(log.read_text(encoding="utf-8") + '["T-1", "open"]\n', encoding="utf-8")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    with pytest.raises(SandboxSweepUnavailableError, match="tasks.jsonl:2: task record is not a JSON object"):
        sweep_leaked_sandboxes(tmp_path)


def test_an_unreadable_archived_audit_segment_fails_closed(tmp_path: Path, audit_key: bytes) -> None:
    """A lost segment may hold the destroy, which would make a destroyed session look leaked."""
    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    archive = tmp_path / ".sdd" / "audit" / "archive"
    archive.mkdir(parents=True)
    (archive / "2026-01-01.jsonl.gz").write_bytes(b"\x1f\x8b not a gzip stream")

    with pytest.raises(SandboxSweepUnavailableError, match=r"2026-01-01\.jsonl\.gz"):
        sweep_leaked_sandboxes(tmp_path)


def test_query_reports_the_segments_it_could_not_read(tmp_path: Path, audit_key: bytes) -> None:
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    archive = tmp_path / ".sdd" / "audit" / "archive"
    archive.mkdir(parents=True)
    (archive / "2026-01-01.jsonl.gz").write_bytes(b"\x1f\x8b not a gzip stream")
    unreadable: list[str] = []

    events = _audit(tmp_path, audit_key).query(include_archived=True, unreadable=unreadable)

    assert [e.resource_id for e in events] == ["sbx-1"]
    assert len(unreadable) == 1
    assert unreadable[0].startswith("2026-01-01.jsonl.gz: unreadable archived segment")
    # Without the list, the same query behaves exactly as before.
    assert [e.resource_id for e in _audit(tmp_path, audit_key).query(include_archived=True)] == ["sbx-1"]


def test_unreadable_task_log_fails_closed(tmp_path: Path, audit_key: bytes) -> None:
    """A status the store cannot parse stops the sweep rather than guessing."""
    _record_task(tmp_path, "T-1", "not-a-real-status")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    with pytest.raises(SandboxSweepUnavailableError, match="task"):
        sweep_leaked_sandboxes(tmp_path)


def test_recreated_session_id_after_destroy_is_open_again(tmp_path: Path, audit_key: bytes) -> None:
    """Records pair in chain order, so an id a backend reuses is tracked per lifetime."""
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-2", "done")
    _record_create(tmp_path, audit_key, "sbx-reused", task_ids=["T-1"])
    _record_destroy(tmp_path, audit_key, "sbx-reused")
    _record_create(tmp_path, audit_key, "sbx-reused", task_ids=["T-2"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert candidate.session.task_ids == ("T-2",)


def test_recreated_session_id_without_a_destroy_keeps_both_lifetimes(tmp_path: Path, audit_key: bytes) -> None:
    """A second create for an open id must not make the first lifetime disappear.

    The earlier lifetime is never listed as leaked: its id now names the newer
    session, so acting on it by id would reach the wrong one. It is excluded
    with its reason instead, and the newer lifetime is judged on its own tasks.
    """
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-2", "done")
    _record_create(tmp_path, audit_key, "sbx-reused", task_ids=["T-1"])
    _record_create(tmp_path, audit_key, "sbx-reused", task_ids=["T-2"])

    candidates = sweep_leaked_sandboxes(tmp_path)

    assert [(c.session.task_ids, c.verdict) for c in candidates] == [
        (("T-2",), SandboxLeakVerdict.LEAKED),
        (("T-1",), SandboxLeakVerdict.EXCLUDED),
    ]
    assert "created again before this lifetime was destroyed" in candidates[1].reason


def test_a_destroy_after_an_id_is_reused_closes_only_the_newest_lifetime(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-2", "done")
    _record_create(tmp_path, audit_key, "sbx-reused", task_ids=["T-1"])
    _record_create(tmp_path, audit_key, "sbx-reused", task_ids=["T-2"])
    _record_destroy(tmp_path, audit_key, "sbx-reused")

    (candidate,) = sweep_leaked_sandboxes(tmp_path)

    assert candidate.session.task_ids == ("T-1",)
    assert candidate.verdict is SandboxLeakVerdict.EXCLUDED


def test_age_is_measured_from_the_create_record(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    (candidate,) = sweep_leaked_sandboxes(tmp_path, now=time.time() + 3600)

    assert candidate.age_seconds is not None
    assert 3590 <= candidate.age_seconds <= 3700


def test_leaked_sessions_sort_ahead_of_excluded_ones(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-live", "in_progress")
    _record_task(tmp_path, "T-done", "done")
    _record_create(tmp_path, audit_key, "sbx-a-live", task_ids=["T-live"])
    _record_create(tmp_path, audit_key, "sbx-b-done", task_ids=["T-done"])

    candidates = sweep_leaked_sandboxes(tmp_path)

    assert [c.session.sandbox_session_id for c in candidates] == ["sbx-b-done", "sbx-a-live"]


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def test_project_without_an_audit_log_has_nothing_to_list(tmp_path: Path) -> None:
    """No audit directory means no sandbox was ever provisioned here - and none is created."""
    assert sweep_leaked_sandboxes(tmp_path) == []
    assert not (tmp_path / ".sdd").exists()


def test_sweep_never_writes(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-2", "in_progress")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    _record_create(tmp_path, audit_key, "sbx-2", task_ids=["T-2"])
    before = _tree(tmp_path)

    sweep_leaked_sandboxes(tmp_path)
    sweep_leaked_sandboxes(tmp_path)

    assert _tree(tmp_path) == before


def test_missing_audit_key_is_reported_not_minted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only sweep must not create key material it would then disagree with."""
    writer_key = load_or_create_audit_key(tmp_path / "writer.key")
    _record_create(tmp_path, writer_key, "sbx-1", task_ids=["T-1"])
    missing = tmp_path / "nowhere" / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(missing))

    with pytest.raises(SandboxSweepUnavailableError, match="audit"):
        sweep_leaked_sandboxes(tmp_path)
    assert not missing.exists()


# ---------------------------------------------------------------------------
# CLI: ``bernstein sandbox leaks``
# ---------------------------------------------------------------------------


def test_cli_lists_leaked_sessions_and_hides_excluded_by_default(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-done", "done")
    _record_task(tmp_path, "T-live", "in_progress")
    _record_create(tmp_path, audit_key, "sbx-leaked", task_ids=["T-done"])
    _record_create(tmp_path, audit_key, "sbx-live", task_ids=["T-live"])

    result = CliRunner().invoke(sandbox_group, ["leaks", "--workdir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "sbx-leaked" in result.output
    assert "T-done" in result.output
    assert "sbx-live" not in result.output
    assert "1 leaked" in result.output


def test_cli_all_shows_excluded_sessions_with_their_reason(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-live", "in_progress")
    _record_create(tmp_path, audit_key, "sbx-live", task_ids=["T-live"])

    result = CliRunner().invoke(sandbox_group, ["leaks", "--workdir", str(tmp_path), "--all"])

    assert result.exit_code == 0, result.output
    assert "sbx-live" in result.output
    assert "in_progress" in result.output


def test_cli_json_output(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-done", "done")
    _record_task(tmp_path, "T-live", "in_progress")
    _record_create(tmp_path, audit_key, "sbx-leaked", task_ids=["T-done"])
    _record_create(tmp_path, audit_key, "sbx-live", task_ids=["T-live"])

    result = CliRunner().invoke(sandbox_group, ["leaks", "--workdir", str(tmp_path), "--json", "--all"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [(r["sandbox_session_id"], r["verdict"]) for r in rows] == [
        ("sbx-leaked", "leaked"),
        ("sbx-live", "excluded"),
    ]
    assert rows[0]["task_ids"] == ["T-done"]
    assert rows[0]["task_statuses"] == {"T-done": "done"}
    assert rows[0]["backend"] == "docker"


def test_cli_invocation_touches_no_disk(tmp_path: Path, audit_key: bytes) -> None:
    """An unqualified invocation is a listing: it neither destroys nor records anything."""
    _record_task(tmp_path, "T-done", "done")
    _record_create(tmp_path, audit_key, "sbx-leaked", task_ids=["T-done"])
    before = _tree(tmp_path)

    result = CliRunner().invoke(sandbox_group, ["leaks", "--workdir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert _tree(tmp_path) == before


def test_cli_reports_an_unreadable_task_log_as_an_error(tmp_path: Path, audit_key: bytes) -> None:
    _record_task(tmp_path, "T-1", "not-a-real-status")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])

    result = CliRunner().invoke(sandbox_group, ["leaks", "--workdir", str(tmp_path)])

    assert result.exit_code != 0
    assert "sbx-1" not in result.output


@pytest.mark.parametrize("record", [{}, {"id": None}, {"id": ""}, {"id": 0}, {"id": False}])
def test_skipped_task_records_cannot_replay_an_older_finished_status(
    tmp_path: Path, audit_key: bytes, record: dict[str, object]
) -> None:
    """Replay ignores an id-less record, so it cannot safely override an earlier done."""
    from bernstein.core.tasks.task_store import TaskStore

    _record_task(tmp_path, "T-1", "done")
    log = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({**record, "status": "open"}) + "\n")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    before = _tree(tmp_path)
    replayed = TaskStore(log)
    replayed.replay_jsonl()
    task = replayed.get_task("T-1")
    assert task is not None and task.status is TaskStatus.DONE

    with pytest.raises(SandboxSweepUnavailableError, match=r"tasks\.jsonl:2: task record has no id"):
        sweep_leaked_sandboxes(tmp_path)

    assert _tree(tmp_path) == before
