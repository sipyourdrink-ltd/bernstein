"""``bernstein prompts compare --json`` writes one parseable JSON object (#7871).

The JSON went through Rich, which wrapped long strings at the terminal width (so stdout no longer
parsed) and read ``[bold]`` in a prompt name as markup and dropped it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.prompts_cmd import prompts_compare
from bernstein.core.tokens.prompt_versioning import PromptRegistry

NAME = "a-prompt-name-[bold]-that-is-much-longer-than-forty-eight-columns"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    registry = PromptRegistry(tmp_path / ".sdd")
    registry.add_version(NAME, "first version", set_active=True)
    registry.add_version(NAME, "second version")
    return tmp_path


def test_json_parses_and_keeps_the_name_at_a_narrow_width(workspace: Path) -> None:
    result = CliRunner().invoke(prompts_compare, [NAME, "1", "2", "--json"], terminal_width=48, env={"COLUMNS": "48"})
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == PromptRegistry(workspace / ".sdd").compare_versions(NAME, 1, 2)
    assert NAME in result.output


def test_a_missing_version_still_exits_1(workspace: Path) -> None:
    result = CliRunner().invoke(prompts_compare, [NAME, "1", "9", "--json"])
    assert result.exit_code == 1
