"""Shared helpers for adapter spawn/name unit tests.

Every ``test_adapter_*.py`` has the same three primitives: a fixture that
patches out the timeout watchdog thread, a ``Popen`` mock factory, and a
helper that extracts the inner CLI command from the worker-wrapped
``subprocess.Popen`` argv.  Extracted here so Sonar's new-code
duplication stops flagging each new adapter test as a 20-line clone.
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def no_watchdog_threads() -> Generator[None, None, None]:
    """Disable watchdog threads to avoid 'can't start new thread' on CI.

    Apply via ``pytestmark = pytest.mark.usefixtures("no_watchdog_threads")``
    at module scope, or by naming the fixture in an individual test's
    parameters.
    """
    with patch("bernstein.adapters.base.CLIAdapter._start_timeout_watchdog", return_value=None):
        yield


def make_popen_mock(pid: int) -> MagicMock:
    """Return a ``MagicMock`` spec'd on ``subprocess.Popen`` with ``pid`` set."""
    mock = MagicMock(spec=subprocess.Popen)
    mock.pid = pid
    return mock


def inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the ``--`` worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]
