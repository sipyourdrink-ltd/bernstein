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

import re
from pathlib import Path

import pytest
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

from bernstein import _BUNDLED_TEMPLATES_DIR
from bernstein.core.agents.spawn_prompt import (
    _render_completion_instructions,
    _task_server_url,
    render_prompt,
)
from bernstein.core.agents.spawn_prompt import _render_prompt as _render_prompt_mirror
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


#: Every base URL a curl in the prompt could name: loopback on any port, or the
#: worker-node server the tests set. Documentation links (https://...) are not
#: server URLs and are deliberately not matched.
_BASE_URL_RE = re.compile(r"http://(?:127\.0\.0\.1|localhost|10\.0\.0\.7):\d+")


def _render(tasks: list[Task], workdir: Path) -> str:
    """The prompt a spawned agent is actually handed, through the public entry point."""
    return render_prompt(tasks, _BUNDLED_TEMPLATES_DIR / "roles", workdir)


class TestTheRenderedPromptNamesOneServer:
    """Asserted on the rendered prompt, not on a helper.

    The completion curl comes from the shared include, and the bulletin and
    channel curls from this module. A test on one helper passed while the
    prompt as a whole named two servers: the bulletin curl carried 8099 and
    the completion POST still carried 8052. So these assert the whole set of
    base URLs, and that 8052 appears nowhere.
    """

    def test_a_dynamic_port_is_the_only_server_in_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        _write_port(tmp_path, 8099)

        prompt = _render([_task()], tmp_path)

        assert "http://127.0.0.1:8099/tasks/t-1/complete" in prompt
        assert "8052" not in prompt
        assert set(_BASE_URL_RE.findall(prompt)) == {"http://127.0.0.1:8099"}

    def test_several_tasks_share_the_same_server(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The multi-task branch of the include substitutes the server too."""
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        _write_port(tmp_path, 8099)

        prompt = _render([_task("t-1"), _task("t-2")], tmp_path)

        assert "http://127.0.0.1:8099/tasks/<task_id>/complete" in prompt
        assert set(_BASE_URL_RE.findall(prompt)) == {"http://127.0.0.1:8099"}

    def test_a_remote_server_url_is_the_only_server_in_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A worker-node agent must POST to the central server, not its own loopback."""
        monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://10.0.0.7:9001")

        prompt = _render([_task()], tmp_path)

        assert "http://10.0.0.7:9001/tasks/t-1/complete" in prompt
        assert set(_BASE_URL_RE.findall(prompt)) == {"http://10.0.0.7:9001"}


def test_an_unreadable_include_falls_back_to_the_resolved_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fallback branch: a stripped install with no include still names the right server."""
    import bernstein

    monkeypatch.setattr(bernstein, "_BUNDLED_TEMPLATES_DIR", tmp_path / "no-templates-here")

    text = _render_completion_instructions([_task()], "http://127.0.0.1:8099")

    assert "http://127.0.0.1:8099/tasks/t-1/complete" in text
    assert "8052" not in text


def test_no_curl_example_in_the_module_hardcodes_the_default_port() -> None:
    """The regression is one literal reappearing, so pin the absence.

    Five literals carried `http://127.0.0.1:8052` and each looked harmless on
    its own. No line is exempt: an earlier filter skipped lines starting with a
    quote, which is four of the five original literals, so it guarded almost
    nothing. The resolver's docstring says "8052" without the host, and is not
    an offender.
    """
    from bernstein.core.agents import spawn_prompt

    source = Path(spawn_prompt.__file__).read_text(encoding="utf-8")
    offenders = [line.strip() for line in source.splitlines() if "127.0.0.1:8052" in line]
    assert offenders == [], offenders


def test_the_manager_system_prompt_names_the_resolved_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The live path the issue reports: the manager creates tasks with curl.

    `templates/roles/manager/system_prompt.md` wrote 8052 three times, so on a
    dynamic port the manager's task-creation POSTs went nowhere, or to another
    run's server. Rendered through both paths that render it.
    """
    from bernstein.core.agents.spawner_core import _render_prompt as _live_render

    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    _write_port(tmp_path, 8099)
    manager_task = _task()
    manager_task.role = "manager"

    for render in (_live_render, _render_prompt_mirror):
        prompt = render([manager_task], _BUNDLED_TEMPLATES_DIR / "roles", tmp_path)
        assert "{{SERVER_URL}}" not in prompt, render.__module__
        assert "8052" not in prompt, render.__module__
        assert "http://127.0.0.1:8099/tasks" in prompt, render.__module__
