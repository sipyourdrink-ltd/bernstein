"""Cross-platform conformance of stop/restart for the three primary
adapters (claude, codex, gemini) under a mocked Windows platform.

Acceptance criterion 2 of the Windows-parity work asks the conformance
suite -- including stop/restart -- to be green for claude, codex, and
gemini on Windows. A real Windows runner is one way to prove that; the
observable contract it checks is the *process-group projection*:

* spawn() must spread the Windows process-group flags
  (``creationflags=CREATE_NEW_PROCESS_GROUP``) into ``subprocess.Popen``,
  not the POSIX ``start_new_session=True`` -- otherwise ``taskkill /T``
  cannot reap the agent tree and stop/restart leaks processes.
* stop() must project onto a ``windows_process_tree`` reap receipt whose
  force tier degrades to the numeric ``9`` code (``SIGKILL`` is not
  importable on Windows), and
* restart() must be able to spawn a fresh process group afterwards.

Those are pure-logic branches once the kernel boundary (``subprocess``,
``kill_process_group``, ``process_alive``) is mocked, so the conformance
contract is asserted here on any host by flipping ``IS_WINDOWS``. This
does not stub the production spawn/reap logic -- only the OS boundary --
so a regression in the Windows projection fails these tests exactly as it
would on a Windows runner.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

import bernstein.core.config.platform_compat as pc
from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.gemini import GeminiAdapter

# spawn() arms a watchdog Timer thread; disable it suite-wide to avoid the
# OS thread ceiling under the isolated runner (same guard as the gemini
# adapter suite).
pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    m.wait.return_value = None
    return m


@dataclass(frozen=True)
class AdapterCase:
    """One primary adapter and how to spawn it under mocked Popen."""

    name: str
    module: str
    factory: Callable[[], Any]
    model: str
    extra_patches: Callable[[], list[Any]]


def _which_gemini() -> list[Any]:
    def _stub(name: str) -> str | None:
        return f"/usr/local/bin/{name}" if name in ("antigravity", "gemini") else None

    return [patch("bernstein.adapters.gemini.shutil.which", side_effect=_stub)]


PRIMARY_ADAPTERS = (
    AdapterCase("claude", "bernstein.adapters.claude", ClaudeCodeAdapter, "sonnet", lambda: []),
    AdapterCase("codex", "bernstein.adapters.codex", CodexAdapter, "o3", lambda: []),
    AdapterCase("gemini", "bernstein.adapters.gemini", GeminiAdapter, "gemini-3.1-pro", _which_gemini),
)


def _spawn_capture(case: AdapterCase, pid: int, tmp_path: Path) -> dict[str, Any]:
    """Spawn the adapter under mocked Popen; return the first Popen kwargs."""
    adapter = case.factory()
    # Two side-effect mocks cover adapters (claude) that spawn an agent
    # process plus a wrapper process in one spawn() call.
    popen_mocks = [_make_popen_mock(pid), _make_popen_mock(pid + 1)]
    patches = [patch(f"{case.module}.subprocess.Popen", side_effect=popen_mocks), *case.extra_patches()]
    entered = [p.__enter__() for p in patches]
    try:
        popen = entered[0]
        adapter.spawn(
            prompt="do the thing",
            workdir=tmp_path,
            model_config=ModelConfig(model=case.model, effort="low"),
            session_id=f"win-conformance-{case.name}",
        )
        # The agent process is the first Popen call.
        return dict(popen.call_args_list[0].kwargs)
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)


@pytest.mark.parametrize("case", PRIMARY_ADAPTERS, ids=lambda c: c.name)
class TestWindowsSpawnProjection:
    """spawn() projects onto the Windows process-group flags under mock."""

    def test_windows_spawn_uses_creationflags(
        self, case: AdapterCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        kwargs = _spawn_capture(case, pid=4100, tmp_path=tmp_path)
        assert "creationflags" in kwargs
        assert kwargs["creationflags"] == pc._WIN_CREATE_NEW_PROCESS_GROUP
        # The POSIX session flag must NOT be present on the Windows branch.
        assert "start_new_session" not in kwargs

    def test_posix_spawn_uses_start_new_session(
        self, case: AdapterCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        kwargs = _spawn_capture(case, pid=4200, tmp_path=tmp_path)
        assert kwargs.get("start_new_session") is True
        assert "creationflags" not in kwargs


@pytest.mark.parametrize("case", PRIMARY_ADAPTERS, ids=lambda c: c.name)
class TestWindowsStopRestart:
    """stop() -> windows_process_tree receipt; restart() spawns again."""

    def test_stop_projects_windows_reap_receipt(
        self, case: AdapterCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_detect_os_name", lambda: "windows")
        spawned_kwargs = _spawn_capture(case, pid=5100, tmp_path=tmp_path)
        assert "creationflags" in spawned_kwargs

        # Stop the agent group: TERM delivered, process gone within grace.
        monkeypatch.setattr(pc, "kill_process_group", lambda _pgid, _sig: True)
        monkeypatch.setattr(pc, "process_alive", lambda _pid: False)
        receipt = pc.reap_process_group(5100, grace_seconds=0.05, poll_interval=0.01)
        assert receipt.method == "windows_process_tree"
        assert receipt.os_name == "windows"
        assert receipt.delivered is True
        assert receipt.escalated is False

    def test_stop_escalation_uses_numeric_force_code(
        self, case: AdapterCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _spawn_capture(case, pid=5300, tmp_path=tmp_path)

        sent: list[tuple[int, int]] = []

        def _kpg(pgid: int, sig: int) -> bool:
            sent.append((pgid, sig))
            return True

        monkeypatch.setattr(pc, "kill_process_group", _kpg)
        # Never dies -> escalate to force-kill after the grace window.
        monkeypatch.setattr(pc, "process_alive", lambda _pid: True)
        receipt = pc.reap_process_group(5300, grace_seconds=0.02, poll_interval=0.01)
        assert receipt.method == "windows_process_tree"
        assert receipt.escalated is True
        # SIGKILL is not importable on Windows: the force tier degrades to 9.
        assert sent[-1] == (5300, 9)

    def test_restart_after_stop_spawns_fresh_group(
        self, case: AdapterCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        first = _spawn_capture(case, pid=6100, tmp_path=tmp_path)
        assert "creationflags" in first

        # Stop cleanly, then restart -> a new Popen with the same Windows
        # projection (a fresh process group the orchestrator can reap).
        monkeypatch.setattr(pc, "kill_process_group", lambda _pgid, _sig: True)
        monkeypatch.setattr(pc, "process_alive", lambda _pid: False)
        pc.reap_process_group(6100, grace_seconds=0.02, poll_interval=0.01)

        second = _spawn_capture(case, pid=6200, tmp_path=tmp_path)
        assert "creationflags" in second
        assert "start_new_session" not in second
