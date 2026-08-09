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


#: Options carried only by the deprecated ``benchmark`` spelling of a name
#: ``eval`` also serves with a *different* command object, mapped to the
#: canonical option that replaces them.  ``None`` means there is no ``eval``
#: equivalent at all: that option stops working when the alias is unregistered
#: in v4.0.0, and the removal is not a pure rename for its users.
#:
#: The declaration is by parameter name; the flag spelling is read back off the
#: command object, because the two differ (``force_lite`` is spelled
#: ``--lite``) and hard-coding a guess produces a test that probes an option
#: neither command has and passes for the wrong reason.
_ALIAS_ONLY_OPTIONS: dict[str, dict[str, str | None]] = {
    # `benchmark run` drives bernstein.evolution.benchmark over a tier tree;
    # `eval run` drives the golden harness or a YAML spec. Different runners,
    # different tier vocabularies, no equivalent option.
    "run": {"benchmarks_dir": None},
    # Both swe-bench commands call the same _run_swe_bench_command; `--lite` is
    # itself documented as a deprecated alias for `--subset lite`, which `eval
    # swe-bench` accepts. Nothing is lost here.
    "swe-bench": {"force_lite": "--subset"},
}


def _flag_for(command: click.Command, param_name: str) -> str:
    """The CLI spelling of ``param_name`` on ``command``."""
    for param in command.params:
        if param.name == param_name:
            return param.opts[0]
    raise AssertionError(f"{param_name} is not a parameter of {command.name}")


def test_alias_only_options_are_a_declared_list() -> None:
    """Name-level overlap is not contract-level overlap.

    ``benchmark run`` and ``eval run`` share a verb, not a contract, and so do
    the two ``swe-bench`` commands.  Treating them as one command is what makes
    the v4.0.0 removal look free, so every divergence is declared here rather
    than discovered by an operator after the removal.
    """
    eval_group = cli.commands["eval"]
    observed: dict[str, set[str]] = {}
    for name, alias_cmd in _alias_group().commands.items():
        eval_cmd = eval_group.commands.get(name)  # type: ignore[attr-defined]
        if eval_cmd is None or eval_cmd is alias_cmd:
            continue
        only = {p.name for p in alias_cmd.params} - {p.name for p in eval_cmd.params}
        only.discard("help")
        if only:
            observed[name] = only
    declared = {name: set(params) for name, params in _ALIAS_ONLY_OPTIONS.items()}
    assert observed == declared, observed


def test_alias_only_options_do_not_resolve_under_eval() -> None:
    """The declared gap is real, not a stale note.

    The flag is read off the alias command, so this cannot pass by probing an
    option that exists on neither side.
    """
    runner = CliRunner()
    alias = _alias_group()
    for name, params in _ALIAS_ONLY_OPTIONS.items():
        for param in params:
            flag = _flag_for(alias.commands[name], param)
            res = runner.invoke(cli, ["eval", name, flag, "x"])
            assert res.exit_code == 2, f"eval {name} {flag} -> {res.output}"
            assert "No such option" in res.output, res.output


def test_declared_replacements_exist_on_eval() -> None:
    """Where a replacement is named, ``eval`` must actually accept it.

    ``--lite`` is a deprecated alias for ``--subset lite`` and ``eval
    swe-bench`` takes ``--subset``, so that entry is a rename, not a loss.  An
    entry mapped to ``None`` is a capability the v4.0.0 removal drops; the
    reference has to keep saying so for as long as that stays true.
    """
    eval_group = cli.commands["eval"]
    unmigrated = set()
    for name, params in _ALIAS_ONLY_OPTIONS.items():
        eval_cmd = eval_group.commands[name]  # type: ignore[attr-defined]
        accepted = {opt for p in eval_cmd.params for opt in p.opts}
        for param, replacement in params.items():
            if replacement is None:
                unmigrated.add(f"benchmark {name} {_flag_for(_alias_group().commands[name], param)}")
                continue
            assert replacement in accepted, f"eval {name} does not accept {replacement}"
    assert unmigrated == {"benchmark run --benchmarks-dir"}, unmigrated


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
