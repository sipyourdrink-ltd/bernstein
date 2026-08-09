"""Unit tests for #3143: Folding overlapping benchmark subcommands into eval."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.main import cli


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


def test_deprecated_benchmark_alias_warning() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["benchmark", "run", "--help"])
    assert (
        "WARNING: 'bernstein benchmark' is deprecated" in res.output
        or "WARNING: 'bernstein benchmark' is deprecated" in res.stderr
    )


def test_bench_still_works() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["bench", "--help"])
    assert res.exit_code == 0


def test_top_level_simulate_still_works() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["simulate", "--help"])
    assert res.exit_code == 0
