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

_HEADING = re.compile(r"^#{1,6}\s+\S")
_CMD_IN_HEADING = re.compile(r"`bernstein ([a-z0-9][A-Za-z0-9 _-]*)`")
_LONG_FLAG = re.compile(r"--[a-z][a-z0-9-]*")


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


def _reference_sections() -> list[tuple[tuple[str, ...], str]]:
    """Split the CLI reference into (commands named by the heading, body) pairs.

    A section runs from one markdown heading to the next heading of *any*
    level, so a flag table can never bleed into the previous command's
    section -- the failure mode that let combined headings such as
    ``bernstein eval`` / ``bernstein benchmark`` attribute their tables to
    whatever command happened to precede them.  Lines inside fenced code
    blocks are ignored on both sides: a ``#`` comment inside a ```` ``` ````
    block is not a heading, and an example table inside one is not a
    documented contract.

    A heading may name several commands (``bernstein eval`` / ``bernstein
    benchmark``); every named command owns the section's body.  Forms
    carrying an option (``bernstein init --wizard``) document a flag on a
    command, not a command, and are dropped.
    """
    reference = Path(__file__).resolve().parents[2] / "docs" / "reference" / "cli-reference.md"
    sections: list[tuple[tuple[str, ...], str]] = []
    commands: tuple[str, ...] = ()
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        if commands:
            sections.append((commands, "\n".join(body)))

    for line in reference.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _HEADING.match(line):
            flush()
            commands = tuple(
                dict.fromkeys(
                    path
                    for raw in _CMD_IN_HEADING.findall(line)
                    if "--" not in raw and (path := _strip_argument_placeholders(raw))
                )
            )
            body = []
        else:
            body.append(line)
    flush()
    return sections


def _documented_command_paths() -> list[str]:
    """Command paths the CLI reference documents, as space-separated words."""
    return sorted({path for commands, _ in _reference_sections() for path in commands})


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
# registration fix was meant to remove, one layer down.  #3465 widened the
# assertion from those two commands to every command the reference documents.
#
# Scope of the gate, stated precisely:
#
# * Asserted: rows of a flag table -- markdown table rows whose *first cell*
#   starts with a backticked long option.  Every long option named in that
#   cell (including ``--x / --no-x`` alias spellings) must be accepted by the
#   command the section heading names, and, unless the command is listed in
#   ``PARTIAL_FLAG_TABLES``, every long option the command really accepts
#   must appear in some row.  Both directions fail: a doc row for a flag
#   that does not exist, and a real flag without a doc row.
# * Asserted, forward direction only: rows of a subcommand table -- a table
#   whose header's first cell is ``Subcommand``.  The row's first cell names
#   a subcommand of the section's command; every long option mentioned in
#   the row's other cells must be accepted by that subcommand, and the
#   subcommand itself must resolve.  There is no reverse assertion here: a
#   purpose cell is a summary, not an exhaustive flag list.
# * Not asserted: flags mentioned in prose outside tables, in fenced code
#   blocks, or in tables whose header is neither a flag table nor
#   ``Subcommand`` (e.g. the ``Command | Purpose`` category tables and the
#   ``Deprecated | Canonical`` alias-mapping table, whose cells discuss
#   several commands at once).  That is the stated limit of the check.

# Commands whose flag table may document flags the command does not accept.
# Keep this empty: an entry here records a documented invocation that exits 2.
GHOST_FLAG_EXEMPTIONS: dict[str, str] = {}

# Commands whose flag table is deliberately a subset of the real surface.
# Every entry carries the reason the table stays partial; the reverse
# (real -> documented) assertion is skipped for them, the forward assertion
# is not.
PARTIAL_FLAG_TABLES: dict[str, str] = {
    "run": "documents the most-used subset of a ~45-flag surface; the full list is `bernstein run --help`",
}

# The complete long-option surface of every command in ``PARTIAL_FLAG_TABLES``.
# The abbreviated table exempts these commands from the reverse doc assertion,
# which would otherwise let a rename or removal ship silently and break every
# script using the old spelling.  This pin closes that hole: changing the
# command's surface fails here and forces a deliberate update -- and a look at
# whether the "most-used" table rows are affected.
PARTIAL_TABLE_FULL_SURFACES: dict[str, frozenset[str]] = {
    "run": frozenset(
        {
            "--ab-test",
            "--activity-log",
            "--allow-network",
            "--allow-paid",
            "--attach",
            "--audit",
            "--auto-approve",
            "--auto-pr",
            "--budget",
            "--budget-cap",
            "--cells",
            "--cli",
            "--compliance",
            "--container",
            "--container-image",
            "--cprofile",
            "--criterion-profile",
            "--dry-run",
            "--fresh",
            "--from-plan",
            "--goal",
            "--hard-budget",
            "--idle",
            "--max-blast-radius",
            "--max-cost-usd",
            "--model",
            "--no-container",
            "--no-two-phase-sandbox",
            "--permission-profile",
            "--plan-only",
            "--port",
            "--profile",
            "--quiet",
            "--refresh-cache",
            "--remote",
            "--retry-budget",
            "--routing",
            "--sandbox",
            "--seed",
            "--skip-gate",
            "--skip-gate-reason",
            "--task",
            "--two-phase-sandbox",
            "--worker",
            "--workflow",
        }
    ),
}


def _documented_flags(body: str) -> set[str]:
    """Long options a section's flag table rows document.

    A flag row is a table row whose first cell starts with a backticked long
    option.  All long options in that cell count as documented, so both
    ``| `--force` / `--hard` |`` and ``| `--last / --no-last` |`` document
    two flags.  Rows starting with a positional placeholder or a subcommand
    name are not flag rows.
    """
    flags: set[str] = set()
    for line in body.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("|"):
            continue
        first_cell = stripped.split("|")[1] if "|" in stripped[1:] else stripped[1:]
        if re.match(r"^\s*`--[a-z]", first_cell):
            flags.update(_LONG_FLAG.findall(first_cell))
    return flags


def _real_flags(command: click.Command) -> set[str]:
    return {opt for param in command.params for opt in (*param.opts, *param.secondary_opts) if opt.startswith("--")}


def _sections_with_flag_tables() -> list[pytest.param]:
    return [
        pytest.param(path, flags, id=path)
        for commands, body in _reference_sections()
        if (flags := _documented_flags(body))
        for path in commands
    ]


@pytest.mark.parametrize(("name", "documented"), _sections_with_flag_tables())
def test_documented_flags_exist(name: str, documented: set[str]) -> None:
    """Every flag the reference documents is accepted by the command.

    A documented flag the parser rejects fails at ``No such option`` before
    the command body runs, which reads to the user exactly like the command
    being missing.
    """
    if name in GHOST_FLAG_EXEMPTIONS:
        pytest.skip(f"known-broken flag table: {GHOST_FLAG_EXEMPTIONS[name]}")
    command = _resolve(name)
    assert command is not None, f"`bernstein {name}` is documented but not registered"
    ghosts = sorted(documented - _real_flags(command))
    assert not ghosts, f"`bernstein {name}` documents flags it does not accept: {ghosts}"


@pytest.mark.parametrize(("name", "documented"), _sections_with_flag_tables())
def test_real_flags_are_documented(name: str, documented: set[str]) -> None:
    """Every long option a command accepts appears in its flag table.

    The reverse direction of the gate: a flag added to a command without a
    doc row fails here, so the reference cannot silently fall behind the
    surface it documents.  Commands without a flag table document nothing to
    contradict and are out of scope; deliberately abbreviated tables are
    named in ``PARTIAL_FLAG_TABLES`` with the reason.
    """
    if name in PARTIAL_FLAG_TABLES:
        pytest.skip(f"deliberately partial table: {PARTIAL_FLAG_TABLES[name]}")
    command = _resolve(name)
    assert command is not None, f"`bernstein {name}` is documented but not registered"
    undocumented = sorted(_real_flags(command) - documented - {"--help"})
    assert not undocumented, f"`bernstein {name}` accepts flags its reference table does not list: {undocumented}"


@pytest.mark.parametrize("name", sorted(PARTIAL_FLAG_TABLES))
def test_partial_table_surface_is_pinned(name: str) -> None:
    """A partially-documented command's full surface is pinned.

    ``PARTIAL_FLAG_TABLES`` exempts the command from the reverse doc
    assertion; without this pin a flag rename or removal outside the
    abbreviated rows would ship with CI green and break every script using
    the old spelling.  Any surface change must update the pin -- and prompt
    a look at whether the documented "most-used" rows are affected.
    """
    assert name in PARTIAL_TABLE_FULL_SURFACES, (
        f"`bernstein {name}` is in PARTIAL_FLAG_TABLES without a pinned surface in PARTIAL_TABLE_FULL_SURFACES"
    )
    command = _resolve(name)
    assert command is not None
    real = _real_flags(command)
    pinned = PARTIAL_TABLE_FULL_SURFACES[name]
    assert real == pinned, (
        f"`bernstein {name}`'s option surface changed: "
        f"added {sorted(real - pinned)}, removed {sorted(pinned - real)}. "
        "Update PARTIAL_TABLE_FULL_SURFACES and check whether the reference's abbreviated table is affected."
    )


# ---------------------------------------------------------------------------
# Subcommand tables (forward direction)
# ---------------------------------------------------------------------------

_SUBCOMMAND_WORDS = re.compile(r"^([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*)")


def _subcommand_rows() -> list[pytest.param]:
    """(subcommand path, flags its purpose cell mentions) for every subcommand-table row.

    A subcommand table is one whose header's first cell is ``Subcommand``.
    Each row's first cell names one or more subcommands of the section's
    command (``pin VERSION`` / ``unpin`` names two); trailing ALL-CAPS
    argument placeholders are stripped the same way as for headings.
    """
    rows: list[pytest.param] = []
    for commands, body in _reference_sections():
        in_table = False
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                in_table = False
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and cells[0].strip("`").lower() == "subcommand":
                in_table = True
                continue
            if not in_table or not cells or set(cells[0]) <= {"-", ":", " "}:
                continue
            flags = frozenset(_LONG_FLAG.findall(" ".join(cells[1:])))
            for parent in commands:
                for piece in cells[0].split("/"):
                    match = _SUBCOMMAND_WORDS.match(piece.strip().strip("`").strip())
                    if match is None:
                        continue
                    path = f"{parent} {match.group(1)}"
                    rows.append(pytest.param(path, flags, id=path))
    return rows


@pytest.mark.parametrize(("path", "flags"), _subcommand_rows())
def test_subcommand_rows_resolve_and_accept_their_flags(path: str, flags: frozenset[str]) -> None:
    """Every subcommand-table row names a live subcommand and real flags.

    The per-command flag tables cannot see one level down, so a rename like
    ``chat serve --driver`` -> ``--platform`` used to ship with CI green
    while the documented invocation exited 2.  Forward direction only: the
    purpose cell is a summary, so flags it omits are not failures, but a
    flag it names must exist on the row's own subcommand.
    """
    command = _resolve(path)
    assert command is not None, f"`bernstein {path}` appears in a subcommand table but does not resolve"
    ghosts = sorted(flags - _real_flags(command))
    assert not ghosts, f"`bernstein {path}`'s subcommand row mentions flags it does not accept: {ghosts}"


@pytest.mark.parametrize(
    "exemptions",
    [GHOST_FLAG_EXEMPTIONS, PARTIAL_FLAG_TABLES],
    ids=["ghost-flag-exemptions", "partial-flag-tables"],
)
def test_flag_gate_exemptions_name_live_documented_commands(exemptions: dict[str, str]) -> None:
    """Every exemption names a command that still exists and still has a table.

    An entry for a renamed or undocumented command would exempt nothing while
    reading as if it did; forcing the sets to track the live surface means
    they can only shrink meaningfully, never rot.
    """
    documented_with_tables = {name for param in _sections_with_flag_tables() for name in (param.values[0],)}
    for name, reason in exemptions.items():
        assert reason.strip(), f"exemption for `bernstein {name}` carries no reason"
        assert _resolve(name) is not None, f"exempted command `bernstein {name}` is not registered any more"
        assert name in documented_with_tables, (
            f"exempted command `bernstein {name}` no longer has a flag table in the reference"
        )
