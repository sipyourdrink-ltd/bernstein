"""``bernstein logs tail --lines`` takes zero as "no stored lines" and refuses a negative count (#7872).

The initial slice used ``[-lines:]``. ``[-0:]`` is the whole list, so ``--lines 0`` printed every
stored line, and ``[1:]`` from ``--lines -1`` printed all but the first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import logs_group_cmd
from bernstein.cli.commands.logs_group_cmd import logs_tail

STORED = ["first stored line", "second stored line", "third stored line"]


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    (tmp_path / "agent-abc.log").write_text("\n".join(STORED) + "\n")
    return tmp_path


def _tail(runtime: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(logs_tail, ["--runtime-dir", str(runtime), *args])
    return result.exit_code, result.output


def test_zero_lines_prints_no_stored_line(runtime: Path) -> None:
    code, out = _tail(runtime, "--lines", "0")
    assert code == 0
    assert not any(line in out for line in STORED)


def test_a_negative_count_is_a_usage_error(runtime: Path) -> None:
    code, out = _tail(runtime, "--lines", "-1")
    assert code == 2
    assert "--lines" in out


@pytest.mark.parametrize(("count", "shown"), [("2", STORED[1:]), ("50", STORED)])
def test_a_positive_count_prints_the_last_lines(runtime: Path, count: str, shown: list[str]) -> None:
    code, out = _tail(runtime, "--lines", count)
    assert code == 0
    assert all(line in out for line in shown)
    assert all(line not in out for line in STORED if line not in shown)


def _stop_following(_seconds: float) -> None:
    raise KeyboardInterrupt


def test_follow_with_zero_lines_prints_only_new_lines(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs_group_cmd.time, "sleep", _stop_following)
    code, out = _tail(runtime, "--follow", "--lines", "0")
    assert code == 0
    assert "following" in out
    assert not any(line in out for line in STORED)


def test_follow_with_a_count_prints_that_many_stored_lines(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs_group_cmd.time, "sleep", _stop_following)
    code, out = _tail(runtime, "--follow", "--lines", "1")
    assert code == 0
    assert STORED[2] in out
    assert STORED[0] not in out
