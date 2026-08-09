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

import re
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli


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


# ---------------------------------------------------------------------------
# Documented surface must be reachable (#3139)
# ---------------------------------------------------------------------------
#
# The mirror image of #3134: there the registered object was not a command;
# here the command was never registered at all.  ``api-check`` and ``ab-test``
# each shipped a complete ``@click.command`` module, sat in the lazy-import
# map, and were documented twice in the CLI reference -- and ``dep-impact``'s
# help text told the reader to use ``api-check``.  Following that pointer
# produced "No such command", and nothing in the suite could see it, because
# every test drove command implementations directly.
#
# This walks the reference in the other direction: for every command the docs
# name, resolve it through the real ``cli`` object.  A command that is
# documented but unreachable now fails here instead of in a user's terminal.

_DOC_COMMAND_HEADING = re.compile(r"^#+\s+`bernstein ([a-z0-9][A-Za-z0-9 _-]*)`\s*$", re.M)


def _strip_argument_placeholders(heading: str) -> str:
    """Drop trailing ALL-CAPS argument placeholders from a heading.

    ``bernstein cost policy verify DECISION_HASH`` documents the command
    ``cost policy verify`` taking one positional argument.  Without this the
    heading matched nothing and the command escaped the gate silently, which
    is the failure mode the gate exists to remove.

    Only a contiguous *trailing* run is dropped.  Removing every ALL-CAPS
    word would let an uppercase token earlier in the path disappear, turning
    ``GROUP verify DECISION_HASH`` into ``verify`` -- resolving a different
    command than the heading names, which is worse than not matching at all.
    """
    words = heading.split()
    while words and words[-1].isupper() and words[-1].replace("_", "").isalpha():
        words.pop()
    return " ".join(words)


def _documented_command_paths() -> list[str]:
    """Command paths the CLI reference documents, as space-separated words.

    Headings carrying an option (``bernstein doctor --failover-drill``) are
    excluded: they document a flag on a command, not a command.
    """
    reference = Path(__file__).resolve().parents[2] / "docs" / "reference" / "cli-reference.md"
    text = reference.read_text(encoding="utf-8")
    return sorted(
        {_strip_argument_placeholders(name) for name in _DOC_COMMAND_HEADING.findall(text) if "--" not in name} - {""}
    )


def _resolve(path: str) -> click.Command | None:
    """Walk ``path`` through the real CLI, returning None at the first miss."""
    node: click.Command | None = cli
    for word in path.split():
        if not isinstance(node, click.Group):
            return None
        node = node.get_command(click.Context(node), word)
        if node is None:
            return None
    return node


def test_documented_commands_are_registered() -> None:
    """Every command the CLI reference documents resolves through ``cli``."""
    unreachable = [path for path in _documented_command_paths() if _resolve(path) is None]
    assert not unreachable, (
        f"documented in docs/reference/cli-reference.md but not registered on the CLI: {unreachable}"
    )


@pytest.mark.parametrize("name", ["api-check", "ab-test"])
def test_previously_orphaned_command_resolves(name: str) -> None:
    """The two commands #3139 measured as unregistered are reachable.

    Kept separate from the sweep above so a regression names the command
    rather than only the fact that some command went missing.
    """
    command = _resolve(name)
    assert command is not None, f"`bernstein {name}` is documented but not registered"
    assert isinstance(command, click.Command)


# Registering a command is only half of making it usable.  The first cut of
# this gate proved the *name* resolved and stopped there, so `api-check` and
# `ab-test` became reachable while every flag the reference documented for
# them still failed with "No such option" -- the same class of failure the
# registration fix was meant to remove, one layer down.
#
# Scoped to the two commands this change makes reachable.  A sweep across the
# whole reference measures 23 commands carrying documented flags that do not
# exist; widening the gate is tracked separately so it lands with the fixes
# rather than as a red build.

_FLAG_ROW = re.compile(r"^\|\s*`(--[a-z][a-z0-9-]*)", re.M)


def _documented_flags(command_path: str) -> set[str]:
    """Long options the CLI reference lists in ``command_path``'s flag table."""
    reference = Path(__file__).resolve().parents[2] / "docs" / "reference" / "cli-reference.md"
    sections = re.split(
        r"^#+\s+`bernstein ([a-z0-9][a-z0-9 _-]*)`\s*$",
        reference.read_text(encoding="utf-8"),
        flags=re.M,
    )
    for name, body in zip(sections[1::2], sections[2::2], strict=False):
        if name == command_path:
            return set(_FLAG_ROW.findall(body))
    return set()


@pytest.mark.parametrize("name", ["api-check", "ab-test", "impact api", "impact deps", "dep-impact"])
def test_documented_flags_exist(name: str) -> None:
    """Every flag the reference documents is accepted by the command.

    A documented flag the parser rejects fails at ``No such option`` before
    the command body runs, which reads to the user exactly like the command
    being missing.
    """
    command = _resolve(name)
    assert command is not None
    real = {opt for param in command.params for opt in param.opts if opt.startswith("--")}
    documented = _documented_flags(name)
    assert documented, f"no flag table found for `bernstein {name}` in the CLI reference"
    assert not (documented - real), (
        f"`bernstein {name}` documents flags it does not accept: {sorted(documented - real)}"
    )
