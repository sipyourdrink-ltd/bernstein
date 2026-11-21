"""``bernstein plan show`` prints the stored YAML body unchanged (#7956).

The body went through Rich, so ``name: '[bold]Garden[/bold]'`` printed as ``name: 'Garden'`` and a
140-character ``summary`` wrapped at the terminal width.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.plan_archive_cmd import plan_show

SUMMARY = "s" * 140
BODY = f"name: '[bold]Garden[/bold]'\nsummary: {SUMMARY}\nstages: []\n"


@pytest.fixture
def plan_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    active = tmp_path / "plans" / "active"
    active.mkdir(parents=True)
    path = active / "flowers.yaml"
    path.write_bytes(BODY.encode())
    monkeypatch.chdir(tmp_path)
    return path


def test_the_body_after_the_comments_is_the_stored_yaml(plan_file: Path) -> None:
    result = CliRunner().invoke(plan_show, ["flowers"], terminal_width=60)

    assert result.exit_code == 0
    assert result.output.endswith(plan_file.read_text())
    assert "[bold]Garden[/bold]" in result.output
    assert SUMMARY in result.output


def test_the_comments_still_name_the_selected_file(plan_file: Path) -> None:
    result = CliRunner().invoke(plan_show, ["flowers"], terminal_width=60)

    header = result.output[: -len(BODY)]
    assert "# state: active" in header
    assert "flowers.yaml" in header
