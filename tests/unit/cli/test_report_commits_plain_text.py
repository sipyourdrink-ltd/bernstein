"""``bernstein report commits`` writes plain text when its output is redirected (#7947).

The table was rendered to a string with ``force_terminal=True`` at width 120 and that string was
printed again. Redirected at 80 columns, the file held terminal escape bytes, and the header and the
totals were broken onto extra lines.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.status_cmd import commit_stats_cmd

ENV = {"NO_COLOR": "1", "COLUMNS": "80"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "backend-agent")
    git("config", "user.email", "backend@example.invalid")
    (tmp_path / "a.txt").write_text("one line\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "add a line")
    return tmp_path


def _report(repo: Path, *args: str) -> str:
    result = CliRunner().invoke(commit_stats_cmd, ["--repo-dir", str(repo), *args], env=ENV, terminal_width=80)
    assert result.exit_code == 0, result.output
    return result.output


def test_redirected_text_has_no_escape_bytes_and_whole_lines(repo: Path) -> None:
    out = _report(repo)
    assert "\x1b" not in out
    assert all(len(line) <= 80 for line in out.splitlines())
    header = next(line for line in out.splitlines() if "Role" in line)
    for column in ("Role", "Commits", "Lines Added", "Lines Deleted"):
        assert column in header
    total = next(line for line in out.splitlines() if "Total" in line)
    assert "+1" in total
    assert "-0" in total


def test_text_totals_match_the_json_totals(repo: Path) -> None:
    data = json.loads(_report(repo, "--json"))
    assert data["total_commits"] == 1
    assert data["total_lines_added"] == 1
    total = next(line for line in _report(repo).splitlines() if "Total" in line)
    cells = [cell.strip() for cell in total.split("│") if cell.strip()]
    assert cells[1:] == [
        str(data["total_commits"]),
        f"+{data['total_lines_added']}",
        f"-{data['total_lines_deleted']}",
    ]
