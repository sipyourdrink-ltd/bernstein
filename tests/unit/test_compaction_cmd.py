"""CLI tests for ``bernstein compaction log`` (issue #2246).

The command prints the receipt chain for a task straight from the
HMAC-chained audit log, and ``--verify`` re-runs the receipt
verification (chain integrity + journal/receipt cross-check) so an
operator can prove a task's compactions from the terminal.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.commands.compaction_cmd import compaction_group
from bernstein.core.persistence.journal import Journal, agent_journal_dir
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.tokens.compaction_receipt import (
    build_receipt,
    record_compaction_journal_step,
    record_compaction_receipt,
)
from bernstein.core.tokens.compaction_validate import run_validators

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

    from bernstein.core.tokens.compaction_receipt import CompactionReceipt

_PRE = "long original context with plenty of detail to compact away\n"
_POST = "short summary\n"


def _receipt(task_id: str, worker_id: str, correlation_id: str) -> CompactionReceipt:
    return build_receipt(
        task_id=task_id,
        worker_id=worker_id,
        trigger="proactive",
        pre_text=_PRE,
        post_text=_POST,
        tokens_before=400,
        tokens_after=120,
        verdicts=run_validators(_PRE, _POST),
        retry_count=0,
        correlation_id=correlation_id,
        ts=1_700_000_000.0,
    )


def _seed_chain(tmp_path: Path) -> AuditChainStore:
    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    record_compaction_receipt(chain=chain, receipt=_receipt("T-1", "sess-1", "compact-aaaa"))
    record_compaction_receipt(chain=chain, receipt=_receipt("T-1", "sess-1", "compact-bbbb"))
    record_compaction_receipt(chain=chain, receipt=_receipt("T-2", "sess-2", "compact-cccc"))
    return chain


def _invoke(tmp_path: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(
        compaction_group,
        ["log", "--audit-dir", str(tmp_path / ".sdd" / "audit"), "--sdd-dir", str(tmp_path / ".sdd"), *args],
        catch_exceptions=False,
    )


class TestCompactionLog:
    def test_prints_receipts_for_task_only(self, tmp_path: Path) -> None:
        _seed_chain(tmp_path)
        result = _invoke(tmp_path, "--task", "T-1")
        assert result.exit_code == 0
        assert "compact-aaaa" in result.output
        assert "compact-bbbb" in result.output
        assert "compact-cccc" not in result.output

    def test_json_output_is_structured(self, tmp_path: Path) -> None:
        _seed_chain(tmp_path)
        result = _invoke(tmp_path, "--task", "T-2", "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["task_id"] == "T-2"
        assert len(data["receipts"]) == 1
        receipt = data["receipts"][0]
        assert receipt["correlation_id"] == "compact-cccc"
        assert receipt["trigger"] == "proactive"
        assert receipt["tokens_before"] == 400
        assert receipt["tokens_after"] == 120
        assert len(receipt["pre_sha256"]) == 64
        assert all(v["result"] == "pass" for v in receipt["validators"])

    def test_missing_audit_dir_is_friendly(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "--task", "T-1")
        assert result.exit_code == 0
        assert "No audit chain" in result.output

    def test_no_receipts_for_task(self, tmp_path: Path) -> None:
        _seed_chain(tmp_path)
        result = _invoke(tmp_path, "--task", "T-404")
        assert result.exit_code == 0
        assert "No compaction receipts" in result.output


class TestCompactionLogVerify:
    def test_verify_passes_when_journal_matches_chain(self, tmp_path: Path) -> None:
        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        receipt = _receipt("T-1", "sess-1", "compact-aaaa")
        record_compaction_receipt(chain=chain, receipt=receipt)
        journal = Journal.open(agent_journal_dir(tmp_path / ".sdd", "sess-1"))
        record_compaction_journal_step(journal, receipt)

        result = _invoke(tmp_path, "--task", "T-1", "--verify")
        assert result.exit_code == 0
        assert "OK" in result.output

    def test_verify_fails_for_journaled_compaction_without_receipt(self, tmp_path: Path) -> None:
        # AC #2 surfaced at the CLI: a compaction step in the replay
        # journal with no chain receipt fails verification.
        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        chain.log(
            event_type="unrelated.event",
            actor="x",
            resource_type="x",
            resource_id="x",
            details={},
        )
        orphan = _receipt("T-1", "sess-1", "compact-orphan")
        journal = Journal.open(agent_journal_dir(tmp_path / ".sdd", "sess-1"))
        record_compaction_journal_step(journal, orphan)

        result = _invoke(tmp_path, "--task", "T-1", "--verify")
        assert result.exit_code == 1
        assert "no chain receipt" in result.output
