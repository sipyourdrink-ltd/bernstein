"""``bernstein audit verify`` reports lineage activity status using active_set (#4651).

The verify command reads lineage entries from the lineage store and computes
active vs inactive status using active_set(). The ledger file is never mutated
during verification.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.lineage.entry import LineageEntry
from bernstein.core.lineage.store import LineageStore
from bernstein.core.security.audit import AUDIT_KEY_ENV, AuditLog, load_or_create_audit_key


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated project with its own chain, lineage store, and pinned tmp HMAC key."""
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    monkeypatch.chdir(tmp_path)
    load_or_create_audit_key()
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return tmp_path


def _run(*args: str):
    return CliRunner().invoke(audit_group, list(args))


def _create_lineage_entry(
    path: Path,
    agent_id: str = "agent-1",
    agent_card_kid: str = "key-1",
    artefact_kind: str = "file",
) -> LineageEntry:
    """Create a minimal lineage entry."""
    artefact_path = str(path.relative_to(path.root) if path.root else str(path))
    entry = LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind=artefact_kind,
        content_hash="sha256:" + "a" * 64,
        parent_hashes=[],
        agent_id=agent_id,
        agent_card_kid=agent_card_kid,
        tool_call_id="",
        span_id="",
        ts_ns=0,
        operator_hmac="0" * 64,
    )
    return entry


def _write_lineage_entry(store: LineageStore, entry: LineageEntry) -> None:
    """Write a lineage entry directly to the store."""
    from bernstein.core.lineage.entry import canonicalise

    canonical = canonicalise(entry)
    with store.log_path.open("ab") as log_fh:
        log_fh.write(canonical + b"\n")
        log_fh.flush()


def _setup_audit_dir(project: Path) -> None:
    """Set up a minimal audit directory with one event and seal."""
    audit_dir = project / ".sdd" / "audit"
    audit_dir.mkdir(parents=True)
    key = load_or_create_audit_key()
    log = AuditLog(audit_dir, key=key)
    log.log("task.complete", "agent-1", "task", "t-1", {})

    # Seal the audit directory
    from bernstein.core.merkle import compute_seal, save_seal

    _tree, seal = compute_seal(audit_dir)
    merkle_dir = audit_dir / "merkle"
    merkle_dir.mkdir(parents=True, exist_ok=True)
    save_seal(seal, merkle_dir)


def test_verify_reports_lineage_activity_status_when_lineage_exists(project: Path) -> None:
    """Verify command reports lineage entry counts when lineage store exists."""
    _setup_audit_dir(project)

    lineage_dir = project / ".sdd" / "lineage"
    lineage_dir.mkdir(parents=True)
    store = LineageStore(lineage_dir)

    entry1 = _create_lineage_entry(project / "file1.py")
    entry2 = _create_lineage_entry(project / "file2.py")

    _write_lineage_entry(store, entry1)
    _write_lineage_entry(store, entry2)

    result = _run("verify")

    assert "Lineage Activity Status" in result.output
    assert "Total entries" in result.output
    assert "Active entries" in result.output
    assert "Inactive entries" in result.output


def test_verify_reports_zero_entries_when_lineage_empty(project: Path) -> None:
    """Verify command handles empty lineage store gracefully."""
    _setup_audit_dir(project)

    lineage_dir = project / ".sdd" / "lineage"
    lineage_dir.mkdir(parents=True)

    result = _run("verify")

    assert "Lineage Activity Status" in result.output
    assert "Total entries" in result.output


def test_verify_does_not_mutate_lineage_store(project: Path) -> None:
    """Verify command does not modify the lineage store."""
    _setup_audit_dir(project)

    lineage_dir = project / ".sdd" / "lineage"
    lineage_dir.mkdir(parents=True)
    store = LineageStore(lineage_dir)

    entry1 = _create_lineage_entry(project / "file1.py")
    entry2 = _create_lineage_entry(project / "file2.py")

    _write_lineage_entry(store, entry1)
    _write_lineage_entry(store, entry2)

    def digest_dir(p: Path) -> dict[str, str]:
        return {
            str(rel): hashlib.sha256(p.joinpath(rel).read_bytes()).hexdigest()
            for rel in sorted(p.rglob("*"))
            if rel.is_file()
        }

    before = digest_dir(lineage_dir)

    result = _run("verify")
    assert result.exit_code == 0

    after = digest_dir(lineage_dir)

    assert after == before, "Lineage store was mutated during verify"


def test_verify_shows_all_entries_active_when_no_seeds(project: Path) -> None:
    """Verify command shows all entries as active when no seeds are present."""
    _setup_audit_dir(project)

    lineage_dir = project / ".sdd" / "lineage"
    lineage_dir.mkdir(parents=True)
    store = LineageStore(lineage_dir)

    entry1 = _create_lineage_entry(project / "file1.py")
    entry2 = _create_lineage_entry(project / "file2.py")

    _write_lineage_entry(store, entry1)
    _write_lineage_entry(store, entry2)

    result = _run("verify")

    assert "Lineage Activity Status" in result.output
    assert "2" in result.output
    assert "0" in result.output


def test_verify_counts_match_entry_count(project: Path) -> None:
    """Verify command counts match the actual entry count."""
    _setup_audit_dir(project)

    lineage_dir = project / ".sdd" / "lineage"
    lineage_dir.mkdir(parents=True)
    store = LineageStore(lineage_dir)

    entry1 = _create_lineage_entry(project / "file1.py")
    entry2 = _create_lineage_entry(project / "file2.py")
    entry3 = _create_lineage_entry(project / "file3.py")

    _write_lineage_entry(store, entry1)
    _write_lineage_entry(store, entry2)
    _write_lineage_entry(store, entry3)

    result = _run("verify")

    assert "Total entries" in result.output and "3" in result.output
    assert "Active entries" in result.output and "3" in result.output
    assert "Inactive entries" in result.output and "0" in result.output


# ---------------------------------------------------------------------------
# Inactive and revoked entries fail the pillar
# ---------------------------------------------------------------------------


def _lineage_store(project: Path) -> LineageStore:
    store = LineageStore(project / ".sdd" / "lineage")
    store.log_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def test_lineage_pillar_passes_when_every_entry_is_active(project: Path) -> None:
    from bernstein.cli.commands.audit_cmd import _verify_lineage_active_set

    _write_lineage_entry(_lineage_store(project), _create_lineage_entry(project / "a.py"))

    assert _verify_lineage_active_set() is True


def test_lineage_pillar_fails_on_entry_with_unknown_parent(project: Path) -> None:
    from dataclasses import replace

    from bernstein.cli.commands.audit_cmd import _verify_lineage_active_set

    orphan = replace(_create_lineage_entry(project / "b.py"), parent_hashes=["sha256:" + "f" * 64])
    _write_lineage_entry(_lineage_store(project), orphan)

    assert _verify_lineage_active_set() is False


def test_lineage_pillar_fails_on_revoked_entry(project: Path) -> None:
    from bernstein.cli.commands.audit_cmd import _verify_lineage_active_set
    from bernstein.core.lineage.entry import entry_hash
    from bernstein.core.security.audit_chain import EVENT_MANDATE_REVOCATION, AuditChainStore

    entry = _create_lineage_entry(project / "c.py")
    _write_lineage_entry(_lineage_store(project), entry)
    audit_dir = project / ".sdd" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    chain = AuditChainStore(audit_dir, key=load_or_create_audit_key())
    chain.log_with_prev_digest(
        event_type=EVENT_MANDATE_REVOCATION,
        actor="operator",
        resource_type="mandate_revocation",
        resource_id="m-1",
        details={"lineage_entry_hash": entry_hash(entry), "reason": "revoked"},
    )

    assert _verify_lineage_active_set() is False


def test_lineage_pillar_fails_closed_when_revocations_cannot_be_read(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.cli.commands.audit_cmd import _verify_lineage_active_set
    from bernstein.core.security.audit import AuditKeyMissingError

    _write_lineage_entry(_lineage_store(project), _create_lineage_entry(project / "d.py"))
    (project / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)

    def _no_key(*_a: object, **_k: object) -> bytes:
        raise AuditKeyMissingError("no key")

    monkeypatch.setattr("bernstein.core.security.audit.load_audit_key", _no_key)

    assert _verify_lineage_active_set() is False


def test_audit_verify_names_the_lineage_pillar_on_inactive_entries(project: Path) -> None:
    from dataclasses import replace

    _setup_audit_dir(project)
    orphan = replace(_create_lineage_entry(project / "e.py"), parent_hashes=["sha256:" + "e" * 64])
    _write_lineage_entry(_lineage_store(project), orphan)

    result = _run("verify")

    assert result.exit_code != 0
    assert "Lineage Activity" in result.output
