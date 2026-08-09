"""Unit tests for #3138: Collapsing top-level command names that duplicate an existing group."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_cost_estimate_subcommand_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["cost", "estimate", "--help"])
    assert result.exit_code == 0
    assert "Predict the cost of a task" in result.output


def test_cost_envelopes_subcommand_registered_and_no_issue_tag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["cost", "envelopes", "--help"])
    assert result.exit_code == 0
    assert "(issue #1405)" not in result.output


def test_skills_provenance_and_verify_registered() -> None:
    runner = CliRunner()
    res1 = runner.invoke(cli, ["skills", "provenance", "--help"])
    assert res1.exit_code == 0
    assert "usage-provenance graph" in res1.output

    res2 = runner.invoke(cli, ["skills", "verify", "--help"])
    assert res2.exit_code == 0
    assert "install receipt" in res2.output


def test_artifact_subcommands() -> None:
    runner = CliRunner()
    res_list = runner.invoke(cli, ["artifact", "list", "--help"])
    assert res_list.exit_code == 0
    res_show = runner.invoke(cli, ["artifact", "show", "--help"])
    assert res_show.exit_code == 0


def test_limits_pool_subcommands() -> None:
    runner = CliRunner()
    for sub in ["register", "list", "show", "verify"]:
        res = runner.invoke(cli, ["limits", "pool", sub, "--help"])
        assert res.exit_code == 0, f"limits pool {sub} failed"


def test_deprecated_top_level_aliases_emit_warning() -> None:
    runner = CliRunner()
    res_est = runner.invoke(cli, ["estimate", "test goal", "--metrics-dir", "nonexistent"])
    assert (
        "WARNING: 'bernstein estimate' is deprecated" in res_est.output
        or "WARNING: 'bernstein estimate' is deprecated" in res_est.stderr
    )

    res_art = runner.invoke(cli, ["artifacts", "list", "--help"])
    assert (
        "WARNING: 'bernstein artifacts' is deprecated" in res_art.output
        or "WARNING: 'bernstein artifacts' is deprecated" in res_art.stderr
    )

    res_skill = runner.invoke(cli, ["skill", "provenance", "--help"])
    assert (
        "WARNING: 'bernstein skill' is deprecated" in res_skill.output
        or "WARNING: 'bernstein skill' is deprecated" in res_skill.stderr
    )

    res_pool = runner.invoke(cli, ["pool", "list", "--help"])
    assert (
        "WARNING: 'bernstein pool' is deprecated" in res_pool.output
        or "WARNING: 'bernstein pool' is deprecated" in res_pool.stderr
    )
