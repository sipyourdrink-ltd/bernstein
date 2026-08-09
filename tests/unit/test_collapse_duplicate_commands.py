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


def test_artifact_list_takes_an_optional_task_argument() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "--help"])
    assert result.exit_code == 0
    assert "[TASK]" in result.output


def test_artifact_list_with_task_lists_posted_artifacts(project: Path) -> None:
    """`artifact list <task>` must reach the agent-posted listing, not the spine listing."""
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "summary" in result.output


def test_artifact_list_without_task_returns_the_spine_document(project: Path) -> None:
    """The two paths answer different questions and must not return the same document."""
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "-w", str(project), "--output-json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Lineage-spine shape: production counts keyed by canonical artifact URI.
    assert sorted(payload) == ["artifacts"]
    assert all(sorted(row) == ["productions", "uri"] for row in payload["artifacts"])


def test_artifact_list_with_task_honours_output_json(project: Path) -> None:
    """`--output-json` must survive the delegation; a table here breaks `| jq`."""
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project), "--output-json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["task"] == "task-1"
    assert payload["verified"] is True
    assert payload["reason"] is None
    assert [a["key"] for a in payload["artifacts"]] == ["summary"]
    assert payload["artifacts"][0]["verified"] is True


def test_artifact_list_with_task_json_reports_the_empty_state(project: Path) -> None:
    """Exit code 1 keeps its meaning under --output-json, and stdout stays parseable."""
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "nope", "-w", str(project), "--output-json"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == {"artifacts": [], "reason": None, "task": "nope", "verified": True}


def test_artifact_list_with_task_json_marks_a_flipped_blob_unverified(project: Path) -> None:
    """The JSON path must carry the same tampered verdict the table column shows."""
    from bernstein.core.evidence.bundle import EvidenceStore
    from bernstein.core.evidence.run_artifacts import read_artifact_rows

    _post(project, body="secret-content")
    record = read_artifact_rows(project / ".sdd", "task-1")[0]
    blob_path = EvidenceStore(project / ".sdd" / "evidence").blob_path(record.content_hash)
    data = bytearray(blob_path.read_bytes())
    data[-2] ^= 0x01
    blob_path.write_bytes(bytes(data))

    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project), "--output-json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["verified"] is False
    assert payload["reason"]
    assert payload["artifacts"][0]["verified"] is False
    assert "secret-content" not in result.stdout


def test_artifact_list_with_task_json_reports_a_journal_that_hides_every_row(project: Path) -> None:
    """Tampering that removes every posted row is exit 2, not an empty clean listing."""
    _post(project)
    journals = sorted((project / ".sdd").rglob("*.jsonl"))
    journal = next(j for j in journals if "artifact_posted" in j.read_text(encoding="utf-8"))
    journal.write_text(journal.read_text(encoding="utf-8").replace("artifact_posted", "artifact_hidden"))

    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "list", "task-1", "-w", str(project), "--output-json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["artifacts"] == []
    assert payload["verified"] is False
    assert "Merkle" in payload["reason"]


def test_artifact_show_renders_a_posted_key(project: Path) -> None:
    _post(project, body="rendered-body")
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "show", "task-1", "summary", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "rendered-body" in result.output


def test_artifact_show_exits_1_for_an_unknown_key(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["artifact", "show", "task-1", "nope", "-w", str(project)])
    assert result.exit_code == 1, result.output
    assert "No artifact" in result.output


# ---------------------------------------------------------------------------
# limits pool
# ---------------------------------------------------------------------------


def test_limits_pool_subcommands_all_project_the_admission_ledger() -> None:
    """Every subcommand under `limits pool` must address the store `create` writes.

    `bernstein pool`'s subcommands project the HMAC audit chain into a sandbox-pool
    registry; `limits pool create` writes slot pools to the admission work ledger.
    Registering the first set under the second group yields a group in which
    `limits pool create staging-env --slots 1` is followed by `limits pool list`
    reporting no pools and `limits pool show staging-env` exiting 1.
    """
    from bernstein.cli.commands import limits_cmd
    from bernstein.cli.commands.limits_cmd import pool_group

    for name, command in pool_group.commands.items():
        callback = command.callback
        assert callback is not None, f"'limits pool {name}' has no callback"
        assert callback.__module__ == limits_cmd.__name__, (
            f"'limits pool {name}' is implemented in {callback.__module__}, which projects a "
            "different store than 'limits pool create'"
        )


def test_limits_pool_create_is_readable_back_through_limits_status(project: Path) -> None:
    """The admission-ledger round trip the group is supposed to own."""
    runner = CliRunner()
    created = runner.invoke(cli, ["limits", "pool", "create", "staging-env", "--slots", "1", "--workdir", str(project)])
    assert created.exit_code == 0, created.output
    listed = runner.invoke(cli, ["limits", "status", "--workdir", str(project)])
    assert listed.exit_code == 0, listed.output
    assert "staging-env" in listed.output


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

    res_skill = runner.invoke(cli, ["skill", "provenance", "--help"])
    assert (
        "WARNING: 'bernstein skill' is deprecated" in res_skill.output
        or "WARNING: 'bernstein skill' is deprecated" in res_skill.stderr
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


def test_deprecated_artifacts_alias_warns_and_still_lists(project: Path) -> None:
    _post(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["artifacts", "list", "task-1", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "WARNING: 'bernstein artifacts' is deprecated" in result.stderr
    assert "summary" in result.stdout
