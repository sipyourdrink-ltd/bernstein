"""Only independently led process groups may be stamped onto sessions."""

from __future__ import annotations

import ast
import inspect
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bernstein.core.agents import spawner_core
from bernstein.core.config import platform_compat


@pytest.mark.parametrize(
    ("pid", "group", "own_group", "expected"),
    [
        (123, 123, 456, 123),
        (123, 456, 789, None),
        (123, 123, 123, None),
        (None, 123, 456, None),
        (0, 0, 456, None),
        (1, 1, 456, None),
    ],
)
def test_posix_group_validation(monkeypatch, pid, group, own_group, expected):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(
        spawner_core,
        "os",
        SimpleNamespace(
            name="posix",
            getpgid=lambda value: group,
            getpgrp=lambda: own_group,
        ),
        raising=False,
    )
    assert spawner_core._leader_pgid(pid) == expected


def test_missing_process(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(spawner_core.os, "name", "posix")

    def gone(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(spawner_core.os, "getpgid", gone, raising=False)
    assert spawner_core._leader_pgid(123) is None


@pytest.mark.parametrize(("pid", "expected"), [(123, 123), (1, 1), (0, None), (-1, None), (None, None)])
def test_windows_tree_leader(monkeypatch, pid, expected):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert spawner_core._leader_pgid(pid) == expected


def test_unknown_platform(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(spawner_core.os, "name", "other")
    assert spawner_core._leader_pgid(123) is None


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX process groups")
@pytest.mark.parametrize("isolated", [True, False])
def test_real_process_group(isolated):
    kwargs = platform_compat.process_group_popen_kwargs() if isolated else {}
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    try:
        if isolated:
            assert spawner_core._leader_pgid(proc.pid) == proc.pid == os.getpgid(proc.pid)
        else:
            assert spawner_core._leader_pgid(proc.pid) is None
    finally:
        if isolated:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=5)


def test_both_spawn_paths_stamp_pgid_immediately():
    tree = ast.parse(inspect.getsource(spawner_core))
    count = 0
    for node in ast.walk(tree):
        for value in vars(node).values():
            if not isinstance(value, list):
                continue
            for index, statement in enumerate(value):
                if not isinstance(statement, ast.Assign):
                    continue
                if ast.unparse(statement) != "session.pid = result.pid":
                    continue
                assert ast.unparse(value[index + 1]) == "session.pgid = _leader_pgid(result.pid)"
                count += 1
    assert count == 2
