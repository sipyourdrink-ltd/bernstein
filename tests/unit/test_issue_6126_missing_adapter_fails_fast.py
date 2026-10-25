"""A run with no adapter configured fails once and exits; it is not retried forever (#6126).

With no ``--cli``, no ``BERNSTEIN_ADAPTER`` and no seed, the orchestrator
subprocess logs ``FATAL: no adapter configured`` and exits ``1``. Nothing about
the next launch differs, but the recovery watchdog treated that death as a crash
and respawned it every poll, while the foreground command reported success and
left a task server and a watchdog running behind a run that could never start.

Three layers are pinned here:

* the pre-flight check that refuses to start such a run at all,
* the stand-down marker the orchestrator writes when it hits the same
  configuration failure through some other launch path, and
* the watchdog honouring that marker, and the next run clearing it.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.orchestration.bootstrap import _watchdog_check_process, bootstrap_from_goal
from bernstein.core.orchestration.orchestrator import NO_ADAPTER_CONFIGURED, _stand_down_watchdog
from bernstein.core.orchestration.preflight import check_adapter_configured
from bernstein.core.orchestration.process_utils import LIVENESS_GONE
from bernstein.core.server.server_launch import _clean_stale_runtime

_MARKER = Path(".sdd") / "runtime" / "spawner-deliberate-stop"


@pytest.fixture(autouse=True)
def _no_ambient_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own shell from supplying an adapter or a seed."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    monkeypatch.delenv("BERNSTEIN_SEED_PATH", raising=False)


# ---------------------------------------------------------------------------
# Pre-flight: fail before anything is started
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cli", [None, "", "auto"])
def test_no_adapter_anywhere_exits_with_the_existing_message(cli: str | None, tmp_path: Path) -> None:
    """Nothing names an adapter: exit 1 with the same text the orchestrator would have logged."""
    with patch("bernstein.core.orchestration.preflight.console") as console, pytest.raises(SystemExit) as exc:
        check_adapter_configured(cli, tmp_path)

    assert exc.value.code == 1
    console.print.assert_called_once()
    assert console.print.call_args.args[0] == NO_ADAPTER_CONFIGURED


def test_message_is_printed_without_rich_markup_interpretation(tmp_path: Path) -> None:
    """The text carries quotes and flags; it must reach the operator verbatim."""
    with patch("bernstein.core.orchestration.preflight.console") as console, pytest.raises(SystemExit):
        check_adapter_configured("auto", tmp_path)

    assert console.print.call_args.kwargs["markup"] is False


def test_a_concrete_cli_is_enough(tmp_path: Path) -> None:
    """``--cli codex`` reaches the orchestrator as ``--adapter codex``."""
    with patch("bernstein.core.orchestration.preflight.console") as console:
        check_adapter_configured("codex", tmp_path)

    console.print.assert_not_called()


def test_the_env_var_is_enough(tmp_path: Path) -> None:
    with patch("bernstein.core.orchestration.preflight.console") as console:
        check_adapter_configured("auto", tmp_path, env={"BERNSTEIN_ADAPTER": "codex"})

    console.print.assert_not_called()


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_env_var_is_not_an_adapter(value: str, tmp_path: Path) -> None:
    """The orchestrator strips it and treats the result as unset, so this must too."""
    with patch("bernstein.core.orchestration.preflight.console"), pytest.raises(SystemExit):
        check_adapter_configured("auto", tmp_path, env={"BERNSTEIN_ADAPTER": value})


def test_an_existing_seed_is_left_to_the_orchestrator(tmp_path: Path) -> None:
    """Whether the seed's ``cli`` resolves is the orchestrator's call; only certain failure is refused here."""
    (tmp_path / "bernstein.yaml").write_text("cli: auto\n")

    with patch("bernstein.core.orchestration.preflight.console") as console:
        check_adapter_configured("auto", tmp_path)

    console.print.assert_not_called()


def test_a_seed_elsewhere_named_by_env_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestrator reads ``BERNSTEIN_SEED_PATH`` before ``workdir/bernstein.yaml``; so does the check."""
    elsewhere = tmp_path / "staged" / "custom-seed.yaml"
    elsewhere.parent.mkdir()
    elsewhere.write_text("cli: auto\n")
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(elsewhere))
    workdir = tmp_path / "project"
    workdir.mkdir()

    with patch("bernstein.core.orchestration.preflight.console") as console:
        check_adapter_configured("auto", workdir)

    console.print.assert_not_called()


def test_inline_goal_run_stops_before_the_server_and_watchdog_exist(tmp_path: Path) -> None:
    """``bernstein -g`` with nothing configured must not leave a server or watchdog behind."""
    # A workspace that already exists is not a first run, so no seed is auto-written.
    (tmp_path / ".sdd").mkdir()

    with (
        patch("bernstein.core.orchestration.bootstrap.preflight_checks"),
        patch("bernstein.core.orchestration.bootstrap.supervised_server") as server,
        patch("bernstein.core.orchestration.bootstrap._start_spawner") as spawner,
        patch("bernstein.core.orchestration.bootstrap._start_watchdog") as watchdog,
        patch("bernstein.core.orchestration.preflight.console"),
        pytest.raises(SystemExit) as exc,
    ):
        bootstrap_from_goal(goal="say hello", workdir=tmp_path)

    assert exc.value.code == 1
    server.assert_not_called()
    spawner.assert_not_called()
    watchdog.assert_not_called()


# ---------------------------------------------------------------------------
# Orchestrator subprocess: stand the watchdog down on a config failure
# ---------------------------------------------------------------------------


def test_stand_down_writes_the_marker_with_its_reason(tmp_path: Path) -> None:
    _stand_down_watchdog(tmp_path, "no-adapter-configured")

    assert (tmp_path / _MARKER).read_text() == "no-adapter-configured"


def test_stand_down_is_best_effort(tmp_path: Path) -> None:
    """An unwritable runtime dir must not turn a clean exit(1) into a traceback."""
    (tmp_path / ".sdd").write_text("a file where the directory should be")

    _stand_down_watchdog(tmp_path, "no-adapter-configured")  # must not raise


def test_the_watchdog_does_not_respawn_after_a_stand_down(tmp_path: Path) -> None:
    """The marker the orchestrator writes is the one the watchdog already reads."""
    _stand_down_watchdog(tmp_path, "no-adapter-configured")
    restart_fn = MagicMock(return_value=99999)

    _alive_since, restarts, _give_up = _watchdog_check_process(
        name="Orchestrator",
        pid=None,
        alive_since=None,
        restarts=0,
        give_up_logged=False,
        max_restarts=5,
        reset_after_s=120.0,
        now=time.monotonic(),
        restart_fn=restart_fn,
        liveness=LIVENESS_GONE,
        workdir=tmp_path,
    )

    restart_fn.assert_not_called()
    assert restarts == 0


def test_the_next_run_clears_a_stale_marker(tmp_path: Path) -> None:
    """A marker from a failed run must not make a later run's watchdog skip a real crash."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "spawner-deliberate-stop").write_text("no-adapter-configured")

    _clean_stale_runtime(tmp_path)

    assert not (runtime / "spawner-deliberate-stop").exists()


def test_clearing_is_a_no_op_when_there_is_no_marker(tmp_path: Path) -> None:
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

    _clean_stale_runtime(tmp_path)  # must not raise


def test_orchestrator_with_no_adapter_exits_1_and_stands_the_watchdog_down(tmp_path: Path) -> None:
    """End to end: the real subprocess logs the FATAL, exits 1, and leaves the marker."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BERNSTEIN_")}

    proc = subprocess.run(
        [sys.executable, "-m", "bernstein.core.orchestration.orchestrator"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 1
    assert "FATAL: no adapter configured" in proc.stdout + proc.stderr
    assert (tmp_path / _MARKER).read_text() == "no-adapter-configured"
