"""Regression tests for server-URL templating in the agent prompt builders (#2808).

The completion/subtask curl commands embedded in every agent prompt must derive
their base URL from ``BERNSTEIN_SERVER_URL`` (the value a remote worker exports
before spawning) rather than a hardcoded ``http://127.0.0.1:8052``. Without this
an agent on a worker node POSTs completion to its own loopback, where nothing
listens, and the task can never be marked done. The same literal also breaks a
local run started on a non-default port.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import Task

from bernstein.core.agents.spawner_core import (
    _render_auth_section,
    _render_batch_prompt,
    _render_prompt,
    _resolve_task_server_url,
)

if TYPE_CHECKING:
    from pathlib import Path

_LOCAL_DEFAULT = "http://127.0.0.1:8052"


def _task() -> Task:
    return Task(id="T-1", title="Do the thing", description="Body.", role="backend")


class TestResolveTaskServerUrl:
    def test_defaults_to_local_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        assert _resolve_task_server_url() == _LOCAL_DEFAULT

    def test_reads_env_and_strips_trailing_slash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://central:9000/")
        assert _resolve_task_server_url() == "http://central:9000"


class TestRenderPromptServerUrl:
    def test_completion_url_uses_server_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://central:9000")
        prompt = _render_prompt([_task()], tmp_path, tmp_path)
        assert "http://central:9000/tasks/T-1/complete" in prompt
        assert _LOCAL_DEFAULT not in prompt

    def test_completion_url_defaults_to_local_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        prompt = _render_prompt([_task()], tmp_path, tmp_path)
        assert f"{_LOCAL_DEFAULT}/tasks/T-1/complete" in prompt


class TestRenderBatchPromptServerUrl:
    def test_completion_url_uses_server_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://central:9000")
        prompt = _render_batch_prompt(_task())
        assert "http://central:9000/tasks/T-1/complete" in prompt
        assert _LOCAL_DEFAULT not in prompt


class TestRenderAuthSectionServerUrl:
    def test_curl_examples_use_server_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://central:9000")
        section = _render_auth_section(tmp_path / "token")
        assert "POST http://central:9000/tasks" in section
        assert "http://central:9000/tasks/<TASK_ID>/complete" in section
        assert _LOCAL_DEFAULT not in section
