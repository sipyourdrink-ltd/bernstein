"""CLI tests for ``bernstein context show|verify|manifest`` (#2545, #3366).

``verify`` recomputes the capsule offline from the on-disk bytes and checks its
hash against the ``context.capsule`` audit-chain entry and the
``context.capsule_recorded`` journal event. A real capsule verifies; a
mock-layer fixture fails with a mock diagnostic.

``manifest`` derives the content-addressed context manifest from the task's
declared path set, so an operator can see what the agent would be shown --
including the entries the deriver could not resolve.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.context_cmd import context_group
from bernstein.core.agents.context_capsule import (
    build_context_capsule,
    seal_and_bind,
    seal_mock_capsule,
    write_capsule_record,
)
from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import AuditChainStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seal_real(project: Path) -> str:
    from bernstein.core.security.audit import load_or_create_audit_key

    sdd = project / ".sdd"
    chain = AuditChainStore(sdd / "audit", key=load_or_create_audit_key())
    journal = EventJournal("run-cli", sdd)
    priv, pub = generate_keypair()
    capsule = build_context_capsule(
        task_id="task-cli",
        run_id="run-cli",
        params_hash="sha256:" + "a" * 64,
        role="backend",
        budget_remaining_tokens=1000,
    )
    seal_and_bind(chain=chain, sdd_dir=sdd, journal=journal, capsule=capsule, private_key_pem=priv, public_key_pem=pub)
    return capsule.capsule_hash()


def test_context_show(project: Path) -> None:
    _seal_real(project)
    runner = CliRunner()
    result = runner.invoke(context_group, ["show", "task-cli", "--workdir", str(project)])
    assert result.exit_code == 0, result.output
    assert "Context capsule" in result.output
    assert "backend" in result.output


def test_context_show_json(project: Path) -> None:
    quoted = _seal_real(project)
    runner = CliRunner()
    result = runner.invoke(context_group, ["show", "task-cli", "--workdir", str(project), "--json"])
    assert result.exit_code == 0, result.output
    assert quoted in result.output


def test_context_verify_ok(project: Path) -> None:
    _seal_real(project)
    runner = CliRunner()
    result = runner.invoke(context_group, ["verify", "task-cli", "--workdir", str(project)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_context_verify_missing(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(context_group, ["verify", "nope", "--workdir", str(project)])
    assert result.exit_code == 1
    assert "NO CAPSULE" in result.output


def test_context_verify_mock_fails(project: Path) -> None:
    sdd = project / ".sdd"
    priv, pub = generate_keypair()
    capsule = build_context_capsule(task_id="task-mock", run_id="run-cli", role="backend")
    write_capsule_record(sdd, seal_mock_capsule(capsule, priv, pub))
    runner = CliRunner()
    result = runner.invoke(context_group, ["verify", "task-mock", "--workdir", str(project)])
    assert result.exit_code == 2
    assert "MOCK" in result.output


def _write_task(project: Path, task_id: str, owned_files: list[str]) -> None:
    """Persist a task with *owned_files* into the store the CLI replays."""
    from bernstein.core.tasks.models import Task

    task = Task(
        id=task_id,
        title="manifest fixture",
        description="declares a path set",
        role="backend",
        owned_files=owned_files,
    )
    tasks_path = project / ".sdd" / "runtime" / "tasks.jsonl"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    with tasks_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(task.to_dict()) + "\n")


def test_context_manifest_addresses_declared_files_and_names_unmanifested_entries(project: Path) -> None:
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "a.py").write_text("alpha\n", encoding="utf-8")
    _write_task(project, "task-manifest", ["src/a.py", "src/gone.py"])

    runner = CliRunner()
    result = runner.invoke(context_group, ["manifest", "task-manifest", "--workdir", str(project)])

    assert result.exit_code == 0, result.output
    assert "Context manifest" in result.output
    assert "sha256:" in result.output
    assert "unmanifested" in result.output
    assert "missing" in result.output
    assert "src/gone.py" in result.output


def test_context_manifest_json_carries_the_digest_and_every_entry(project: Path) -> None:
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "a.py").write_text("alpha\n", encoding="utf-8")
    _write_task(project, "task-manifest-json", ["src/a.py", "src/gone.py"])

    runner = CliRunner()
    result = runner.invoke(
        context_group,
        ["manifest", "task-manifest-json", "--workdir", str(project), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["manifest_digest"].startswith("sha256:")
    assert payload["entry_count"] == 2
    assert payload["unmanifested_count"] == 1
    assert [entry["path"] for entry in payload["entries"]] == ["src/a.py", "src/gone.py"]
    assert payload["entries"][1]["reason"] == "missing"


def test_context_manifest_exits_nonzero_for_an_unknown_task(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(context_group, ["manifest", "no-such-task", "--workdir", str(project)])

    assert result.exit_code == 1
    assert "NO TASK" in result.output
