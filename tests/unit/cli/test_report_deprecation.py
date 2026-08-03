'''
Tests backward compatibility of the new
`report` group subcommands; 
the test can be removed after v4.0.0.
'''


import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli


@pytest.mark.parametrize(
    "old_args, new_args, deprecated_name, new_name",
    [
        (
            ["postmortem", "--help"],
            ["report", "postmortem", "--help"],
            "bernstein postmortem",
            "bernstein report postmortem",
        ),
        (
            ["incident", "--help"],
            ["report", "incident", "--help"],
            "bernstein incident",
            "bernstein report incident",
        ),
        (
            ["commit-stats", "--help"],
            ["report", "commits", "--help"],
            "bernstein commit-stats",
            "bernstein report commits",
        ),
    ],
)
def test_report_command_aliases_deprecation(
    old_args: list[str],
    new_args: list[str],
    deprecated_name: str,
    new_name: str,
) -> None:
    runner = CliRunner()
    result_new = runner.invoke(cli, new_args)
    result_old = runner.invoke(cli, old_args)
    assert result_new.exit_code == 0, f"Error on new command: {result_new.output}"
    assert result_old.exit_code == 0, f"Error on old command: {result_old.output}"
    assert "(Deprecated)" in result_old.output
    assert new_name in result_old.output
    assert "(Deprecated)" not in result_new.output