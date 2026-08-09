"""Unit tests for #3143: Folding overlapping benchmark subcommands into eval.

The change has three promises, and each one is pinned here:

* ``programbench`` and ``compare`` are reachable under ``bernstein eval``,
  and their help steers the reader at that path rather than at the
  deprecated one.
* ``bernstein benchmark`` keeps working for the whole of 3.x -- *every*
  subcommand it carried, not just the one a spot check happens to name --
  and announces its deprecation on stderr so piped stdout stays parseable.
* ``bench`` and ``simulate`` are deliberately untouched by the fold.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from bernstein.cli.commands.eval_benchmark_cmd import benchmark_group
from bernstein.cli.main import cli

_DEPRECATION = "WARNING: 'bernstein benchmark' is deprecated"


def _alias_group() -> click.Group:
    """The object registered on the top-level CLI under ``benchmark``."""
    registered = cli.commands["benchmark"]
    assert isinstance(registered, click.Group), type(registered).__name__
    return registered


def test_eval_programbench_registered() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["eval", "programbench", "--help"])
    assert res.exit_code == 0
    assert "--subset" in res.output


def test_eval_compare_registered() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["eval", "compare", "--help"])
    assert res.exit_code == 0
    assert "Run comparative benchmark" in res.output


def test_folded_commands_are_the_originals() -> None:
    """``eval`` must expose the real implementations, not re-declared stubs.

    A second copy of the command would drift from the one ``benchmark``
    still serves, which is the capability loss the port exists to avoid.
    """
    for name in ("programbench", "compare"):
        eval_cmd = cli.commands["eval"].get_command(click.Context(cli), name)  # type: ignore[attr-defined]
        assert eval_cmd is benchmark_group.commands[name], name


def test_folded_command_help_points_at_the_eval_path() -> None:
    """The ported commands document ``bernstein eval ...``, not the alias.

    ``bernstein eval programbench --help`` renders the same docstring the
    deprecated path renders.  Leaving the old examples in place tells every
    reader of the canonical surface to type the deprecated one.
    """
    runner = CliRunner()
    for name in ("programbench", "compare", "simulate"):
        res = runner.invoke(cli, ["eval", name, "--help"])
        assert res.exit_code == 0, res.output
        assert f"bernstein eval {name}" in res.output, res.output
        assert f"bernstein benchmark {name}" not in res.output, res.output
    for sub in ("emit", "verify"):
        res = runner.invoke(cli, ["eval", "receipt", sub, "--help"])
        assert res.exit_code == 0, res.output
        assert f"bernstein eval receipt {sub}" in res.output, res.output
        assert f"bernstein benchmark receipt {sub}" not in res.output, res.output


def test_every_alias_subcommand_has_an_eval_destination() -> None:
    """The alias tells the operator to use ``eval``; ``eval`` must be able to serve them.

    The alias group is unregistered in v4.0.0.  A subcommand carried only by
    the alias is therefore a capability scheduled for deletion, not a spelling
    scheduled for retirement, and the group's own one-line help sends its user
    at ``bernstein eval``, where it does not exist.
    """
    eval_group = cli.commands["eval"]
    missing = sorted(name for name in _alias_group().commands if name not in eval_group.commands)  # type: ignore[attr-defined]
    assert not missing, f"carried only by the deprecated alias: {missing}"


def test_eval_simulate_is_not_the_top_level_simulate() -> None:
    """The two ``simulate`` commands are different features sharing a verb.

    Treating them as duplicates is what makes dropping the benchmark one look
    free.  ``bernstein simulate`` replays a *plan* against historical traces
    (#1374); ``bernstein eval simulate`` replays the standard *benchmark task
    set*.  They have no options in common beyond ``--seed``.
    """
    eval_simulate = cli.commands["eval"].get_command(click.Context(cli), "simulate")  # type: ignore[attr-defined]
    top_level = cli.commands["simulate"]
    assert eval_simulate is not top_level
    shared = {p.name for p in eval_simulate.params} & {p.name for p in top_level.params}
    assert shared == {"seed"}, shared


def test_eval_carries_the_folded_run_and_swe_bench() -> None:
    """The fold targets named in the issue resolve under ``eval``."""
    runner = CliRunner()
    for name in ("run", "swe-bench"):
        res = runner.invoke(cli, ["eval", name, "--help"])
        assert res.exit_code == 0, f"`bernstein eval {name} --help` -> {res.output}"


def test_deprecated_benchmark_alias_warns_on_stderr() -> None:
    """The alias warns on stderr, so stdout stays machine-readable.

    Asserting only "the warning appeared somewhere in the output" passes
    just as happily when the warning is printed to stdout, which corrupts
    every pipeline reading a benchmark report.
    """
    runner = CliRunner()
    res = runner.invoke(cli, ["benchmark", "run", "--help"])
    assert res.exit_code == 0, res.output
    assert _DEPRECATION in res.stderr, res.stderr
    assert _DEPRECATION not in res.stdout, res.stdout


def test_benchmark_alias_exposes_every_original_subcommand() -> None:
    """Nothing the old group carried may disappear behind the alias.

    Spot-checking one subcommand cannot see the difference between an alias
    that forwards the whole group and one that forwards a single command.
    """
    assert set(_alias_group().commands) == set(benchmark_group.commands)


def test_benchmark_alias_subcommands_resolve_through_the_cli() -> None:
    """Every aliased subcommand is reachable from the top-level CLI."""
    runner = CliRunner()
    for name in sorted(benchmark_group.commands):
        res = runner.invoke(cli, ["benchmark", name, "--help"])
        assert res.exit_code == 0, f"`bernstein benchmark {name} --help` -> {res.output}"


def test_bare_benchmark_group_does_not_warn() -> None:
    """``bernstein benchmark`` alone prints help; the warning gates on a subcommand."""
    runner = CliRunner()
    res = runner.invoke(cli, ["benchmark"])
    assert _DEPRECATION not in res.stderr, res.stderr


def test_bench_still_works() -> None:
    """``bench`` is a separate command and is out of scope for the fold."""
    runner = CliRunner()
    res = runner.invoke(cli, ["bench", "--help"])
    assert res.exit_code == 0


def test_top_level_simulate_still_works() -> None:
    """``benchmark simulate`` folds into the existing top-level ``simulate``."""
    runner = CliRunner()
    res = runner.invoke(cli, ["simulate", "--help"])
    assert res.exit_code == 0


def test_main_still_reexports_benchmark_group() -> None:
    """``bernstein.cli.main`` is a back-compat re-export surface.

    Registering the alias must add a name there, not replace one.
    """
    from bernstein.cli import main as main_module

    assert main_module.benchmark_group is benchmark_group
    assert "benchmark_group" in main_module.__all__
    assert "benchmark_alias_group" in main_module.__all__
