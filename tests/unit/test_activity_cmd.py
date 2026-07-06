"""CLI tests for ``bernstein activity verify`` (#2311).

``verify`` walks a run's canonical event journal, recomputes each anchored
activity's ``evidence_set_hash`` from its pinned observation hashes, and -- when
a content store is available -- reattaches the evidence bytes and re-verifies
every content hash. A tampered journal entry or a divergent stored blob fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.activity_cmd import activity_group
from bernstein.core.orchestration.activity import dispatch_activity
from bernstein.core.orchestration.activity_modalities import ContentStore, ResearchActivity
from bernstein.core.replay.journal import EventJournal


def _seed_run(project: Path, run_id: str = "run-cli-1") -> str:
    """Seed a research activity anchored into ``.sdd/runs/<run_id>/journal.jsonl``."""
    store = ContentStore(project / ".sdd" / "cas")
    research = ResearchActivity(store=store)
    research.fetch("https://example.com/a", b"<html>alpha</html>")
    research.fetch("https://example.com/b", b"<html>beta</html>")
    result = research.finish(artifact={"summary": "found 2 sources"})
    journal = EventJournal(run_id=run_id, sdd_dir=project / ".sdd")
    dispatch_activity(result, stage_id="research-0", journal=journal)
    return run_id


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_verify_passes_on_intact_journal(project: Path) -> None:
    run_id = _seed_run(project)
    result = CliRunner().invoke(
        activity_group,
        ["verify", run_id, "--workdir", str(project)],
    )
    assert result.exit_code == 0, result.output
    assert "research-0" in result.output
    assert "verified" in result.output.lower()


def test_verify_json_output(project: Path) -> None:
    run_id = _seed_run(project)
    result = CliRunner().invoke(
        activity_group,
        ["verify", run_id, "--workdir", str(project), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run"] == run_id
    assert payload["ok"] is True
    assert payload["stages"][0]["stage_id"] == "research-0"
    assert payload["stages"][0]["kind"] == "research"
    assert payload["stages"][0]["evidence_reattached"] is True


def test_verify_fails_on_tampered_evidence_hash(project: Path) -> None:
    run_id = _seed_run(project)
    journal_path = project / ".sdd" / "runs" / run_id / "journal.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    # Tamper the anchored evidence_set_hash so it no longer matches the pinned
    # observation hashes.
    rows[0]["evidence_set_hash"] = "sha256:tampered"
    journal_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    result = CliRunner().invoke(
        activity_group,
        ["verify", run_id, "--workdir", str(project)],
    )
    assert result.exit_code == 2, result.output
    assert "mismatch" in result.output.lower() or "diverge" in result.output.lower()


def test_verify_fails_on_tampered_stored_bytes(project: Path) -> None:
    run_id = _seed_run(project)
    # Reach into the content store and corrupt a blob without changing its key.
    store = ContentStore(project / ".sdd" / "cas")
    journal_path = project / ".sdd" / "runs" / run_id / "journal.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    first_hash = rows[0]["observations"][0]["content_hash"]
    store.force_put(first_hash, b"<html>TAMPERED</html>")

    result = CliRunner().invoke(
        activity_group,
        ["verify", run_id, "--workdir", str(project)],
    )
    assert result.exit_code == 2, result.output


def test_verify_missing_run_exits_nonzero(project: Path) -> None:
    result = CliRunner().invoke(
        activity_group,
        ["verify", "no-such-run", "--workdir", str(project)],
    )
    assert result.exit_code == 1, result.output
    assert "no" in result.output.lower()
