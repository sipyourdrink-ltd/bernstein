"""verify() must not report a clean pass over a seal-only chain (#2789).

On the CLI-adapter path the only spine write is the journal-head seal, whose
recorded artifact is the run's own journal file, not any produced deliverable.
A chain that contains only that internal self-reference has no artifact
provenance, so ``verify`` must return a distinct non-OK status rather than the
``OK -- chain intact`` an auditor reads as "provenance confirmed".
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine, SpineStatus
from bernstein.core.replay.journal import EventJournal, seal_journal_into_spine

_KEY = b"k" * 32


def _seal_only_spine(tmp_path: Path) -> LineageSpine:
    sdd_dir = tmp_path / ".sdd"
    lineage_root = sdd_dir / "lineage"
    journal = EventJournal(run_id="run-seal-only", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-seal-only")
    journal.record("run_completed", run_id="run-seal-only")
    seal_journal_into_spine(journal, lineage_root=lineage_root, hmac_key=_KEY, actor="orchestrator")
    return LineageSpine(lineage_root, run_id="run-seal-only", hmac_key=_KEY)


def test_seal_only_chain_is_not_ok(tmp_path: Path, monkeypatch) -> None:
    """A chain whose only entry is the journal seal reports SEAL_ONLY, not OK."""
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    spine = _seal_only_spine(tmp_path)
    result = spine.verify()
    assert result.status is SpineStatus.SEAL_ONLY
    assert not result.ok
    # The chain is still cryptographically intact - this is not tampering.
    assert result.count == 1


def test_chain_with_artifact_entry_is_ok(tmp_path: Path, monkeypatch) -> None:
    """Adding a real produced-artifact entry restores an OK verify."""
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    spine = _seal_only_spine(tmp_path)
    # A genuine artifact-provenance entry (not a journal seal).
    spine.record(
        artifact_path="src/produced.py",
        content=b"print('hi')\n",
        actor="agent:test",
        step_id="tc-1",
        model="claude",
        timestamp=1,
    )
    result = spine.verify()
    assert result.status is SpineStatus.OK
    assert result.ok
