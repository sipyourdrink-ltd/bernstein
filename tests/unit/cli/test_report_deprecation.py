"""Backward compatibility of the `report` group subcommands.

The legacy top-level `postmortem`, `incident`, and `commit-stats` commands
remain as deprecated aliases through the 3.x line; these tests pin that the
aliases stay behaviourally identical to their `report` replacements and that
the deprecation notice goes to stderr. The module can be removed together
with the aliases in v4.0.0.
"""

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli


def _without_usage_lines(text: str) -> str:
    """Drop `Usage:` lines, which legitimately differ by program name."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("Usage:"))


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


@pytest.mark.parametrize(
    "old_args, new_args, warning_fragment",
    [
        (
            ["postmortem"],
            ["report", "postmortem"],
            "'bernstein postmortem' is deprecated",
        ),
        (
            ["incident"],
            ["report", "incident"],
            "'bernstein incident' is deprecated",
        ),
        (
            ["commit-stats"],
            ["report", "commits"],
            "'bernstein commit-stats' is deprecated",
        ),
    ],
)
def test_alias_execution_matches_report_subcommand_output(
    old_args: list[str],
    new_args: list[str],
    warning_fragment: str,
) -> None:
    """An alias invocation must produce the same exit code and the same
    stdout as its `report` replacement (usage lines aside, which differ by
    program name only), with the deprecation notice on stderr — so existing
    scripts keep working unchanged until v4.0.0."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result_new = runner.invoke(cli, new_args)
        result_old = runner.invoke(cli, old_args)
    assert result_old.exit_code == result_new.exit_code, (
        f"exit code diverged: old={result_old.exit_code} new={result_new.exit_code}\n"
        f"old output: {result_old.output}\nnew output: {result_new.output}"
    )
    assert warning_fragment in result_old.stderr
    assert "deprecated" not in result_new.stderr
    assert _without_usage_lines(result_old.stdout) == _without_usage_lines(result_new.stdout)


def test_alias_usage_line_keeps_legacy_program_name() -> None:
    """Forwarded invocations must render usage/help with the real program
    name (`bernstein incident`), not the auto-detected one — otherwise every
    usage or error message printed through an alias starts with `Usage: -`."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["incident"])
    assert result.exit_code == 0, result.output
    assert "Usage: bernstein incident" in result.stdout
