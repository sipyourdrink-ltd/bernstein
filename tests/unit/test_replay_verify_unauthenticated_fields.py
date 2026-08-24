"""Tests for CLI --verify surfacing unauthenticated_fields in JSON and text output."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.advanced_cmd import replay_cmd
from click.testing import CliRunner

from bernstein.core.replay.journal import EventJournal, seal_journal_into_spine

_SEAL_KEY = b"k" * 32


def _make_journal(sdd_dir: Path, run_id: str) -> EventJournal:
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id)
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1")
    journal.record("run_completed", run_id=run_id)
    return journal


def _sealed_journal(sdd_dir: Path, run_id: str) -> EventJournal:
    """Build a journal, finalize it, and seal its head into the lineage spine."""
    import os

    key_path = Path(os.environ["BERNSTEIN_AUDIT_KEY_PATH"])
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(_SEAL_KEY)
    key_path.chmod(0o600)

    journal = _make_journal(sdd_dir, run_id)
    seal_journal_into_spine(
        journal,
        lineage_root=sdd_dir / "lineage",
        hmac_key=_SEAL_KEY,
        actor="orchestrator",
    )
    return journal


def _expected_unauthenticated_fields() -> list[str]:
    return ["elapsed_s", "event_hash", "index", "payload_hash", "prev_hash", "ts"]


class TestCLIVerifyUnauthenticatedFields:
    def test_verify_intact_chain_as_json_includes_unauthenticated_fields(self, tmp_path: Path) -> None:
        """--verify --as-json on intact chain includes unauthenticated_fields."""
        sdd_dir = tmp_path / ".sdd"
        _sealed_journal(sdd_dir, "run-sealed-1")

        result = CliRunner().invoke(
            replay_cmd,
            ["run-sealed-1", "--sdd-dir", str(sdd_dir), "--verify", "--as-json"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "unauthenticated_fields" in payload
        assert payload["unauthenticated_fields"] == _expected_unauthenticated_fields()

    def test_verify_intact_chain_text_output_prints_unauthenticated_fields(self, tmp_path: Path) -> None:
        """--verify text output on intact chain prints unauthenticated fields message."""
        sdd_dir = tmp_path / ".sdd"
        _sealed_journal(sdd_dir, "run-sealed-2")

        result = CliRunner().invoke(
            replay_cmd,
            ["run-sealed-2", "--sdd-dir", str(sdd_dir), "--verify"],
        )

        assert result.exit_code == 0
        assert "unauthenticated fields:" in result.output.lower()
        # Check at least one field name appears
        assert any(field in result.output for field in _expected_unauthenticated_fields())

    def test_verify_chain_divergence_as_json_includes_unauthenticated_fields(self, tmp_path: Path) -> None:
        """--verify --as-json on chain divergence (broken prev_hash) includes unauthenticated_fields."""
        sdd_dir = tmp_path / ".sdd"
        journal = _make_journal(sdd_dir, "run-5")

        # Tamper: modify a row's task_id to break the chain
        import json

        lines = journal.path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[2])
        row["task_id"] = "T-INJECTED"
        lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = CliRunner().invoke(
            replay_cmd,
            ["run-5", "--sdd-dir", str(sdd_dir), "--verify", "--as-json"],
        )

        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "unauthenticated_fields" in payload
        assert payload["unauthenticated_fields"] == _expected_unauthenticated_fields()

    def test_verify_chain_divergence_text_output_prints_unauthenticated_fields(self, tmp_path: Path) -> None:
        """--verify text output on chain divergence prints unauthenticated fields message."""
        sdd_dir = tmp_path / ".sdd"
        journal = _make_journal(sdd_dir, "run-6")

        # Tamper: modify a row's task_id to break the chain
        import json

        lines = journal.path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[2])
        row["task_id"] = "T-INJECTED"
        lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = CliRunner().invoke(
            replay_cmd,
            ["run-6", "--sdd-dir", str(sdd_dir), "--verify"],
        )

        assert result.exit_code != 0
        assert "unauthenticated fields:" in result.output.lower()
        assert any(field in result.output for field in _expected_unauthenticated_fields())
