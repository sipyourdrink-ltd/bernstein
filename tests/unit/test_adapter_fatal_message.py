"""The no-adapter fatal must only name things the operator can actually run.

The message fires inside the orchestrator subprocess, which has its own
``argparse`` surface, so it is easy to write it against the flags in scope at
that point in the file. The reader is somewhere else entirely: they typed
``bernstein``, and every remedy they try will be checked against
``bernstein --help``. A flag named here that lives only on the subprocess sends
them looking for an option that does not exist (#3526, reported in #3514).

These tests resolve each flag and command the message names against the
registered CLI, so the text cannot drift back to naming an internal one.
"""

from __future__ import annotations

import re

import click
import pytest

from bernstein.cli.main import cli
from bernstein.core.orchestration.orchestrator import NO_ADAPTER_CONFIGURED

_FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]+")
#: A quoted invocation: the command path, then the flags that belong to it. The
#: flags are captured with the path so they are checked against the command they
#: were written for rather than against the top-level group.
_COMMAND_RE = re.compile(r"bernstein ((?:[a-z][a-z0-9-]* )*[a-z][a-z0-9-]*)((?: --[a-z][a-z0-9-]+)*)")


def _group_option_names(group: click.Group) -> set[str]:
    return {name for param in group.params if isinstance(param, click.Option) for name in param.opts}


def _resolve(path: str) -> click.Command | None:
    """Resolve a space-separated command path against the CLI, or return None."""
    current: click.Command = cli
    for token in path.split():
        if not isinstance(current, click.Group):
            return None
        found = current.commands.get(token)
        if found is None:
            return None
        current = found
    return current


def _invocations() -> list[tuple[str, tuple[str, ...]]]:
    """Return each ``bernstein ...`` invocation as (command path, its flags)."""
    return [(m.group(1), tuple(_FLAG_RE.findall(m.group(2)))) for m in _COMMAND_RE.finditer(NO_ADAPTER_CONFIGURED)]


def _flags_outside_invocations() -> set[str]:
    """Return flags the message offers on the ``bernstein`` command itself.

    Flags written as part of a quoted invocation belong to that subcommand and
    are checked against it, so whole invocations are removed here first.
    """
    return set(_FLAG_RE.findall(_COMMAND_RE.sub(" ", NO_ADAPTER_CONFIGURED)))


def _command_flag_cases() -> list[tuple[str, str]]:
    return [(path, flag) for path, flags in _invocations() for flag in flags]


@pytest.mark.parametrize("flag", sorted(_flags_outside_invocations()))
def test_every_flag_the_fatal_offers_is_a_flag_of_the_bernstein_command(flag: str) -> None:
    """The reader checks the remedy against ``bernstein --help``; it has to be there."""
    assert flag in _group_option_names(cli), (
        f"the no-adapter fatal tells the operator to pass {flag}, which the bernstein "
        f"command does not accept; name the user-facing option instead"
    )


@pytest.mark.parametrize("path", [path for path, _ in _invocations()])
def test_every_command_the_fatal_suggests_resolves(path: str) -> None:
    """A suggested command that does not resolve is a second dead end."""
    assert _resolve(path) is not None, f"the no-adapter fatal suggests 'bernstein {path}', which does not resolve"


@pytest.mark.parametrize(("path", "flag"), _command_flag_cases())
def test_every_flag_on_a_suggested_command_belongs_to_that_command(path: str, flag: str) -> None:
    """A flag quoted with a command has to be accepted by that command."""
    command = _resolve(path)
    assert command is not None, f"'bernstein {path}' does not resolve"
    accepted = {name for param in command.params if isinstance(param, click.Option) for name in param.opts}
    assert flag in accepted, f"the no-adapter fatal suggests 'bernstein {path} {flag}', which that command rejects"


def test_the_fatal_does_not_name_the_orchestrators_own_adapter_flag() -> None:
    """``--adapter`` is reachable only from the subprocess argparse, never from the CLI."""
    assert "--adapter" not in NO_ADAPTER_CONFIGURED
    assert "--adapter" not in _group_option_names(cli), (
        "the CLI grew an --adapter option; if it is the operator-facing spelling, "
        "this test and the fatal message should both move to it"
    )


def test_the_fatal_still_offers_the_two_remedies_that_are_not_flags() -> None:
    """The env var and the config key work and are the fallbacks when no flag fits."""
    assert "BERNSTEIN_ADAPTER" in NO_ADAPTER_CONFIGURED
    assert "bernstein.yaml" in NO_ADAPTER_CONFIGURED


def _run_bootstrap(workdir, monkeypatch, *, cli="auto", adapter_env=None):
    """Call the goal bootstrap with every process-spawning step replaced by a mock."""
    from unittest.mock import patch

    from bernstein.core.orchestration import bootstrap

    if adapter_env:
        monkeypatch.setenv("BERNSTEIN_ADAPTER", adapter_env)
    else:
        monkeypatch.delenv("BERNSTEIN_ADAPTER", raising=False)
    monkeypatch.delenv("BERNSTEIN_SEED_PATH", raising=False)
    with patch.object(bootstrap, "_acquire_pid_lock", side_effect=_Reached):
        bootstrap.bootstrap_from_goal(goal="anything", workdir=workdir, cli=cli)


class _Reached(Exception):
    """Raised by the patched pid lock: the guard let the run proceed."""


def test_fresh_directory_without_adapter_refuses_at_startup(tmp_path, monkeypatch) -> None:
    """No bernstein.yaml, no .sdd, no flag, no env: exit 1 before anything starts (#6126)."""
    with pytest.raises(SystemExit) as excinfo:
        _run_bootstrap(tmp_path, monkeypatch)

    assert excinfo.value.code == 1
    assert not (tmp_path / ".sdd").exists()
    assert not (tmp_path / "bernstein.yaml").exists()


def test_existing_sdd_without_adapter_refuses_at_startup(tmp_path, monkeypatch) -> None:
    (tmp_path / ".sdd").mkdir()
    with pytest.raises(SystemExit) as excinfo:
        _run_bootstrap(tmp_path, monkeypatch)
    assert excinfo.value.code == 1


def test_cli_flag_lets_the_run_proceed(tmp_path, monkeypatch) -> None:
    with pytest.raises(_Reached):
        _run_bootstrap(tmp_path, monkeypatch, cli="codex")


def test_env_adapter_lets_the_run_proceed(tmp_path, monkeypatch) -> None:
    with pytest.raises(_Reached):
        _run_bootstrap(tmp_path, monkeypatch, adapter_env="codex")


def test_seed_file_lets_the_run_proceed(tmp_path, monkeypatch) -> None:
    (tmp_path / "bernstein.yaml").write_text("cli: auto\n")
    with pytest.raises(_Reached):
        _run_bootstrap(tmp_path, monkeypatch)
