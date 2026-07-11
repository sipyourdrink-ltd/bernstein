"""Log-sanitization tests for user-controlled values on task_crud routes.

Several task routes interpolate user-controlled strings (task ids, reasons)
into server log lines.  A caller that embeds CR/LF in such a value could
forge additional log lines (log injection).  Two independent layers close
this off:

*   A **boundary** ``field_validator`` on the ``reason`` request models
    strips CR/LF (to spaces) and caps length before the value ever reaches a
    route, so an injected newline never survives ingest.
*   A **sink** wrapper (``sanitize_log``) escapes CR/LF at every ``logger.*``
    call for values that are not boundary-validated (e.g. path-param task
    ids), so the log line is safe even if a control char reaches the call.

These tests drive the real FastAPI routes via TestClient and assert:

1.  A ``reason`` carrying CR/LF is neutralized (boundary layer) so it cannot
    forge a second log line.
2.  A normal ``reason`` (no control chars) is logged verbatim -- sanitizing
    does not change the logged meaning for non-malicious input.
3.  A path-param ``task_id`` carrying CR/LF is escaped by the sink wrapper in
    the emitted log record.
4.  The boundary validator itself strips CR/LF and rejects over-long reasons.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from bernstein.core.server import create_app
from bernstein.core.server.server_models import (
    _MAX_REASON_LEN,
    TaskFailRequest,
    TaskReopenRequest,
)

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


def test_reopen_reason_with_crlf_is_neutralized_in_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
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
    # The literal CR/LF must not survive into the formatted message: the
    # boundary validator strips them to spaces at ingest, so no second log
    # line can be forged and the payload text is preserved on one line.
    assert "\r" not in line
    assert "\n" not in line
    assert "real  task.reopen: FORGED injected line" in line


def test_path_task_id_with_crlf_is_escaped_in_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A CR/LF-bearing path-param task id is escaped by the sink wrapper.

    ``task_id`` is a path parameter, not a validated request-body field, so
    the sink-side ``sanitize_log`` wrapper is what protects the log line here.
    A 404 on an unknown id logs ``task.claim 404: task_id=...`` -- the injected
    newline must appear escaped, not as a real line break.
    """
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        # URL-encoded CR/LF in the path segment.
        forged = "ghost%0d%0atask.claim%20404:%20FORGED"
        resp = client.post(f"/tasks/{forged}/claim")
        assert resp.status_code == 404, resp.text

    records = [
        r.message  # type: ignore[attr-defined]
        for r in caplog.records
        if r.message.startswith("task.claim 404:")  # type: ignore[attr-defined]
    ]
    assert records, f"expected a task.claim 404 log line, got: {caplog.text}"  # type: ignore[attr-defined]
    line = records[0]
    # The raw CR/LF must not survive; the escaped forms are present instead.
    assert "\r" not in line
    assert "\n" not in line
    assert "\\r\\n" in line


def test_reason_validator_strips_crlf_and_caps_length() -> None:
    """The boundary field_validator strips CR/LF and rejects over-long reasons."""
    ok = TaskReopenRequest(reason="line1\r\nline2\rline3\nend")
    assert "\r" not in ok.reason
    assert "\n" not in ok.reason
    assert ok.reason == "line1  line2 line3 end"

    # A plain reason is untouched.
    assert TaskFailRequest(reason="flaky test").reason == "flaky test"

    # Over-long reason is rejected at the boundary (surfaced by FastAPI as 422).
    with pytest.raises(ValueError, match="exceeds"):
        TaskFailRequest(reason="x" * (_MAX_REASON_LEN + 1))


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
