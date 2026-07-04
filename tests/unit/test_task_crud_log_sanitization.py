"""Log-sanitization tests for user-controlled values on task_crud routes.

Several task routes interpolate user-controlled strings (task ids, reasons)
into server log lines.  A caller that embeds CR/LF in such a value could
forge additional log lines (log injection).  The routes now pass each
user-controlled value through ``sanitize_log`` before logging, which escapes
newlines and carriage returns.

These tests drive the real FastAPI routes via TestClient and assert:

1.  A ``reason`` carrying CR/LF is escaped in the emitted log record so it
    cannot forge a second log line.
2.  A normal ``reason`` (no control chars) is logged verbatim -- sanitizing
    does not change the logged meaning for non-malicious input.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


def _create_and_complete(client: TestClient) -> str:
    """Create a task, claim it, and complete it so it is DONE (reopen-able)."""
    create_resp = client.post(
        "/tasks",
        json={
            "title": "Do the thing",
            "description": "Some work.",
            "role": "backend",
            "priority": 1,
            "scope": "small",
            "complexity": "low",
            "estimated_minutes": 10,
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    task_id = create_resp.json()["id"]

    claim = client.post(f"/tasks/{task_id}/claim")
    assert claim.status_code == 200, claim.text

    complete = client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": "did it"},
    )
    assert complete.status_code == 200, complete.text
    return task_id


def _reopen_log_line(caplog: pytest.LogCaptureFixture) -> str:
    records = [
        r.message  # type: ignore[attr-defined]
        for r in caplog.records
        if r.message.startswith("task.reopen:")  # type: ignore[attr-defined]
    ]
    assert records, f"expected a task.reopen log line, got: {caplog.text}"  # type: ignore[attr-defined]
    return records[0]


def test_reopen_reason_with_crlf_is_escaped_in_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        task_id = _create_and_complete(client)
        # A forged-log-line payload: the CR/LF would otherwise inject a
        # fabricated "task.reopen: forged" line into the server log.
        malicious = "real\r\ntask.reopen: FORGED injected line"
        reopen = client.post(f"/tasks/{task_id}/reopen", json={"reason": malicious})
        assert reopen.status_code == 200, reopen.text

    line = _reopen_log_line(caplog)
    # The literal CR/LF must not survive into the formatted message.
    assert "\r" not in line
    assert "\n" not in line
    # The escaped forms are present instead, and the payload text is preserved.
    assert "real\\r\\ntask.reopen: FORGED injected line" in line


def test_reopen_reason_plain_is_logged_verbatim(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        task_id = _create_and_complete(client)
        reopen = client.post(f"/tasks/{task_id}/reopen", json={"reason": "janitor rejected: flaky test"})
        assert reopen.status_code == 200, reopen.text

    line = _reopen_log_line(caplog)
    assert task_id in line
    assert "reason=janitor rejected: flaky test" in line
