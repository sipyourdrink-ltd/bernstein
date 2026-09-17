"""The curl examples in a spawn prompt must name the server this run started.

`spawn_prompt` wrote `http://127.0.0.1:8052` into every curl example it renders
-- task completion, bulletin posts, and the agent-to-agent channel. A run on a
dynamically allocated port therefore handed its agent commands aimed at a port
nothing is listening on, or worse, at *another run's* server, which answers and
rejects. The agent cannot resolve this for itself: it works inside a worktree
whose `.sdd` carries no port file (#5964).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

from bernstein.core.agents.spawn_prompt import (
    _legacy_completion_instructions,
    _task_server_url,
)
from bernstein.core.defaults import SDD_SERVER_PORT


def _task(task_id: str = "t-1") -> Task:
    return Task(
        id=task_id,
        title="Do the thing",
        description="Do the thing well",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
    )


def _write_port(workdir: Path, port: int) -> None:
    path = workdir / SDD_SERVER_PORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")


class TestTaskServerUrl:
    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A worker node exports the central server's URL; it outranks any local port."""
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://10.0.0.7:9001/")
        _write_port(tmp_path, 8099)

        assert _task_server_url(tmp_path) == "http://10.0.0.7:9001"

    def test_reads_the_run_s_own_port_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """This is the case the hardcoded default got wrong."""
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        _write_port(tmp_path, 8099)

        assert _task_server_url(tmp_path) == "http://127.0.0.1:8099"

    def test_falls_back_to_the_default_port(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """With nothing to read, the historical default is still the right guess."""
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)

        assert _task_server_url(tmp_path) == "http://127.0.0.1:8052"


class TestSpawnPromptRendersDynamicServerPort:
    def test_spawn_prompt_renders_dynamic_server_port(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The completion curl names the resolved server, not 8052."""
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        _write_port(tmp_path, 8099)

        rendered = _legacy_completion_instructions([_task()], _task_server_url(tmp_path))

        assert "http://127.0.0.1:8099/tasks/t-1/complete" in rendered
        assert "8052" not in rendered

    def test_a_remote_server_url_reaches_the_completion_curl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A worker-node agent must POST to the central server, not its own loopback."""
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://10.0.0.7:9001")

        rendered = _legacy_completion_instructions([_task()], _task_server_url(tmp_path))

        assert "http://10.0.0.7:9001/tasks/t-1/complete" in rendered
        assert "127.0.0.1" not in rendered


def test_no_curl_example_in_the_module_hardcodes_the_default_port() -> None:
    """The regression is one literal reappearing, so pin the absence.

    Four call sites carried `http://127.0.0.1:8052` and each looked harmless on
    its own. The only mentions left are in the resolver's own docstring, which
    explains the fallback rather than emitting it.
    """
    from bernstein.core.agents import spawn_prompt

    source = Path(spawn_prompt.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "127.0.0.1:8052" in line and not line.strip().startswith(("#", '"', "'"))
    ]
    assert offenders == [], offenders
