"""Cross-platform adapter conformance: spawn / stop / restart (#2367).

Unlike ``test_adapter_e2e.py`` (which skips on Windows because a handful of its
cases lean on POSIX signal delivery), this module exercises the platform-neutral
conformance contract for the three primary adapters -- claude, codex, gemini --
against the cross-platform fake-CLI harness, so it runs on the Windows CI lane
too. It proves, against a real spawned subprocess (no ``Popen`` mock), that each
adapter can:

* **spawn** the upstream CLI and capture its output,
* **stop** a hung spawn through the platform process-tree reap (Job Object /
  ``taskkill`` on Windows, process-group signal on POSIX), and
* **restart** into a fresh session after a prior spawn has exited.

The harness installs ``claude``/``codex``/``gemini`` shims on ``PATH`` (a POSIX
``sh`` wrapper or a Windows ``.cmd`` batch shim); the adapter launch path
resolves and runs them exactly as it would the real CLI.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.core.config.platform_compat import process_alive, reap_process_group

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter

    from .fake_cli.conftest_adapters import FakeCLIHandle

pytestmark = [pytest.mark.integration]

# The three primary adapters the conformance suite must green on every OS.
_ADAPTERS: dict[str, tuple[type[CLIAdapter], ModelConfig]] = {
    "claude": (ClaudeCodeAdapter, ModelConfig(model="sonnet", effort="medium")),
    "codex": (CodexAdapter, ModelConfig(model="gpt-5.5-mini", effort="medium")),
    "gemini": (GeminiAdapter, ModelConfig(model="gemini-3-flash", effort="medium")),
}


def _git_workdir(tmp_path: Path) -> Path:
    """Initialise the minimal git workdir the adapters expect."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "commit", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(args, cwd=workdir, check=True, capture_output=True)
    return workdir


def _reap_via_proc(result: object, *, timeout_s: float = 8.0) -> int:
    """Wait for the spawned worker to exit (cross-platform ``proc.wait``)."""
    proc = getattr(result, "proc", None)
    if proc is not None and hasattr(proc, "wait"):
        return int(proc.wait(timeout=timeout_s))
    return 0


@pytest.mark.parametrize("adapter_name", sorted(_ADAPTERS))
def test_adapter_spawns_and_captures_output(
    adapter_name: str,
    tmp_path: Path,
    fake_cli_fixture: FakeCLIHandle,
) -> None:
    """Each primary adapter spawns the fake CLI and captures its output."""
    adapter_cls, model = _ADAPTERS[adapter_name]
    workdir = _git_workdir(tmp_path)
    result = adapter_cls().spawn(
        prompt=f"{adapter_name}-success",
        workdir=workdir,
        model_config=model,
        session_id=f"{adapter_name}-conf-spawn",
    )
    exit_code = _reap_via_proc(result, timeout_s=8.0)
    with contextlib.suppress(Exception):
        adapter_cls.cancel_timeout(result)
    assert exit_code == 0
    # The fake recorded the argv the adapter actually assembled.
    assert fake_cli_fixture.read_argv(), "adapter did not spawn the fake CLI"


@pytest.mark.parametrize("adapter_name", sorted(_ADAPTERS))
def test_adapter_stop_terminates_a_hung_spawn(
    adapter_name: str,
    tmp_path: Path,
    fake_cli_fixture: FakeCLIHandle,
) -> None:
    """A hung spawn is stopped through the platform process-tree reap."""
    adapter_cls, model = _ADAPTERS[adapter_name]
    workdir = _git_workdir(tmp_path)
    fake_cli_fixture.configure(mode="hang")
    result = adapter_cls().spawn(
        prompt=f"{adapter_name}-hang",
        workdir=workdir,
        model_config=model,
        session_id=f"{adapter_name}-conf-stop",
    )
    pid = int(getattr(result, "pid", 0))
    assert pid > 0
    receipt = reap_process_group(pid, grace_seconds=3.0)
    assert receipt.delivered
    # The worker must actually be gone after the reap.
    proc = getattr(result, "proc", None)
    if proc is not None and hasattr(proc, "wait"):
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=8.0)
        assert proc.poll() is not None, "worker still alive after platform reap"
    else:
        assert not process_alive(pid), "worker still alive after platform reap"
    with contextlib.suppress(Exception):
        adapter_cls.cancel_timeout(result)


@pytest.mark.parametrize("adapter_name", sorted(_ADAPTERS))
def test_adapter_restarts_after_prior_spawn_exits(
    adapter_name: str,
    tmp_path: Path,
    fake_cli_fixture: FakeCLIHandle,
) -> None:
    """After a spawn exits, the adapter restarts into a fresh session."""
    adapter_cls, model = _ADAPTERS[adapter_name]
    workdir = _git_workdir(tmp_path)
    adapter = adapter_cls()

    first = adapter.spawn(
        prompt=f"{adapter_name}-restart-1",
        workdir=workdir,
        model_config=model,
        session_id=f"{adapter_name}-conf-restart-1",
    )
    assert _reap_via_proc(first, timeout_s=8.0) == 0
    with contextlib.suppress(Exception):
        adapter_cls.cancel_timeout(first)

    second = adapter.spawn(
        prompt=f"{adapter_name}-restart-2",
        workdir=workdir,
        model_config=model,
        session_id=f"{adapter_name}-conf-restart-2",
    )
    assert _reap_via_proc(second, timeout_s=8.0) == 0
    with contextlib.suppress(Exception):
        adapter_cls.cancel_timeout(second)
    assert int(getattr(second, "pid", 0)) != int(getattr(first, "pid", -1))
