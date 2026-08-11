"""Unit tests for #3139: consolidating impact analysis under ``bernstein impact``.

Every assertion here is written so it can go red.  The first cut of this file
matched substrings against the whole ``--help`` body, and the group's own
docstring already contains the words it looked for: deleting
``impact_group.add_command(blast_radius_group, "blast")`` left
``test_impact_group_subcommands_registered`` green because "blast radius"
appears in the group description.  Registration is therefore asserted against
the group's command table and by object identity, not by substring.

The deprecated top-level aliases are asserted on the two properties the
migration actually promises: the arguments reach the underlying command, and
the warning is written to stderr.  Neither was covered before -- deleting the
``ctx.invoke`` forwarding call, or dropping ``err=True``, left the suite
green.  The stderr half is not cosmetic: a deprecation banner on stdout ends
up inside the payload a ``--json`` consumer parses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from bernstein.cli.commands.api_check_cmd import api_check_cmd
from bernstein.cli.commands.blast_radius_cmd import blast_radius_group
from bernstein.cli.commands.dep_impact_cmd import dep_impact_cmd
from bernstein.cli.commands.impact_cmd import blast_radius_alias_group, impact_group
from bernstein.cli.main import cli

_DEP_IMPACT_WARNING = "WARNING: 'bernstein dep-impact' is deprecated"
_BLAST_RADIUS_WARNING = "WARNING: 'bernstein blast-radius' is deprecated"


def _resolve(*path: str) -> click.Command:
    """Walk ``path`` through the real ``cli`` object, failing at the first miss."""
    node: click.Command = cli
    for word in path:
        assert isinstance(node, click.Group), f"`bernstein {' '.join(path)}`: {word!r} has no parent group"
        found = node.get_command(click.Context(node), word)
        assert found is not None, f"`bernstein {' '.join(path)}` is not registered"
        node = found
    return node


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_impact_group_is_registered() -> None:
    assert isinstance(_resolve("impact"), click.Group)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("api", api_check_cmd), ("deps", dep_impact_cmd), ("blast", blast_radius_group)],
)
def test_impact_subcommand_is_the_existing_command_object(name: str, expected: click.Command) -> None:
    """``impact <name>`` resolves to the module that already implemented it.

    Identity, not a substring of ``--help``: the group description mentions
    "API", "caller sites" and "blast radius", so a text match passes even
    when the subcommand is missing.
    """
    assert impact_group.commands.get(name) is expected
    assert _resolve("impact", name) is expected


def test_impact_blast_exposes_the_blast_radius_subcommands() -> None:
    assert set(blast_radius_group.commands) >= {"score", "show"}
    assert set(_resolve("impact", "blast").commands) == set(blast_radius_group.commands)  # type: ignore[attr-defined]


def test_deprecated_blast_alias_mirrors_the_canonical_group() -> None:
    """The alias copies subcommands at import time; a later addition must not skip it."""
    assert {name: cmd for name, cmd in blast_radius_alias_group.commands.items()} == dict(blast_radius_group.commands)


# ---------------------------------------------------------------------------
# Canonical commands are reachable and describe themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_summary"),
    [
        (("impact", "api"), "Detect breaking API changes"),
        (("impact", "deps"), "Analyse which call sites break"),
        (("impact", "blast", "score"), "Score a change described on the command line"),
    ],
)
def test_canonical_command_help_renders(path: tuple[str, ...], expected_summary: str) -> None:
    res = CliRunner().invoke(cli, [*path, "--help"])
    assert res.exit_code == 0, res.output
    assert expected_summary in res.output


@pytest.mark.parametrize("path", [("impact", "api"), ("impact", "deps"), ("impact", "blast")])
def test_canonical_help_does_not_advertise_the_deprecated_spelling(path: tuple[str, ...]) -> None:
    """The command this PR makes canonical must not tell the reader to use the alias.

    ``impact deps --help`` shipped the ``dep-impact`` examples verbatim, so
    the help of the replacement pointed at the surface the same change
    deprecates.
    """
    res = CliRunner().invoke(cli, [*path, "--help"])
    assert res.exit_code == 0, res.output
    for deprecated in ("bernstein dep-impact", "bernstein api-check", "bernstein blast-radius"):
        assert deprecated not in res.output, f"`bernstein {' '.join(path)} --help` still points at `{deprecated}`"


def test_canonical_path_emits_no_deprecation_warning() -> None:
    res = CliRunner().invoke(cli, ["impact", "deps", "--help"])
    assert res.exit_code == 0, res.output
    assert "deprecated" not in res.output.lower()


# ---------------------------------------------------------------------------
# Deprecated top-level aliases
# ---------------------------------------------------------------------------


def test_dep_impact_alias_forwards_every_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The alias runs the real command with the arguments it was given.

    Without this, deleting the ``ctx.invoke`` call in the alias leaves a
    command that prints a deprecation notice and does nothing, and the suite
    stays green.
    """
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(dep_impact_cmd, "callback", lambda **kwargs: recorded.update(kwargs))

    res = CliRunner().invoke(
        cli,
        ["dep-impact", "--base", "release-3.9", "--workdir", str(tmp_path), "--strict", "--json"],
    )

    assert res.exit_code == 0, res.output
    assert recorded == {
        "base": "release-3.9",
        "workdir": str(tmp_path),
        "strict": True,
        "output_json": True,
    }


def test_dep_impact_alias_warning_goes_to_stderr_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dep_impact_cmd, "callback", lambda **_kwargs: None)

    res = CliRunner().invoke(cli, ["dep-impact"])

    assert res.exit_code == 0, res.output
    assert _DEP_IMPACT_WARNING in res.stderr
    assert _DEP_IMPACT_WARNING not in res.stdout


def test_dep_impact_alias_keeps_json_stdout_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` through the alias must still emit a document, not a document with a banner on top."""
    monkeypatch.setattr(dep_impact_cmd, "callback", lambda **_kwargs: click.echo(json.dumps({"api_breaking": []})))

    res = CliRunner().invoke(cli, ["dep-impact", "--json"])

    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == {"api_breaking": []}


def test_blast_radius_alias_warns_on_stderr_and_still_reaches_the_subcommand() -> None:
    res = CliRunner().invoke(cli, ["blast-radius", "score", "--help"])

    assert res.exit_code == 0, res.output
    assert _BLAST_RADIUS_WARNING in res.stderr
    assert _BLAST_RADIUS_WARNING not in res.stdout
    assert "Score a change described on the command line" in res.stdout


def test_blast_radius_alias_without_a_subcommand_shows_help_not_a_bare_warning() -> None:
    res = CliRunner().invoke(cli, ["blast-radius"])

    assert _BLAST_RADIUS_WARNING not in res.output
    assert "score" in res.output
    assert "show" in res.output
