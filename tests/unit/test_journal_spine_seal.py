"""The journal head is sealed into the lineage spine root (issue #2293, AC5).

At run finalization the canonical journal's head hash is recorded into
the f01 lineage spine so a run's replay identity and its artifact
provenance share one root.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine, SpineStatus
from bernstein.core.replay.journal import EventJournal, seal_journal_into_spine


def test_journal_head_appears_in_spine_root(tmp_path: Path) -> None:
    """After sealing, the spine chain carries the journal head hash."""
    sdd_dir = tmp_path / ".sdd"
    lineage_root = sdd_dir / "lineage"
    hmac_key = b"k" * 32

    journal = EventJournal(run_id="run-seal", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-seal")
    journal.record("run_completed", run_id="run-seal")
    head = journal.head()
    assert head

    seal_journal_into_spine(
        journal,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        actor="orchestrator",
    )

    spine = LineageSpine(lineage_root, run_id="run-seal", hmac_key=hmac_key)
    entries = list(spine.iter_entries())
    assert entries, "spine should carry the sealed journal entry"
    seal = entries[-1]
    # The journal head is embedded in the spine step_id so a verifier can
    # pin the run's replay identity against its provenance chain.
    assert head in seal.step_id
    assert seal.artifact_path.endswith("journal.jsonl")
    # The chain is cryptographically intact, but it records only the journal
    # seal and no produced-artifact provenance, so verify reports SEAL_ONLY
    # rather than a clean OK (issue #2789).
    result = spine.verify()
    assert result.status is SpineStatus.SEAL_ONLY
    assert not result.ok


def test_seal_is_noop_when_lineage_disabled(monkeypatch, tmp_path: Path) -> None:
    """Disabling the spine gate makes sealing a no-op, not an error."""
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "0")
    sdd_dir = tmp_path / ".sdd"
    lineage_root = sdd_dir / "lineage"
    journal = EventJournal(run_id="run-off", sdd_dir=sdd_dir)
    journal.record("run_started")

    # Must not raise and must not create the lineage root.
    seal_journal_into_spine(
        journal,
        lineage_root=lineage_root,
        hmac_key=b"k" * 32,
        actor="orchestrator",
    )
    assert not (lineage_root / "run-off").exists()
