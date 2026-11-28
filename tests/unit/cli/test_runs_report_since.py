"""``bernstein runs report --since`` refuses a duration that names no past window (#7942).

``--since nonsense`` exited with a Python traceback, and ``nan``, ``inf`` and ``-2h`` were accepted
although the flag names a window in the past.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import runs_cmd


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def _fake_list(_sdd: Path, *, since: float | None = None) -> list[Any]:
        calls.append(since)
        return []

    monkeypatch.setattr(runs_cmd, "list_finished_runs", _fake_list)
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1_000_000.0)
    return calls


def _report(tmp_path: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(runs_cmd.runs_report_cmd, ["--workdir", str(tmp_path), *args])
    return result.exit_code, result.output


@pytest.mark.parametrize("value", ["nonsense", "nan", "inf", "-2h", "--1d", "6x"])
def test_an_invalid_duration_is_a_usage_error(tmp_path: Path, seen: list[Any], value: str) -> None:
    code, out = _report(tmp_path, f"--since={value}")
    assert code == 2
    assert "--since" in out
    assert "6h" in out
    assert "Traceback" not in out
    assert seen == []


def test_a_supported_duration_filters_by_start_time(tmp_path: Path, seen: list[Any]) -> None:
    code, _out = _report(tmp_path, "--since", "6h")
    assert code == 0
    assert seen == [1_000_000.0 - 6 * 3600]


def test_no_since_reads_every_run(tmp_path: Path, seen: list[Any]) -> None:
    code, _out = _report(tmp_path)
    assert code == 0
    assert seen == [None]
