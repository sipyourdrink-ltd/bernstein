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
    EVENT_CHAIN_TEAR_ACKNOWLEDGED,
    SANDBOX_SESSION_CREATE,
    SANDBOX_SESSION_DESTROY,
    AuditLog,
    load_or_create_audit_key,
)
from bernstein.core.tasks.models import TaskStatus

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit import ChainVerifyReport


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


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("valid", None),
        ("valid-reused", None),
        ("invalid-hmac", "audit verification failed"),
        ("acknowledged-invalid-hmac", "audit contains tear evidence"),
        ("unreadable-archive", "2026-01-01.jsonl.gz"),
        ("torn-task-log", "tasks.jsonl:3: task record does not parse"),
        ("reopened", "T-1 is open"),
        ("excluded", "create record names no owning task"),
        ("superseded", "superseded"),
        ("ambiguous", "ambiguous"),
        ("destroyed", "no open lifetime"),
        ("unknown-hmac", "no open lifetime"),
    ],
)
def test_removal_proposal_requires_verified_leaked_lifetime(
    tmp_path: Path, audit_key: bytes, monkeypatch: pytest.MonkeyPatch, case: str, reason: str | None
) -> None:
    from bernstein.core.sandbox import leak_sweep

    prepare = getattr(leak_sweep, "prepare_sandbox_removal_proposal", None)
    assert callable(prepare), "a verified removal proposal API is required"
    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-2", "refused")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=None if case == "excluded" else ["T-1", "T-2"])
    create = _audit(tmp_path, audit_key).query(event_type=SANDBOX_SESSION_CREATE)[0]
    selected_hmac = create.hmac
    if case in {"invalid-hmac", "acknowledged-invalid-hmac"}:
        segment = next((tmp_path / ".sdd" / "audit").glob("*.jsonl"))
        record = json.loads(segment.read_text())
        record["hmac"] = "f" * 64
        if case == "invalid-hmac":
            segment.write_text(json.dumps(record) + "\n")
        else:
            record["prev_hmac"] = create.hmac
            with segment.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            selected_hmac = record["hmac"]
            log = _audit(tmp_path, audit_key)
            log.log("test.event", "tester", "task", "after-tear")
            (tear,) = log.verify_detailed().tears
            log.log(
                EVENT_CHAIN_TEAR_ACKNOWLEDGED,
                "operator",
                "audit_segment",
                tear.segment,
                {"segment": tear.segment, "byte_offset": tear.byte_offset, "reason": "investigated"},
            )
            assert log.verify_detailed().ok
    elif case == "unreadable-archive":
        archive = tmp_path / ".sdd" / "audit" / "archive"
        archive.mkdir()
        (archive / "2026-01-01.jsonl.gz").write_bytes(b"\x1f\x8b not a gzip stream")
    elif case in {"torn-task-log", "reopened"}:
        _record_task(tmp_path, "T-1", "open")
        if case == "torn-task-log":
            _tear_task_log(tmp_path, at=3)
    elif case in {"superseded", "ambiguous", "valid-reused"}:
        if case == "valid-reused":
            _record_destroy(tmp_path, audit_key, "sbx-1")
        _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-2"], backend="e2b", agent_session_id="agent-2")
        if case == "ambiguous":
            selected_hmac = None
        elif case == "valid-reused":
            create = _audit(tmp_path, audit_key).query(event_type=SANDBOX_SESSION_CREATE)[-1]
            selected_hmac = create.hmac
    elif case == "destroyed":
        _record_destroy(tmp_path, audit_key, "sbx-1")
    elif case == "unknown-hmac":
        selected_hmac = "0" * 64

    verified: list[bool] = []
    original_verify = AuditLog.verify_detailed

    def verify(log: AuditLog, *, include_events: bool = False) -> ChainVerifyReport:
        report = original_verify(log, include_events=include_events)
        verified.append(report.ok)
        return report

    monkeypatch.setattr(AuditLog, "verify_detailed", verify)
    before = _tree(tmp_path)
    if reason is not None:
        with pytest.raises((SandboxSweepUnavailableError, ValueError), match=reason):
            prepare(tmp_path, "sbx-1", create_hmac=selected_hmac)
    else:
        proposal = prepare(tmp_path, "sbx-1", create_hmac=selected_hmac)
        assert proposal.create_hmac == create.hmac
        assert proposal.backend == create.details["backend"]
        assert proposal.sandbox_session_id == create.resource_id
        assert proposal.agent_session_id == create.details["agent_session_id"]
        assert proposal.task_ids == tuple(create.details["task_ids"])
        (candidate,) = sweep_leaked_sandboxes(tmp_path)
        assert candidate.is_leaked
        assert candidate.session.create_hmac == proposal.create_hmac
    assert verified == [case not in {"invalid-hmac", "unreadable-archive"}]
    assert _tree(tmp_path) == before, "proposal preparation must not write anything"


@pytest.mark.parametrize("archived", [False, True], ids=["live", "archived"])
def test_removal_proposal_uses_verified_snapshot(
    tmp_path: Path, audit_key: bytes, monkeypatch: pytest.MonkeyPatch, archived: bool
) -> None:
    import gzip

    from bernstein.core.sandbox.leak_sweep import prepare_sandbox_removal_proposal

    _record_task(tmp_path, "T-1", "done")
    _record_task(tmp_path, "T-2", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    create = _audit(tmp_path, audit_key).query()[0]
    segment = next((tmp_path / ".sdd" / "audit").glob("*.jsonl"))
    if archived:
        archive = segment.parent / "archive" / f"{segment.name}.gz"
        archive.parent.mkdir()
        archive.write_bytes(gzip.compress(segment.read_bytes()))
        segment.unlink()
        segment = archive

    original_verify = AuditLog.verify_detailed
    verified: list[bool] = []

    def verify_then_change(log: AuditLog, **kwargs: bool) -> ChainVerifyReport:
        report = original_verify(log, **kwargs)
        verified.append(report.ok)
        raw = gzip.decompress(segment.read_bytes()) if archived else segment.read_bytes()
        record = json.loads(raw)
        record["details"].update(backend="e2b", agent_session_id="unverified-agent", task_ids=["T-2"])
        changed = (json.dumps(record) + "\n").encode()
        segment.write_bytes(gzip.compress(changed) if archived else changed)
        return report

    monkeypatch.setattr(AuditLog, "verify_detailed", verify_then_change)
    proposal = prepare_sandbox_removal_proposal(tmp_path, "sbx-1", create_hmac=create.hmac)

    assert verified == [True]
    assert proposal.create_hmac == create.hmac
    assert proposal.sandbox_session_id == create.resource_id
    assert proposal.backend == create.details["backend"]
    assert proposal.agent_session_id == create.details["agent_session_id"]
    assert proposal.task_ids == tuple(create.details["task_ids"])
    assert not original_verify(_audit(tmp_path, audit_key)).ok, "the changed record must fail authentication"


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


# Unified task-authoritative lane sweep and gated removal.


def _lane_api(name: str):
    from bernstein.core.sandbox import leak_sweep

    if name == "remove_leaked_lanes":
        from bernstein.cli.commands import worktrees_cmd

        api = getattr(worktrees_cmd, name, None)
    else:
        api = getattr(leak_sweep, name, None)
    assert callable(api), f"task-authoritative lane sweep requires {name}"
    return api


@pytest.fixture
def live_worktree(tmp_path: Path) -> Path:
    import os
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.org",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-m",
        "seed",
    )
    wt = tmp_path / ".sdd/runtime/worktrees/live"
    wt.parent.mkdir(parents=True)
    git("worktree", "add", "-b", "lane", str(wt), "main")
    pids = tmp_path / ".sdd/runtime/pids"
    pids.mkdir()
    (pids / "live.json").write_text(json.dumps({"worker_pid": os.getpid(), "task_id": "T-1"}))
    _record_task(tmp_path, "T-1", "done")
    return wt


def _removal_approval(lanes):
    from dataclasses import replace
    from datetime import UTC, datetime

    from bernstein.core.security.dual_approval import (
        ApprovalChannel,
        ApprovalResponse,
        create_approval_request,
        evaluate_approval,
    )

    operation = _lane_api("lane_removal_operation")(lanes)
    request = create_approval_request(operation, "test", channels=[ApprovalChannel.CLI])
    request = replace(request, reason="removal")
    responses = [
        ApprovalResponse(request.request_id, ApprovalChannel.CLI, who, True, datetime.now(UTC).isoformat())
        for who in ("owner", "reviewer")
    ]
    return evaluate_approval(request, responses)


def test_sweep_lists_worktree_whose_task_is_terminal(tmp_path: Path, live_worktree: Path) -> None:
    from bernstein.core.worktrees.classifier import WorktreeState, classify_worktrees

    original = classify_worktrees(tmp_path)[0]
    assert original.pid_alive and original.state is WorktreeState.ACTIVE
    assert not original.is_reapable
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    assert len(lanes) == 1
    assert lanes[0].kind == "worktree"
    assert lanes[0].task_ids == ("T-1",)
    assert lanes[0].path == live_worktree
    assert lanes[0].size_bytes > 0
    assert lanes[0].age_seconds >= 0


@pytest.mark.parametrize("status", ["in_progress", "open", "suspended", "orphaned"])
def test_unfinished_worktree_is_excluded(tmp_path: Path, live_worktree: Path, status: str) -> None:
    _record_task(tmp_path, "T-1", status)
    assert _lane_api("sweep_leaked_lanes")(tmp_path) == []


@pytest.mark.parametrize("group", ["worktrees", "sandbox"])
def test_dry_run_is_the_default_with_no_flags(
    tmp_path: Path, audit_key: bytes, live_worktree: Path, group: str
) -> None:
    from bernstein.cli.commands.worktrees_cmd import worktrees_group

    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    before = _tree(tmp_path)
    command = worktrees_group if group == "worktrees" else sandbox_group
    result = CliRunner().invoke(command, ["sweep", "--workdir", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert {r["kind"] for r in rows} == {"worktree", "sandbox"}
    assert all("size_bytes" in r and "age_seconds" in r for r in rows)
    assert _tree(tmp_path) == before


def test_removal_is_journaled_with_task_id(tmp_path: Path, audit_key: bytes, dead_worktree: Path) -> None:
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    remove = _lane_api("remove_leaked_lanes")
    with pytest.raises(PermissionError, match="removal"):
        remove(tmp_path, lanes)
    assert dead_worktree.exists()
    approval = _removal_approval(lanes)
    assert remove(tmp_path, lanes, removal_approval=approval) == 1
    assert not dead_worktree.exists()
    events = [e for e in _audit(tmp_path, audit_key).query() if e.event_type == "lane.remove"]
    assert events
    assert all(e.details["task_ids"] == ["T-1"] for e in events)
    assert all(e.details["approval_class"] == "removal" for e in events)
    assert all(e.details["removal_approval_id"] == approval.request.request_id for e in events)
    assert events[-1].details["removed"] is True
    assert _audit(tmp_path, audit_key).verify()[0]


def test_unsaved_terminal_worktree_is_preserved(tmp_path: Path, audit_key: bytes, dead_worktree: Path) -> None:
    (dead_worktree / "unsaved.txt").write_text("only copy")
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    assert len(lanes) == 1
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes)) == 0
    assert (dead_worktree / "unsaved.txt").read_text() == "only copy"


def test_reopened_task_is_rechecked_before_removal(tmp_path: Path, audit_key: bytes, live_worktree: Path) -> None:
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    _record_task(tmp_path, "T-1", "open")
    with pytest.raises(RuntimeError, match="changed"):
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=approval)
    assert live_worktree.exists()


@pytest.mark.parametrize("kind", ["worktree", "sandbox"])
def test_doctor_flags_a_leak_older_than_threshold(
    tmp_path: Path,
    audit_key: bytes,
    live_worktree: Path,
    kind: str,
) -> None:
    from bernstein.cli.commands import doctor_cmd

    check = getattr(doctor_cmd, "check_leaked_lanes", None)
    assert callable(check), "doctor must check task-terminal lane leaks"
    if kind == "sandbox":
        _record_task(tmp_path, "T-1", "open")
        _record_task(tmp_path, "T-2", "done")
        _record_create(tmp_path, audit_key, "sbx-old", task_ids=["T-2"])
    assert check(tmp_path, max_age_seconds=7200, now=time.time() + 3600)["status"] == "PASS"
    finding = check(tmp_path, max_age_seconds=60, now=time.time() + 3600)
    assert finding["status"] == "WARN"
    assert kind in finding["detail"]
    assert "sweep" in finding["fix"]


def test_worktree_gc_default_writes_nothing(tmp_path: Path, audit_key: bytes, live_worktree: Path) -> None:
    from bernstein.cli.commands.worktrees_cmd import worktrees_group

    (tmp_path / ".sdd/runtime/pids/live.json").unlink()
    before = _tree(tmp_path)
    result = CliRunner().invoke(worktrees_group, ["gc", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("defect", ["wrong-plan", "denied", "expired", "same-person", "zero-quorum"])
def test_removal_gate_refuses_invalid_approval(
    tmp_path: Path,
    audit_key: bytes,
    live_worktree: Path,
    defect: str,
) -> None:
    from dataclasses import replace

    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    if defect == "wrong-plan":
        approval = replace(approval, request=replace(approval.request, operation="something else"))
    elif defect == "denied":
        approval = replace(approval, responses=[replace(r, approved=False) for r in approval.responses])
    elif defect == "expired":
        approval = replace(approval, request=replace(approval.request, expires_at="2000-01-01T00:00:00+00:00"))
    elif defect == "same-person":
        approval = replace(approval, responses=[approval.responses[0], approval.responses[0]])
    else:
        approval = replace(approval, request=replace(approval.request, required_approvals=0), responses=[])
    with pytest.raises(PermissionError, match="removal"):
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=approval)
    assert live_worktree.exists()


def test_sandbox_is_measured_but_never_removed_with_valid_approval(
    tmp_path: Path,
    audit_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from bernstein.core.sandbox import registry
    from bernstein.core.sandbox.backends.docker import DockerSandboxBackend

    container = MagicMock()
    container.attrs = {"Config": {"Labels": {"bernstein.sandbox": "docker", "bernstein.sandbox.session_id": "sbx-1"}}}
    client = MagicMock()
    client.containers.get.return_value = container
    client.api.inspect_container.return_value = {"SizeRw": 8192}
    backend = DockerSandboxBackend(client=client)
    monkeypatch.setattr(registry, "get_backend", lambda name: backend)
    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path, measure_disk=True)
    assert lanes[0].size_bytes == 8192
    container.remove.assert_not_called()
    outcomes = []
    assert (
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes), outcomes=outcomes)
        == 0
    )
    container.remove.assert_not_called()
    container.stop.assert_not_called()
    client.containers.get.assert_called_once_with("bernstein-sbx-1")
    assert sweep_leaked_sandboxes(tmp_path)[0].is_leaked
    assert outcomes == [{"removal_status": "report_only", "removal_reason": "report only (removal: #6571)"}]
    entries = [
        e for e in _audit(tmp_path, audit_key).query() if e.event_type in {"lane.remove", SANDBOX_SESSION_DESTROY}
    ]
    assert entries == []


def test_docker_refuses_a_container_with_different_owner() -> None:
    from unittest.mock import MagicMock

    from bernstein.core.sandbox.backends.docker import DockerSandboxBackend

    client = MagicMock()
    client.containers.get.return_value.attrs = {"Config": {"Labels": {"bernstein.sandbox.session_id": "other"}}}
    with pytest.raises(ValueError, match="ownership"):
        DockerSandboxBackend(client=client).leaked_session_size_bytes("sbx-1")
    client.containers.get.return_value.remove.assert_not_called()


def test_audit_failure_prevents_deletion(
    tmp_path: Path, audit_key: bytes, live_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)

    def fail(*args, **kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr(AuditLog, "log", fail)
    with pytest.raises(OSError, match="journal unavailable"):
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=approval)
    assert live_worktree.exists()


def test_cli_applies_exact_removal_plan(tmp_path: Path, audit_key: bytes, dead_worktree: Path) -> None:
    from dataclasses import asdict

    from bernstein.cli.commands.worktrees_cmd import worktrees_group

    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps({"request": asdict(approval.request), "responses": [asdict(r) for r in approval.responses]})
    )
    command = ["sweep", "--workdir", str(tmp_path), "--apply", "--removal-approval", str(approval_path)]
    preview = CliRunner().invoke(worktrees_group, [*command, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert dead_worktree.exists()
    result = CliRunner().invoke(worktrees_group, command)
    assert result.exit_code == 0, result.output
    assert not dead_worktree.exists()


def test_unknown_worktree_owner_is_excluded(tmp_path: Path, live_worktree: Path) -> None:
    (tmp_path / ".sdd/runtime/tasks.jsonl").unlink()
    assert _lane_api("sweep_leaked_lanes")(tmp_path) == []


def test_unsupported_backend_is_report_only_without_lookup(
    tmp_path: Path,
    audit_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bernstein.core.sandbox import registry

    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"], backend="custom")

    def refuse_lookup(name):
        pytest.fail("report-only sandbox must not attach to a backend")

    monkeypatch.setattr(registry, "get_backend", refuse_lookup)
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes)) == 0
    assert sweep_leaked_sandboxes(tmp_path)[0].is_leaked


def test_unverified_source_audit_prevents_removal(tmp_path: Path, audit_key: bytes, live_worktree: Path) -> None:
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    audit_file = next((tmp_path / ".sdd/audit").glob("*.jsonl"))
    audit_file.write_text(audit_file.read_text().replace('"docker"', '"tampered"'))
    with pytest.raises(SandboxSweepUnavailableError, match="unverified audit chain"):
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=approval)
    assert live_worktree.exists()


def test_doctor_uses_configured_leak_age(tmp_path: Path, live_worktree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.commands.doctor_cmd import check_leaked_lanes

    monkeypatch.setenv("BERNSTEIN_LEAK_MAX_AGE_SECONDS", "60")
    assert check_leaked_lanes(tmp_path, now=time.time() + 3600)["status"] == "WARN"
    monkeypatch.setenv("BERNSTEIN_LEAK_MAX_AGE_SECONDS", "7200")
    assert check_leaked_lanes(tmp_path, now=time.time() + 3600)["status"] == "PASS"


def test_legacy_gc_accepts_dry_run_alias(tmp_path: Path, audit_key: bytes, live_worktree: Path) -> None:
    from bernstein.cli.commands.worktrees_cmd import worktrees_group

    (tmp_path / ".sdd/runtime/pids/live.json").unlink()
    before = _tree(tmp_path)
    result = CliRunner().invoke(worktrees_group, ["gc", "--workdir", str(tmp_path), "--yes", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert _tree(tmp_path) == before


def test_sandbox_teardown_is_never_attempted(
    tmp_path: Path,
    audit_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from bernstein.core.sandbox import registry

    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    backend = MagicMock()
    backend.destroy_leaked_session.side_effect = OSError("teardown failed")
    monkeypatch.setattr(registry, "get_backend", lambda name: backend)
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes)) == 0
    backend.destroy_leaked_session.assert_not_called()
    assert sweep_leaked_sandboxes(tmp_path)[0].is_leaked
    assert not any(e.event_type == "lane.remove" for e in _audit(tmp_path, audit_key).query())


def test_sandbox_create_evidence_is_retained_but_not_bound_to_removal(tmp_path: Path, audit_key: bytes) -> None:
    from dataclasses import replace

    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    (lane,) = _lane_api("sweep_leaked_lanes")(tmp_path)
    create = _audit(tmp_path, audit_key).query()[0]
    assert lane.create_hmac == create.hmac
    changed = replace(lane, create_hmac="different-lifetime")
    operation = _lane_api("lane_removal_operation")
    assert operation([lane]) == operation([changed]) == operation([])


def test_removal_does_not_invoke_sandbox_proposal(
    tmp_path: Path, audit_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from bernstein.core.sandbox import leak_sweep, registry

    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    backend = MagicMock()
    monkeypatch.setattr(registry, "get_backend", lambda name: backend)

    def refuse_proposal(workdir, sandbox_session_id, *, create_hmac):
        assert workdir == tmp_path
        assert sandbox_session_id == "sbx-1"
        assert create_hmac == _audit(tmp_path, audit_key).query()[0].hmac
        raise SandboxSweepUnavailableError("verified proposal refused")

    monkeypatch.setattr(leak_sweep, "prepare_sandbox_removal_proposal", refuse_proposal)
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes)) == 0
    backend.destroy_leaked_session.assert_not_called()
    assert not any(e.event_type == "lane.remove" for e in _audit(tmp_path, audit_key).query())


def test_acknowledged_audit_tear_prevents_any_lane_removal(
    tmp_path: Path, audit_key: bytes, live_worktree: Path
) -> None:
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    segment = next((tmp_path / ".sdd/audit").glob("*.jsonl"))
    record = json.loads(segment.read_text())
    record["prev_hmac"] = record["hmac"]
    record["hmac"] = "f" * 64
    with segment.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    log = _audit(tmp_path, audit_key)
    log.log("test.event", "tester", "task", "after-tear")
    (tear,) = log.verify_detailed().tears
    log.log(
        EVENT_CHAIN_TEAR_ACKNOWLEDGED,
        "operator",
        "audit_segment",
        tear.segment,
        {"segment": tear.segment, "byte_offset": tear.byte_offset, "reason": "investigated"},
    )
    assert log.verify()[0]
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    with pytest.raises(SandboxSweepUnavailableError, match="unverified audit chain"):
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes))
    assert live_worktree.exists()


@pytest.fixture
def dead_worktree(tmp_path: Path, live_worktree: Path) -> Path:
    (tmp_path / ".sdd/runtime/pids/live.json").write_text(json.dumps({"task_id": "T-1"}))
    return live_worktree


@pytest.mark.parametrize("group", ["worktrees", "sandbox"])
def test_force_unsaved_cannot_replay_removal_approval(
    tmp_path: Path, audit_key: bytes, dead_worktree: Path, group: str
) -> None:
    from dataclasses import asdict

    from bernstein.cli.commands.worktrees_cmd import worktrees_group

    (dead_worktree / "only-copy.txt").write_text("preserve me")
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps({"request": asdict(approval.request), "responses": [asdict(r) for r in approval.responses]})
    )
    command = worktrees_group if group == "worktrees" else sandbox_group
    result = CliRunner().invoke(
        command,
        ["sweep", "--workdir", str(tmp_path), "--apply", "--removal-approval", str(approval_path), "--force-unsaved"],
    )
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output and "--force-unsaved" in result.output
    assert (dead_worktree / "only-copy.txt").read_text() == "preserve me"
    assert not any(e.event_type == "lane.remove" for e in _audit(tmp_path, audit_key).query())


@pytest.mark.parametrize("corrupt", [False, True])
def test_live_pid_worktree_is_listed_but_never_removed(
    tmp_path: Path, audit_key: bytes, live_worktree: Path, corrupt: bool
) -> None:
    if corrupt:
        (live_worktree / ".git").unlink()
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    assert len(lanes) == 1
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes)) == 0
    assert live_worktree.exists()
    event = [e for e in _audit(tmp_path, audit_key).query() if e.event_type == "lane.remove"][-1]
    assert event.details["phase"] == "skipped"
    assert event.details["reason"] == "worker PID is alive"


def test_pid_becoming_alive_before_apply_is_preserved(tmp_path: Path, audit_key: bytes, dead_worktree: Path) -> None:
    import os

    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    (tmp_path / ".sdd/runtime/pids/live.json").write_text(json.dumps({"worker_pid": os.getpid(), "task_id": "T-1"}))
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=approval) == 0
    assert dead_worktree.exists()


def test_vanished_worktree_is_controlled_failure(
    tmp_path: Path, audit_key: bytes, dead_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.core.worktrees import classifier

    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    classify = classifier.classify_worktrees
    calls = 0

    def disappear(*args, **kwargs):
        nonlocal calls
        calls += 1
        return classify(*args, **kwargs) if calls <= 2 else []

    monkeypatch.setattr(classifier, "classify_worktrees", disappear)
    with pytest.raises(RuntimeError, match="worktree disappeared"):
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes))
    assert not any(e.details.get("phase") == "intent" for e in _audit(tmp_path, audit_key).query())


@pytest.mark.parametrize("as_json", [False, True])
def test_partial_apply_reports_every_lane_and_fails(
    tmp_path: Path, audit_key: bytes, dead_worktree: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    import subprocess
    from dataclasses import asdict

    from bernstein.cli.commands.worktrees_cmd import worktrees_group
    from bernstein.core.worktrees import classifier

    monkeypatch.setenv("COLUMNS", "80")
    # Keep partial-apply coverage on the only removable resource: worktrees.
    for name in ("second", "third"):
        path = dead_worktree.parent / name
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", "-b", name, str(path), "main"],
            check=True,
            capture_output=True,
        )
        (tmp_path / f".sdd/runtime/pids/{name}.json").write_text(json.dumps({"task_id": "T-1"}))
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    assert len(lanes) == 3
    approval = _removal_approval(lanes)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps({"request": asdict(approval.request), "responses": [asdict(r) for r in approval.responses]})
    )
    reap = classifier.reap_worktree
    calls = []

    def fail_second(workdir, row):
        calls.append(row.session_id)
        if len(calls) == 2:
            raise OSError("teardown failed")
        return reap(workdir, row)

    monkeypatch.setattr(classifier, "reap_worktree", fail_second)
    command = ["sweep", "--workdir", str(tmp_path), "--apply", "--removal-approval", str(approval_path)]
    if as_json:
        command.append("--json")
    result = CliRunner().invoke(worktrees_group, command)
    assert result.exit_code == 1, result.output
    assert calls == [lane.resource_id for lane in lanes[:2]]
    if as_json:
        rows = json.loads(result.stdout)
        assert [row["resource_id"] for row in rows] == [lane.resource_id for lane in lanes]
        assert [row["removal_status"] for row in rows] == ["removed", "failed", "not_attempted"]
    else:
        assert "1 removed" in result.output
        for lane, status in zip(lanes, ("removed", "failed", "not_attempted"), strict=True):
            assert f"{lane.kind} {lane.resource_id}: {status}" in result.output.splitlines()
    assert "teardown failed" in result.stderr
    assert not lanes[0].path.exists()
    assert all(lane.path.exists() for lane in lanes[1:])


@pytest.mark.parametrize("group", ["worktrees", "sandbox"])
def test_sweep_reports_full_resource_ids_in_narrow_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group: str
) -> None:
    from bernstein.cli.commands.worktrees_cmd import worktrees_group
    from bernstein.core.sandbox import leak_sweep

    monkeypatch.setenv("COLUMNS", "80")
    lanes = [
        leak_sweep.LeakedLane(
            kind=kind,
            resource_id=f"{kind}-" + "long-resource-id-" * 8,
            task_ids=("T-1",),
            age_seconds=60,
            size_bytes=None,
        )
        for kind in ("worktree", "sandbox")
    ]
    monkeypatch.setattr(leak_sweep, "sweep_leaked_lanes", lambda *args, **kwargs: lanes)
    command = worktrees_group if group == "worktrees" else sandbox_group
    result = CliRunner().invoke(command, ["sweep", "--workdir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    for lane, status in zip(lanes, ("approval required", "report only (removal: #6571)"), strict=True):
        assert f"{lane.kind} {lane.resource_id}: {status}" in result.output.splitlines()


def test_false_reap_result_is_failure(
    tmp_path: Path, audit_key: bytes, dead_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.core.worktrees import classifier

    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    monkeypatch.setattr(classifier, "reap_worktree", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="worktree removal failed"):
        _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes))
    assert dead_worktree.exists()
    event = [e for e in _audit(tmp_path, audit_key).query() if e.event_type == "lane.remove"][-1]
    assert event.details["phase"] == "failed"


@pytest.mark.parametrize("change", ["unsaved", "pid"])
def test_worktree_safety_is_rechecked_after_intent(
    tmp_path: Path, audit_key: bytes, dead_worktree: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import os

    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    original_log = AuditLog.log

    def change_after_intent(self, *args, **kwargs):
        result = original_log(self, *args, **kwargs)
        if args[0] == "lane.remove" and args[4].get("phase") == "intent":
            if change == "unsaved":
                (dead_worktree / "only-copy.txt").write_text("save me")
            else:
                (tmp_path / ".sdd/runtime/pids/live.json").write_text(
                    json.dumps({"worker_pid": os.getpid(), "task_id": "T-1"})
                )
        return result

    monkeypatch.setattr(AuditLog, "log", change_after_intent)
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes)) == 0
    assert dead_worktree.exists()
    events = [event for event in _audit(tmp_path, audit_key).query() if event.event_type == "lane.remove"]
    assert [event.details["phase"] for event in events] == ["intent", "skipped"]
    assert events[-1].details["reason"] == ("unsaved work" if change == "unsaved" else "worker PID is alive")


def test_unsaved_work_added_after_approval_is_preserved(tmp_path: Path, audit_key: bytes, dead_worktree: Path) -> None:
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    (dead_worktree / "only-copy.txt").write_text("save me")
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=approval) == 0
    assert (dead_worktree / "only-copy.txt").read_text() == "save me"


@pytest.mark.parametrize("group", ["worktrees", "sandbox"])
@pytest.mark.parametrize("as_json", [False, True])
def test_sandbox_preview_is_report_only(tmp_path: Path, audit_key: bytes, group: str, as_json: bool) -> None:
    from bernstein.cli.commands.worktrees_cmd import worktrees_group

    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    command = worktrees_group if group == "worktrees" else sandbox_group
    args = ["sweep", "--workdir", str(tmp_path)]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(command, args)
    assert result.exit_code == 0, result.output
    if as_json:
        (row,) = json.loads(result.output)
        assert row["removal_status"] == "report_only"
        assert row["removal_reason"] == "report only (removal: #6571)"
        assert row["has_unsaved_work"] is None
    else:
        assert "report only" in result.output and "#6571" in result.output


def test_sandbox_not_in_worktree_removal_digest(tmp_path: Path, audit_key: bytes, dead_worktree: Path) -> None:
    from dataclasses import replace

    (worktree,) = _lane_api("sweep_leaked_lanes")(tmp_path)
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    sandbox = next(lane for lane in lanes if lane.kind == "sandbox")
    operation = _lane_api("lane_removal_operation")
    assert operation(lanes) == operation([worktree])
    assert operation([sandbox]) == operation([])
    assert operation([replace(worktree, created_at="different")]) != operation([worktree])
    assert _lane_api("remove_leaked_lanes")(tmp_path, lanes, removal_approval=_removal_approval(lanes)) == 1
    assert not dead_worktree.exists()
    assert sweep_leaked_sandboxes(tmp_path)[0].is_leaked
    assert all(
        event.resource_id != "sbx-1"
        for event in _audit(tmp_path, audit_key).query()
        if event.event_type == "lane.remove"
    )


def test_whole_removal_plan_rechecked_under_lock(
    tmp_path: Path, audit_key: bytes, dead_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager
    from dataclasses import replace

    from bernstein.cli.commands import worktrees_cmd
    from bernstein.core.sandbox import leak_sweep

    (first,) = leak_sweep.sweep_leaked_lanes(tmp_path)
    second = replace(first, resource_id="second", created_at="second-lifetime")
    lanes = [first, second]
    locked = False

    @contextmanager
    def lock(workdir):
        nonlocal locked
        locked = True
        yield

    def current(*args, **kwargs):
        assert locked
        return [first, replace(second, created_at="changed-lifetime")]

    monkeypatch.setattr(worktrees_cmd, "lock_gc", lock)
    monkeypatch.setattr(leak_sweep, "sweep_leaked_lanes", current)
    with pytest.raises(RuntimeError, match="plan changed"):
        worktrees_cmd.remove_leaked_lanes(tmp_path, lanes, removal_approval=_removal_approval(lanes))
    assert dead_worktree.exists()
    assert not any(event.event_type == "lane.remove" for event in _audit(tmp_path, audit_key).query())


@pytest.mark.parametrize("group", ["worktrees", "sandbox"])
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_apply_leaves_sandbox_report_only(
    tmp_path: Path, audit_key: bytes, monkeypatch: pytest.MonkeyPatch, group: str, as_json: bool
) -> None:
    from dataclasses import asdict

    from bernstein.cli.commands.worktrees_cmd import worktrees_group
    from bernstein.core.sandbox import registry

    _record_task(tmp_path, "T-1", "done")
    _record_create(tmp_path, audit_key, "sbx-1", task_ids=["T-1"])
    lanes = _lane_api("sweep_leaked_lanes")(tmp_path)
    approval = _removal_approval(lanes)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps({"request": asdict(approval.request), "responses": [asdict(r) for r in approval.responses]})
    )

    def refuse_lookup(name):
        pytest.fail("apply must not attach to a report-only sandbox")

    monkeypatch.setattr(registry, "get_backend", refuse_lookup)
    command = worktrees_group if group == "worktrees" else sandbox_group
    args = ["sweep", "--workdir", str(tmp_path), "--apply", "--removal-approval", str(approval_path)]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(command, args)
    assert result.exit_code == 0, result.output
    if as_json:
        (row,) = json.loads(result.output)
        assert row["removal_status"] == "report_only"
        assert row["removal_reason"] == "report only (removal: #6571)"
    else:
        assert "report only" in result.output and "#6571" in result.output
        assert "0 removed" in result.output
    assert sweep_leaked_sandboxes(tmp_path)[0].is_leaked
    assert not any(
        event.event_type in {"lane.remove", SANDBOX_SESSION_DESTROY} for event in _audit(tmp_path, audit_key).query()
    )
