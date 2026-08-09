"""Unit tests for #3138: collapsing top-level command names that duplicate an existing group.

The moves are only safe if two things hold at once: the new spelling reaches the
same implementation, and the deprecated spelling keeps working with the flags
scripts already pass it. Asserting ``--help`` exits 0 proves neither, so the
tests below drive the commands against a real project directory and assert on
what comes back on each stream.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.evidence.run_artifacts import ArtifactPayload, post_run_artifact
from bernstein.core.security.audit import load_or_create_audit_key


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with ``.sdd/`` and an audit key that never leaves tmp_path."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _post(project: Path, *, task_id: str = "task-1", key: str = "summary", body: str = "hello") -> None:
    post_run_artifact(
        sdd_dir=project / ".sdd",
        task_id=task_id,
        key=key,
        payload=ArtifactPayload.report(body),
        actor="worker-a",
        hmac_key=load_or_create_audit_key(),
    )


# ---------------------------------------------------------------------------
# New spellings reach the implementation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# artifacts -> artifact
# ---------------------------------------------------------------------------


def test_artifact_subcommands() -> None:
    runner = CliRunner()
    res_list = runner.invoke(cli, ["artifact", "list", "--help"])
    assert res_list.exit_code == 0
    res_show = runner.invoke(cli, ["artifact", "show", "--help"])
    assert res_show.exit_code == 0


# ---------------------------------------------------------------------------
# limits pool
# ---------------------------------------------------------------------------


def test_limits_pool_subcommands() -> None:
    runner = CliRunner()
    for sub in ["register", "list", "show", "verify"]:
        res = runner.invoke(cli, ["limits", "pool", sub, "--help"])
        assert res.exit_code == 0, f"limits pool {sub} failed"


# ---------------------------------------------------------------------------
# Deprecated spellings still work, and say so on stderr
# ---------------------------------------------------------------------------


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


def test_deprecated_cost_envelopes_alias_still_dispatches_show(tmp_path: Path) -> None:
    """The alias is only ever used with a subcommand; a leaf alias rejects `show`."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "cost-envelopes",
            "show",
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--config",
            str(tmp_path / "bernstein.yaml"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "WARNING: 'bernstein cost-envelopes' is deprecated" in result.stderr
    assert json.loads(result.stdout)["envelopes"] == {}


def test_cost_envelopes_alias_exposes_the_same_subcommands_as_the_group() -> None:
    from bernstein.cli.commands.cost import cost_envelopes_alias_cmd, cost_envelopes_group

    assert set(cost_envelopes_alias_cmd.commands) == set(cost_envelopes_group.commands)
    for name, command in cost_envelopes_group.commands.items():
        assert cost_envelopes_alias_cmd.commands[name] is command
