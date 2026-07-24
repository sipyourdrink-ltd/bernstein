"""``bernstein stop --force`` reports what it actually reaped (issue #2800).

The hard-stop summary printed ``len(killed_pids)`` - every PID it attempted -
so PIDs that were already dead or that resisted SIGKILL inflated the count.
``_count_reaped`` reports only the collected PIDs confirmed terminated.
"""

from __future__ import annotations

from typing import Any

from bernstein.cli.commands import stop_cmd


def test_count_reaped_excludes_survivors(monkeypatch: Any) -> None:
    """A PID still alive after the sweep is not counted as reaped."""
    # PIDs 1 and 2 are gone; PID 3 resisted SIGKILL and is still alive.
    monkeypatch.setattr(stop_cmd, "is_alive", lambda pid: pid == 3)

    assert stop_cmd._count_reaped({1, 2, 3}) == 2


def test_count_reaped_all_dead(monkeypatch: Any) -> None:
    monkeypatch.setattr(stop_cmd, "is_alive", lambda _pid: False)

    assert stop_cmd._count_reaped({10, 11}) == 2


def test_count_reaped_none_dead(monkeypatch: Any) -> None:
    monkeypatch.setattr(stop_cmd, "is_alive", lambda _pid: True)

    assert stop_cmd._count_reaped({10, 11}) == 0
