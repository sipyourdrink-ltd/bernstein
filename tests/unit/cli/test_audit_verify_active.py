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
