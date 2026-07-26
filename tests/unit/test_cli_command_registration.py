"""Registration-level coverage for the top-level ``bernstein`` CLI group.

Issue #3134: ``bernstein merge`` shipped broken because a ``@click.command``
decorator stack landed on a private helper instead of on the command body.
The bare function was then handed to ``cli.add_command``, so the object
registered under ``merge`` was not a Click command at all.  Every existing
test drove command implementations directly, so nothing exercised the
top-level ``cli`` object and the defect stayed invisible.

These tests close that gap.  They walk ``cli.commands`` and drive the CLI
through ``CliRunner``, which is the only layer where a mis-wired decorator
is observable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click
import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli

if TYPE_CHECKING:
    from pathlib import Path


def _command_names() -> list[str]:
    return sorted(cli.commands)


def test_every_registered_entry_is_a_click_command() -> None:
    """No bare function may be registered on the top-level group.

    ``Group.add_command`` stores whatever object it is handed.  A function
    that lost its decorator stack is accepted silently and only fails when a
    user (or ``man-pages``) reaches it.
    """
    not_commands = {
        name: type(obj).__name__ for name, obj in cli.commands.items() if not isinstance(obj, click.Command)
    }
    assert not_commands == {}, f"Entries registered on the top-level CLI that are not click.Command: {not_commands}"


def test_every_registered_command_exposes_help() -> None:
    """Every registered entry must carry a help string the tree walker can read.

    ``generate_all_man_pages`` reads ``cmd.help`` for every entry in
    ``cli.commands``.  An entry without that attribute takes
    ``bernstein man-pages`` down.
    """
    missing = sorted(
        name for name, obj in cli.commands.items() if not (hasattr(obj, "help") and hasattr(obj, "short_help"))
    )
    assert missing == [], f"Registered commands the man-page tree walker cannot read: {missing}"


@pytest.mark.parametrize("name", _command_names())
def test_command_help_resolves_through_top_level_cli(name: str) -> None:
    """``bernstein <command> --help`` must resolve for every registered command.

    Driving this through the top-level ``cli`` object is the point: a
    module-level invocation of the implementation function would pass even
    when the registration is broken.
    """
    result = CliRunner().invoke(cli, [name, "--help"])
    assert result.exit_code == 0, (
        f"`bernstein {name} --help` exited {result.exit_code}: {result.output}\n{result.exception!r}"
    )


def test_merge_help_lists_documented_options() -> None:
    """``bernstein merge --help`` publishes the options documented for it.

    ``docs/reference/cli/task-lifecycle.md`` publishes a synopsis for this
    command, so the flags below are a documented contract.
    """
    result = CliRunner().invoke(cli, ["merge", "--help"])
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    for option in ("--pick", "--base", "--workdir", "--no-ff", "--message", "--dry-run", "--reject"):
        assert option in result.output, f"`bernstein merge --help` does not list {option}:\n{result.output}"


def test_merge_dry_run_reports_and_exits_zero(tmp_path: Path) -> None:
    """``bernstein merge --pick ... --dry-run`` runs the real command body.

    With no agents registered the command reports that nothing resolved and
    exits non-zero, which still proves the 216-line command body is reached:
    the broken registration could not get this far.
    """
    result = CliRunner().invoke(cli, ["merge", "--pick", "no-such-agent", "--workdir", str(tmp_path), "--dry-run"])
    assert not isinstance(result.exception, AttributeError), repr(result.exception)
    assert "no-such-agent" in result.output, result.output


def test_man_pages_completes(tmp_path: Path) -> None:
    """``bernstein man-pages`` walks the whole command tree without raising.

    It fails as collateral damage from any non-command entry in
    ``cli.commands``.
    """
    result = CliRunner().invoke(cli, ["man-pages", "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert list(tmp_path.glob("bernstein-merge.1")), sorted(p.name for p in tmp_path.iterdir())[:20]
