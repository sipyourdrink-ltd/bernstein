"""Migration safety for enabling the HMAC audit chain (issue #2690, point 4).

The audit chain is opt-in today. Any move toward on-by-default must never turn
a workspace that predates the chain into a false tamper report: a workspace
with no chain has nothing to compare against, and the append-only chain gives
an operator no way to repair such a record after the fact. These tests lock the
invariant the migration ADR (ADR-010) depends on - that a chain-less workspace,
and the first run that starts a chain in it, both verify clean.

They are characterization tests: the behavior already holds. They exist so a
future default flip cannot regress it silently.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.audit import (
    _GENESIS_HMAC,  # pyright: ignore[reportPrivateUsage]
    AuditLog,
)

_KEY = b"migration-test-key"


def test_pre_chain_workspace_verifies_clean(tmp_path: Path) -> None:
    """A workspace whose audit dir never existed verifies clean, not tampered."""
    audit_dir = tmp_path / ".sdd" / "audit"
    assert not audit_dir.exists()

    log = AuditLog(audit_dir, key=_KEY)
    # No prior history means the chain resumes from genesis, not a bogus tail.
    assert log._prev_hmac == _GENESIS_HMAC  # pyright: ignore[reportPrivateUsage]

    ok, errors = log.verify()
    assert ok is True
    assert errors == []


def test_first_run_starting_a_chain_verifies_clean(tmp_path: Path) -> None:
    """Enabling the chain and writing the first event does not flag the run.

    The first event anchors to genesis; verifying it, and re-verifying from a
    fresh reader process, both report clean. This is the exact first-run path a
    default flip would trigger on an existing install.
    """
    audit_dir = tmp_path / ".sdd" / "audit"
    writer = AuditLog(audit_dir, key=_KEY)

    first = writer.log("schedule.decision", "orchestrator", "task", "t1", {"n": 1})
    assert first.prev_hmac == _GENESIS_HMAC

    ok, errors = writer.verify()
    assert ok is True, errors

    # A separate reader (new process) recovers the tail and re-verifies clean.
    reader = AuditLog(audit_dir, key=_KEY)
    ok2, errors2 = reader.verify()
    assert ok2 is True, errors2


def test_preexisting_sdd_state_is_not_mistaken_for_chain_history(tmp_path: Path) -> None:
    """Sibling .sdd state that predates the chain is ignored by the verifier.

    A workspace that predates the chain typically has lineage and runtime state
    already on disk. Starting the chain must key only off the audit dir's own
    JSONL segments, never off unrelated files, so their presence cannot make the
    first chained run look inconsistent.
    """
    sdd = tmp_path / ".sdd"
    (sdd / "lineage").mkdir(parents=True)
    (sdd / "lineage" / "spine.jsonl").write_text('{"artefact": "x"}\n', encoding="utf-8")
    (sdd / "runtime").mkdir(parents=True)
    (sdd / "runtime" / "session.json").write_text('{"pid": 1}\n', encoding="utf-8")

    audit_dir = sdd / "audit"
    log = AuditLog(audit_dir, key=_KEY)
    assert log._prev_hmac == _GENESIS_HMAC  # pyright: ignore[reportPrivateUsage]

    log.log("schedule.decision", "orchestrator", "task", "t1")
    ok, errors = log.verify()
    assert ok is True, errors


def test_empty_audit_dir_scans_verified_clean(tmp_path: Path) -> None:
    """An authenticated scan of an empty chain yields no events and no error."""
    audit_dir = tmp_path / ".sdd" / "audit"
    result = AuditLog(audit_dir, key=_KEY).scan_verified()
    assert result.ok is True
    assert result.events == []
    assert result.errors == []
