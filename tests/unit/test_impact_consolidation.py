"""Unit tests for #3139: Consolidating impact analysis under bernstein impact."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_impact_group_subcommands_registered() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["impact", "--help"])
    assert res.exit_code == 0
    assert "api" in res.output
    assert "deps" in res.output
    assert "blast" in res.output


def test_impact_api_reachable() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["impact", "api", "--help"])
    assert res.exit_code == 0
    assert "Detect breaking API changes" in res.output


def test_impact_deps_reachable() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["impact", "deps", "--help"])
    assert res.exit_code == 0
    assert "Analyse which call sites break" in res.output


def test_impact_blast_reachable() -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["impact", "blast", "score", "--help"])
    assert res.exit_code == 0
    assert "Score a change described on the command line" in res.output


def test_deprecated_top_level_aliases_warning() -> None:
    runner = CliRunner()
    res_dep = runner.invoke(cli, ["dep-impact", "--help"])
    assert res_dep.exit_code == 0

    res_dep_run = runner.invoke(cli, ["dep-impact"])
    assert (
        "WARNING: 'bernstein dep-impact' is deprecated" in res_dep_run.output
        or "WARNING: 'bernstein dep-impact' is deprecated" in res_dep_run.stderr
    )

    res_blast_run = runner.invoke(cli, ["blast-radius", "score", "--help"])
    assert (
        "WARNING: 'bernstein blast-radius' is deprecated" in res_blast_run.output
        or "WARNING: 'bernstein blast-radius' is deprecated" in res_blast_run.stderr
    )
