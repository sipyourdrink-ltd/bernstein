"""A resume whose runner raises is recorded ``failed``, not ``resumed`` (issue #8206).

``consume_resume_request`` and the supervisor's ssh-runner path used to record
``resumed`` when the runner raised. ``claim_resume_request`` never claims a
``resumed`` request, so the failed attempt reported
``runtime_acknowledged: true`` and could not be retried for its request id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.resume_cmd import EXIT_OK, resume_cmd
from bernstein.core.persistence.task_resume import (
    SOURCE_WORK_LEDGER,
    ResumeDispatchStatus,
    TaskResumeCheckpoint,
    claim_resume_request,
    consume_resume_request,
    load_resume_request,
    publish_resume_request,
    read_resume_requests,
    resume_request_acknowledged,
    save_checkpoint,
)
from bernstein.core.run_service import RunService, advance_run
from bernstein.core.run_service.ssh_runner import SSHRunnerError


def _checkpoint(task_id: str) -> TaskResumeCheckpoint:
    return TaskResumeCheckpoint.model_validate(
        {
            "task_id": task_id,
            "last_completed_step_id": "step-3",
            "adapter": "claude",
            "adapter_session_id": "sess-1",
        }
    )


def _resume_json(workdir: Path, task_id: str) -> dict[str, object]:
    result = CliRunner().invoke(resume_cmd, [task_id, "--workdir", str(workdir), "--json"])
    assert result.exit_code == EXIT_OK, result.output
    return json.loads(result.stdout)


def _boom() -> None:
    raise RuntimeError("runner crashed")


def test_a_runner_exception_records_failed_and_is_not_acknowledged(tmp_path: Path) -> None:
    record = publish_resume_request(tmp_path, "t-1", source_digest="d" * 64)

    with pytest.raises(RuntimeError, match="runner crashed"):
        consume_resume_request(tmp_path, "t-1", _boom, request_id=record.request_id, worker_id="w-1")

    failed = load_resume_request(tmp_path, "t-1", record.request_id)
    assert failed is not None
    assert failed.dispatch_status == ResumeDispatchStatus.FAILED.value
    assert failed.failed_at
    assert failed.resumed_at is None
    assert resume_request_acknowledged(tmp_path, "t-1", record.request_id) is False
    # It survives a round trip through the companion file.
    assert read_resume_requests(tmp_path, "t-1")[0].failed_at == failed.failed_at


def test_a_failed_request_is_retried_by_its_request_id(tmp_path: Path) -> None:
    record = publish_resume_request(tmp_path, "t-1", source_digest="d" * 64)
    with pytest.raises(RuntimeError):
        consume_resume_request(tmp_path, "t-1", _boom, request_id=record.request_id)

    # Not picked up implicitly: a runner that keeps failing is not retried forever.
    assert claim_resume_request(tmp_path, "t-1") is None

    calls: list[str] = []
    retried = consume_resume_request(tmp_path, "t-1", lambda: calls.append("ran"), request_id=record.request_id)
    assert calls == ["ran"]
    assert retried is not None
    assert retried.dispatch_status == ResumeDispatchStatus.RESUMED.value
    assert resume_request_acknowledged(tmp_path, "t-1", record.request_id) is True


def test_the_cli_reports_a_failed_attempt_and_requeues_it(tmp_path: Path) -> None:
    save_checkpoint(tmp_path, _checkpoint("crash"))
    first = _resume_json(tmp_path, "crash")
    request_id = str(first["request_id"])
    with pytest.raises(RuntimeError):
        consume_resume_request(tmp_path, "crash", _boom, request_id=request_id)

    again = _resume_json(tmp_path, "crash")

    assert again["request_id"] == request_id
    assert again["runtime_acknowledged"] is False
    # Asking again re-queues the failed attempt for the next worker pass.
    assert again["dispatch_status"] == ResumeDispatchStatus.QUEUED.value
    assert claim_resume_request(tmp_path, "crash") is not None


def test_a_failed_ssh_runner_leaves_the_supervisor_request_retryable(tmp_path: Path) -> None:
    run_id = RunService(tmp_path).submit("goal", ["t0"], run_id="run-8206").run_id
    record = publish_resume_request(tmp_path, "t0", source_digest="d" * 64, source=SOURCE_WORK_LEDGER, run_id=run_id)

    def runner(_task_id: str) -> None:
        raise SSHRunnerError("remote exited 23", exit_code=23)

    with pytest.raises(SSHRunnerError):
        advance_run(tmp_path, run_id, task_runner=runner)

    after = load_resume_request(tmp_path, "t0", record.request_id)
    assert after is not None
    assert after.dispatch_status == ResumeDispatchStatus.FAILED.value
    assert resume_request_acknowledged(tmp_path, "t0", record.request_id) is False
