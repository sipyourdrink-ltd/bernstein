"""Unit tests for failure-aware retry creation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from bernstein.core.task_lifecycle import maybe_retry_task


def _write_log(tmp_path: Path, session_id: str, content: str) -> None:
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{session_id}.log").write_text(content, encoding="utf-8")


def test_retry_injects_failure_context(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-1", title="Compile parser", description="Fix the parser.")
    _write_log(tmp_path, "A-1", "SyntaxError: invalid syntax\nModified: src/parser.py\n")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": "T-2"}
    client = MagicMock()
    client.post.return_value = response

    created = maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=2,
        client=client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=tmp_path,
        session_id="A-1",
    )

    payload = client.post.call_args.kwargs["json"]
    assert created is True
    assert "## Previous attempt failed" in payload["description"]
    assert "compile_error" in payload["description"]


def test_retry_without_log(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-1", title="Retry me", description="Do the thing.")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": "T-2"}
    client = MagicMock()
    client.post.return_value = response

    maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=2,
        client=client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=tmp_path,
        session_id="missing",
    )

    # audit-017: description is passed through verbatim; retry attempt is
    # tracked in the typed ``retry_count`` field (no ``[RETRY N]`` prefix).
    payload = client.post.call_args.kwargs["json"]
    assert payload["description"] == "Do the thing."
    assert payload["retry_count"] == 1
    assert "[RETRY" not in payload["title"]


def test_retry_logs_failure_category(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-1", title="Retry me", description="Do the thing.")
    _write_log(tmp_path, "A-1", "429 Too Many Requests\n")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": "T-2"}
    client = MagicMock()
    client.post.return_value = response
    collector = MagicMock()

    with patch("bernstein.core.tasks.task_lifecycle.get_collector", return_value=collector):
        maybe_retry_task(
            task,
            retried_task_ids=set(),
            max_task_retries=2,
            client=client,
            server_url="http://server",
            quarantine=MagicMock(),
            workdir=tmp_path,
            session_id="A-1",
        )

    args = collector.record_error.call_args.args
    kwargs = collector.record_error.call_args.kwargs
    assert args[0] == "rate_limit"
    assert args[1] == "retry"
    assert kwargs["role"] == "backend"


def test_retry_truncates_long_failure_context(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-1", title="Retry me", description="Do the thing.")
    _write_log(
        tmp_path,
        "A-1",
        "\n".join(f"SyntaxError: invalid syntax on line {idx}" for idx in range(1000)),
    )
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": "T-2"}
    client = MagicMock()
    client.post.return_value = response

    maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=2,
        client=client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=tmp_path,
        session_id="A-1",
    )

    description = client.post.call_args.kwargs["json"]["description"]
    context = description.split("## Previous attempt failed\n", 1)[1].split("\n\nAvoid the same mistakes.", 1)[0]
    assert len(context) <= 500
