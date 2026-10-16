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


@pytest.mark.parametrize("status", ["done", "failed", "closed", "cancelled"])
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
