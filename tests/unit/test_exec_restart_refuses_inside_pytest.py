"""``exec_restart`` must refuse to replace a running pytest process.

``bernstein.cli.run_bootstrap.exec_restart`` calls ``os.execv``, which replaces
the current process image. A test that hands the dashboard a bare ``MagicMock``
gets a truthy ``_restart_on_exit`` (every attribute of a bare MagicMock is
truthy), the production path then reaches ``exec_restart``, and the pytest
process is replaced by ``bernstein run``. The observable result is a run that
prints no test results and exits 0 - indistinguishable from success, with a
whole file having silently stopped protecting anything.

The guard closes the hole at the source: inside a pytest process the call
raises instead of exec'ing, so the offending test fails loudly.
"""

from __future__ import annotations

import os
from typing import Any, NoReturn

import pytest

from bernstein.cli.run_bootstrap import exec_restart


def test_exec_restart_raises_inside_a_pytest_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under pytest, exec_restart refuses rather than replacing the process."""

    def _must_not_exec(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("os.execv must not run inside a pytest process")

    monkeypatch.setattr(os, "execv", _must_not_exec)
    assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest sets this for every running test"

    with pytest.raises(RuntimeError, match="pytest"):
        exec_restart()


def test_exec_restart_refusal_names_the_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error tells the reader why the restart was refused."""

    def _must_not_exec(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("os.execv must not run inside a pytest process")

    monkeypatch.setattr(os, "execv", _must_not_exec)

    with pytest.raises(RuntimeError) as excinfo:
        exec_restart()
    assert "PYTEST_CURRENT_TEST" in str(excinfo.value)


def test_exec_restart_still_execs_outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production path is unchanged when no pytest process is running."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    calls: list[tuple[str, list[str]]] = []

    def _record(executable: str, argv: list[str]) -> None:
        calls.append((executable, argv))

    monkeypatch.setattr(os, "execv", _record)

    exec_restart()

    assert len(calls) == 1
    _executable, argv = calls[0]
    assert argv[1:] == ["-m", "bernstein.cli.main", "run"]
