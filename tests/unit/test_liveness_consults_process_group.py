"""A session is alive while any member of its process group is.

Finalization seals the journal head into the lineage spine and writes the run
receipt after drain reports every session dead. Drain asked
`_check_alive_process`, which was `proc.poll()` on the stored wrapper alone.

Adapters spawn the tool with `start_new_session=True`, so the wrapper is a
session leader and the tool — and anything it forks — lives in that group and
outlives it. The old check therefore reported a session dead while its
descendants were still writing, and the receipt covering the run was produced
over execution that had not stopped (#5272).

The test that matters is the last one: a real orphan in the group, not a mock,
because the whole defect is about what `poll()` does not see.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

import pytest

from bernstein.core.config.platform_compat import IS_WINDOWS, process_group_alive
from bernstein.core.tasks.models import AgentSession


class _FakeProc:
    """A stand-in for the stored `Popen`: a pid and a scripted `poll()`."""

    def __init__(self, pid: int, exit_code: int | None) -> None:
        self.pid = pid
        self._exit_code = exit_code

    def poll(self) -> int | None:
        return self._exit_code


def _spawner_with(procs: dict[str, Any]) -> Any:
    from bernstein.core.agents.spawner_core import AgentSpawner

    spawner = AgentSpawner.__new__(AgentSpawner)
    spawner._procs = procs
    return spawner


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(id="s-1", role="backend")


def test_a_running_wrapper_is_alive(session: AgentSession) -> None:
    """Unchanged: `poll()` returning None short-circuits before any group probe."""
    spawner = _spawner_with({"s-1": _FakeProc(pid=4242, exit_code=None)})
    assert spawner._check_alive_process(session) is True
    assert session.exit_code is None


def test_no_stored_process_defers_to_the_next_checker(session: AgentSession) -> None:
    """`None` keeps the checker chain working."""
    assert _spawner_with({})._check_alive_process(session) is None


def test_an_exited_wrapper_with_an_empty_group_is_dead(session: AgentSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary case, and the one that must stay fast."""
    import bernstein.core.config.platform_compat as compat

    monkeypatch.setattr(compat, "process_group_alive", lambda pgid: False)
    spawner = _spawner_with({"s-1": _FakeProc(pid=4242, exit_code=0)})
    assert spawner._check_alive_process(session) is False
    assert session.exit_code == 0


def test_an_exited_wrapper_with_a_live_group_is_alive(session: AgentSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect, in one assertion.

    The wrapper has exited; a descendant has not. Reporting dead here is what
    let finalization seal a journal over work still in flight.
    """
    import bernstein.core.config.platform_compat as compat

    monkeypatch.setattr(compat, "process_group_alive", lambda pgid: True)
    spawner = _spawner_with({"s-1": _FakeProc(pid=4242, exit_code=0)})
    assert spawner._check_alive_process(session) is True


def test_the_wrapper_exit_code_is_recorded_even_while_the_group_survives(
    session: AgentSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper's exit code is a fact about the wrapper, known now."""
    import bernstein.core.config.platform_compat as compat

    monkeypatch.setattr(compat, "process_group_alive", lambda pgid: True)
    spawner = _spawner_with({"s-1": _FakeProc(pid=4242, exit_code=3)})
    spawner._check_alive_process(session)
    assert session.exit_code == 3


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX process groups")
def test_a_real_orphan_in_the_group_keeps_the_session_alive(session: AgentSession) -> None:
    """The end-to-end case, with real processes rather than a monkeypatch.

    A wrapper that starts its own session, forks a child that outlives it, and
    exits. `poll()` reports the wrapper gone; the group is not.
    """
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);sys.exit(0)",
        ],
        start_new_session=True,
    )
    try:
        wrapper.wait(timeout=30)
        # The wrapper is gone; its child is not, and shares the group.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not process_group_alive(wrapper.pid):
            time.sleep(0.05)
        assert process_group_alive(wrapper.pid), "fixture failed: the orphan did not survive"

        spawner = _spawner_with({"s-1": wrapper})
        assert spawner._check_alive_process(session) is True, (
            "the wrapper exited but its group still has members: the session is not done"
        )
        assert session.exit_code == 0
    finally:
        from bernstein.core.config.platform_compat import kill_process_group

        kill_process_group(wrapper.pid, 9)
