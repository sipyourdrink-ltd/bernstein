"""``bernstein explain`` shows every example whole at 80 columns (#7945).

The examples sat in a table column with ``no_wrap=True`` inside a panel, so at 80 columns the Bash
completion example was cut to ``--shell bash…``.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.explain_help_cmd import _COMMAND_EXAMPLES, explain_help_cmd


@pytest.mark.parametrize("command", ["completions", *sorted(set(_COMMAND_EXAMPLES) - {"completions"})])
def test_every_example_and_description_is_whole_at_80_columns(command: str) -> None:
    result = CliRunner().invoke(explain_help_cmd, [command], terminal_width=80, env={"COLUMNS": "80"})
    assert result.exit_code == 0, result.output
    for example, description in _COMMAND_EXAMPLES[command]["examples"]:
        assert example in result.output
        assert description in result.output
    assert "…" not in result.output


def test_the_completion_examples_for_each_shell() -> None:
    result = CliRunner().invoke(explain_help_cmd, ["completions"], terminal_width=80, env={"COLUMNS": "80"})
    for shell in ("bash", "zsh"):
        assert f'eval "$(bernstein completions --shell {shell})"' in result.output
    assert "bernstein completions --shell fish | source" in result.output
