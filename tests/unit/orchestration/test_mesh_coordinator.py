"""Leaderless MESH coordination over the signed claim journal (issue #2558).

Phases 3-5: the MESH topology branch, receipt gossip with fork detection, the
``bernstein cluster claims`` CLI, and the MESH config keys. The tests here
assert the issue's acceptance criteria that the phase 1-2 substrate could not
reach on its own:

* **Determinism across nodes** -- two *independently instantiated* coordinators
  that never share a process fold the same gossiped receipt set into
  byte-identical state and an identical head hash.
* **Verifiability** -- the offline replay checks audit-chain anchors as well as
  chain links and signatures, and fails at the exact entry index on tamper.
* **Isolation of failure** -- a simulated partition surfaces as a signed
  ``fork`` receipt carrying the divergence entry index and is never merged.
* **No central server** -- two nodes claim, execute, and release against one
  logical workspace while ``POST /cluster/steal`` and the central
  ``NodeRegistry`` are booby-trapped to fail the test if touched.
* **STAR is unchanged** -- the topology branch returns ``None`` for STAR, and a
  ledger with no journal writes no receipts.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.lineage.tracker_audit import GENESIS_PREV_HASH, _canonical_bytes
from bernstein.core.orchestration.orchestrator import start_cluster_coordinator
from bernstein.core.orchestration.tracker_pipeline import (
    CLAIM_JOURNAL_SCHEMA_VERSION,
    ClaimJournal,
    ClaimReceipt,
    compute_claim_entry_hash,
    project_claims,
)
from bernstein.core.protocols.cluster.mesh_coordinator import (
    MeshCoordinator,
    build_mesh_coordinator,
)
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.tasks.models import ClusterConfig, ClusterTopology

if TYPE_CHECKING:
    from pathlib import Path

_HMAC_KEY = b"0" * 32


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _kms(tmp_path: Path, *, seed: int, name: str) -> Any:
    """Return a deterministic file-backed Ed25519 signer for one node."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

    key_path = tmp_path / f"{name}.pem"
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return FileBasedKMSAdapter(key_path, kid=name)


def _coordinator(
    tmp_path: Path,
    *,
    node_id: str,
    seed: int,
    journal_name: str | None = None,
    chain: AuditChainStore | None = None,
    lease_ttl_s: int = 300,
    peers: tuple[str, ...] = (),
) -> MeshCoordinator:
    """Build a coordinator with its own journal file (a distinct machine)."""
    name = journal_name or f"{node_id}_journal.jsonl"
    return MeshCoordinator(
        journal=ClaimJournal(
            tmp_path / name,
            kms_adapter=_kms(tmp_path, seed=seed, name=f"{node_id}-key"),
            node_id=node_id,
            chain=chain,
        ),
        lease_ttl_s=lease_ttl_s,
        peers=peers,
    )


# ---------------------------------------------------------------------------
# Criterion 1: byte-identical projection across two independent nodes
# ---------------------------------------------------------------------------


def test_two_independent_nodes_reduce_a_gossiped_journal_identically(tmp_path: Path) -> None:
    """Issue criterion 1, over the gossip path rather than a shared file.

    Node A mints receipts. Node B is a *separate* coordinator with its own
    journal file and its own signing key, and receives A's receipts only
    through :meth:`MeshCoordinator.ingest` -- the same call the gossip route
    makes. After ingest, both nodes' projected state must serialise to
    identical bytes and both journals must report the same head hash.

    Byte-comparison, not field-by-field: a field added to the projection that
    one node computed and the other did not would fail here.
    """
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)

    node_a.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)
    node_a.claim(tracker="jira", ticket_id="T-2", role="qa", claimer_id="w-a2", now=1001.0)
    node_a.release(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1002.0)
    node_a.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a3", now=1003.0)

    # Gossip: node B receives A's receipts over the wire (JSON round-trip) and
    # folds each one through the verifying ingest path.
    for receipt in node_a.journal.read():
        wire = ClaimReceipt.from_dict(receipt.to_dict())
        result = node_b.ingest(wire, now=2000.0)
        assert result.status == "applied", result.reason

    state_a = node_a.state()
    state_b = node_b.state()

    assert state_a.canonical_bytes() == state_b.canonical_bytes()
    assert state_a.head == state_b.head
    assert node_a.head() == node_b.head()
    # And the two files are byte-identical: the ingest path preserves the
    # signed bytes rather than re-signing under the receiving node's key.
    assert node_a.journal.path.read_bytes() == node_b.journal.path.read_bytes()

    holder = state_b.holder("jira", "T-1", "backend")
    assert holder is not None
    assert holder.claimer_id == "w-a3"


def test_projection_ignores_receipt_arrival_order_for_the_winner(tmp_path: Path) -> None:
    """The winner is a function of content hashes, not of who observed first.

    Two nodes each mint a competing claim; a third folds them in one order and
    a fourth in the reverse. Both must pick the same holder. This is the
    property that lets the fold stay clock-free: no wall-clock tiebreak, no
    node-identity tiebreak.
    """
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)

    a_claim = node_a.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-9",
        role="backend",
        claimer_id="w-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    b_claim = node_b.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-9",
        role="backend",
        claimer_id="w-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )

    forward = project_claims([a_claim, b_claim])
    reverse = project_claims([b_claim, a_claim])
    expected = min(a_claim.entry_hash, b_claim.entry_hash)

    for state in (forward, reverse):
        hold = state.holder("jira", "T-9", "backend")
        assert hold is not None
        assert hold.entry_hash == expected


# ---------------------------------------------------------------------------
# Criterion 2: verifiability -- audit-chain anchors, offline
# ---------------------------------------------------------------------------


def test_verify_checks_audit_chain_anchors(tmp_path: Path) -> None:
    """``verify(chain=...)`` confirms every receipt is anchored in the chain."""
    chain = AuditChainStore(tmp_path / "audit", key=_HMAC_KEY)
    node = _coordinator(tmp_path, node_id="node-a", seed=1, chain=chain)
    node.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)

    result = node.journal.verify(chain=chain)
    assert result.ok, result.failures
    assert result.anchors_checked is True
    assert result.entry_count == 1
    assert result.head == node.head()
    assert result.clean is True


def test_verify_fails_on_a_receipt_with_no_audit_anchor(tmp_path: Path) -> None:
    """A journal rebuilt wholesale is internally consistent but unanchored.

    The receipt below chains and signs correctly -- offline link + signature
    checks pass -- but it was never mirrored into the HMAC chain. Without the
    anchor check, a fabricated coordination history would verify clean.
    """
    chain = AuditChainStore(tmp_path / "audit", key=_HMAC_KEY)
    anchored = _coordinator(tmp_path, node_id="node-a", seed=1, chain=chain)
    anchored.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)

    # Second receipt appended with no chain wired: valid, but unanchored.
    unanchored = MeshCoordinator(
        journal=ClaimJournal(
            anchored.journal.path,
            kms_adapter=_kms(tmp_path, seed=1, name="node-a-key"),
            node_id="node-a",
            chain=None,
        ),
    )
    unanchored.claim(tracker="jira", ticket_id="T-2", role="qa", claimer_id="w-b", now=1001.0)

    assert anchored.journal.verify().ok  # links + signatures alone: clean
    result = anchored.journal.verify(chain=chain)
    assert result.ok is False
    assert result.bad_index == 1
    assert "no audit-chain anchor" in result.failures[0]


def test_verify_flipped_byte_reports_exact_index_with_anchors_on(tmp_path: Path) -> None:
    """Tamper still fails at the exact entry index when anchors are checked."""
    chain = AuditChainStore(tmp_path / "audit", key=_HMAC_KEY)
    node = _coordinator(tmp_path, node_id="node-a", seed=1, chain=chain)
    for index in range(3):
        node.claim(
            tracker="jira",
            ticket_id=f"T-{index}",
            role="backend",
            claimer_id=f"w-{index}",
            now=1000.0 + index,
        )

    lines = node.journal.path.read_bytes().splitlines()
    lines[1] = lines[1].replace(b'"w-1"', b'"w-X"')
    node.journal.path.write_bytes(b"\n".join(lines) + b"\n")

    result = node.journal.verify(chain=chain)
    assert result.ok is False
    assert result.bad_index == 1


def test_legacy_schema_receipt_still_verifies_after_the_field_bump(tmp_path: Path) -> None:
    """An append-only journal written before this change keeps verifying.

    Schema v3 adds four fields. Hashing the new field set over an old receipt
    would recompute a different ``entry_hash`` and report tamper on a file
    nobody touched -- so the signing payload projects away fields introduced
    after the receipt's own ``schema_version``.
    """
    kms = _kms(tmp_path, seed=1, name="legacy-key")
    from bernstein.core.security.audit_head_signature import build_head_signature

    legacy_body = {
        "schema_version": 2,
        "kind": "claim",
        "ts_ns": 1_000_000_000,
        "tracker": "jira",
        "ticket_id": "T-1",
        "role": "backend",
        "claimer_id": "w-a",
        "node_id": "node-legacy",
        "lease_expires_at": 1600.0,
        "prev_entry_hash": GENESIS_PREV_HASH,
        "entry_hash": "",
        "signature": {},
        "supersedes": None,
        "winner_claimer_id": None,
        "winner_entry_hash": None,
        "superseded_node_id": None,
        "superseded_claimer_id": None,
    }
    import hashlib

    digest = "sha256:" + hashlib.sha256(_canonical_bytes(legacy_body)).hexdigest()
    legacy_body["entry_hash"] = digest
    legacy_body["signature"] = build_head_signature(digest.split(":", 1)[1], kms_adapter=kms)

    path = tmp_path / "legacy.jsonl"
    path.write_bytes(_canonical_bytes(legacy_body) + b"\n")

    journal = ClaimJournal(path, kms_adapter=kms, node_id="verifier")
    result = journal.verify()
    assert result.ok, result.failures
    assert result.entry_count == 1
    # And the parsed receipt recomputes to the hash the old release produced.
    receipt = journal.read()[0]
    assert receipt.schema_version == 2
    assert compute_claim_entry_hash(receipt) == digest
    assert receipt.schema_version < CLAIM_JOURNAL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Criterion 4: fork detection with the divergence entry index
# ---------------------------------------------------------------------------


def test_partition_produces_a_signed_fork_receipt_with_divergence_index(tmp_path: Path) -> None:
    """Issue criterion 4: two divergent heads are reported, never merged.

    Both nodes agree on entries 0 and 1, then the partition heals and node A is
    handed a receipt built on node B's divergent entry 2. It must not merge:
    it appends a signed ``fork`` receipt naming the divergence entry index, and
    ``verify`` reports the fork while confirming the chain is still intact.
    """
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)

    # Shared prefix: two receipts both sides hold.
    for index in range(2):
        receipt = node_a.journal.append(
            kind="claim",
            tracker="jira",
            ticket_id=f"T-{index}",
            role="backend",
            claimer_id=f"w-{index}",
            lease_expires_at=1600.0,
            ts_ns=1_000_000_000 + index,
        )
        assert node_b.ingest(receipt, now=1000.0).status == "applied"
    shared_head = node_a.head()
    assert node_b.head() == shared_head

    # Partition: each side appends its own entry 2 on top of the shared head.
    node_a.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-A",
        role="backend",
        claimer_id="w-a",
        lease_expires_at=1600.0,
        ts_ns=1_100_000_000,
    )
    b_side = node_b.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-B",
        role="backend",
        claimer_id="w-b",
        lease_expires_at=1600.0,
        ts_ns=1_100_000_000,
    )
    assert node_a.head() != node_b.head()

    # Heal: B gossips its divergent entry to A.
    result = node_a.ingest(b_side, now=1200.0)
    assert result.status == "forked"
    # Entries 0 and 1 are shared; the chains disagree at index 2.
    assert result.divergence_index == 2
    assert result.fork_receipt is not None
    assert result.fork_receipt.kind == "fork"
    assert result.fork_receipt.fork_entry_hash == b_side.entry_hash
    assert result.fork_receipt.node_id == "node-a"

    # The foreign receipt was NOT merged.
    assert b_side.entry_hash not in {r.entry_hash for r in node_a.journal.read()}

    # verify: chain intact, fork surfaced with the divergence index.
    verified = node_a.journal.verify()
    assert verified.ok, verified.failures
    assert verified.clean is False
    assert len(verified.forks) == 1
    assert verified.forks[0].divergence_index == 2
    assert verified.forks[0].entry_hash == b_side.entry_hash
    assert verified.forks[0].observed_by == "node-a"


def test_fork_receipt_is_signed_and_survives_replay(tmp_path: Path) -> None:
    """The fork record is itself a signed, chained entry, not a side log."""
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)

    node_a.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)
    orphan = node_b.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-2",
        role="qa",
        claimer_id="w-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )

    result = node_a.ingest(orphan, now=1100.0)
    assert result.status == "forked"

    fork_receipt = node_a.journal.read()[-1]
    assert fork_receipt.kind == "fork"
    assert fork_receipt.signature != {}
    assert compute_claim_entry_hash(fork_receipt) == fork_receipt.entry_hash
    # A fork observation must not change who holds what.
    assert node_a.state().holder("jira", "T-2", "qa") is None


def test_ingest_rejects_a_tampered_receipt_without_writing(tmp_path: Path) -> None:
    """Verification precedes any write: bad receipts never touch the journal."""
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)

    good = node_b.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="w-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    tampered = replace(good, claimer_id="attacker")

    before = node_a.head()
    result = node_a.ingest(tampered, now=1100.0)
    assert result.status == "rejected"
    assert "entry_hash mismatch" in (result.reason or "")
    assert node_a.head() == before
    assert not node_a.journal.path.exists() or node_a.journal.path.read_bytes() == b""


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    """Re-gossiping a receipt already on disk is a no-op, not a fork."""
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)

    receipt = node_b.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="w-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    assert node_a.ingest(receipt, now=1100.0).status == "applied"
    assert node_a.ingest(receipt, now=1200.0).status == "duplicate"
    assert len(node_a.journal.read()) == 1


def test_ingest_pins_node_keys_when_trusted_keys_supplied(tmp_path: Path) -> None:
    """A receipt signed by a key not pinned for its ``node_id`` is refused."""
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)
    receipt = node_b.journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="w-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    strict = MeshCoordinator(
        journal=ClaimJournal(
            tmp_path / "strict.jsonl",
            kms_adapter=_kms(tmp_path, seed=1, name="node-a-key"),
            node_id="node-a",
        ),
        trusted_keys={"node-z": {"kty": "OKP"}},
    )
    result = strict.ingest(receipt, now=1100.0)
    assert result.status == "rejected"
    assert "no trusted key pinned" in (result.reason or "")


# ---------------------------------------------------------------------------
# Criterion 5: no central server
# ---------------------------------------------------------------------------


def test_two_nodes_claim_execute_release_with_no_central_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue criterion 5: leaderless work against one logical workspace.

    ``NodeRegistry`` and the ``/cluster/steal`` handler are replaced with traps
    that fail the test if invoked, so "no central arbiter" is enforced rather
    than asserted in prose. Two nodes then contend for one key, one wins by the
    deterministic rule, executes, releases, and the other picks it up -- all
    through the shared signed journal with no server running.

    Both nodes confirm before treating the claim as held: ``claim`` reports the
    journal at append time, and a competing claim can still arrive after it.
    """
    import bernstein.core.protocols.cluster.cluster as cluster_mod
    import bernstein.core.routes.task_cluster as task_cluster_mod

    def _central_trap(*args: object, **kwargs: object) -> object:
        pytest.fail("MESH coordination must not touch the central NodeRegistry / steal path")

    monkeypatch.setattr(cluster_mod, "NodeRegistry", _central_trap)
    monkeypatch.setattr(task_cluster_mod, "steal_tasks", _central_trap)

    shared = "shared_journal.jsonl"
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1, journal_name=shared)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2, journal_name=shared)

    key = {"tracker": "jira", "ticket_id": "T-1", "role": "backend"}
    outcome_a = node_a.claim(**key, claimer_id="w-a", now=1000.0)
    outcome_b = node_b.claim(**key, claimer_id="w-b", now=1000.0)

    # Phase two: each node re-checks once both claims are on the journal.
    outcome_a = node_a.confirm(outcome_a, **key, now=1001.0)
    outcome_b = node_b.confirm(outcome_b, **key, now=1001.0)

    # Exactly one holder, and both nodes agree which.
    assert outcome_a.granted != outcome_b.granted
    winner, loser = (node_a, node_b) if outcome_a.granted else (node_b, node_a)
    winning_outcome = outcome_a if outcome_a.granted else outcome_b
    losing_outcome = outcome_b if outcome_a.granted else outcome_a

    # The winner is the lexicographically lowest entry hash -- no clock, no
    # node identity, no arrival order.
    assert winning_outcome.entry_hash == min(outcome_a.entry_hash, outcome_b.entry_hash)
    assert node_a.state().canonical_bytes() == node_b.state().canonical_bytes()
    assert losing_outcome.superseded_by == winning_outcome.entry_hash

    # The loser holds a chain-anchored supersede receipt naming the winner.
    supersedes = [r for r in loser.journal.read() if r.kind == "supersede"]
    assert len(supersedes) == 1
    assert supersedes[0].supersedes == losing_outcome.entry_hash
    assert supersedes[0].winner_entry_hash == winning_outcome.entry_hash

    # Execute, then release. The other node then claims the freed key.
    winner_claimer = "w-a" if outcome_a.granted else "w-b"
    winner.release(tracker="jira", ticket_id="T-1", role="backend", claimer_id=winner_claimer, now=1100.0)
    assert winner.state().holder("jira", "T-1", "backend") is None

    second = loser.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-second", now=1200.0)
    assert second.granted is True
    assert node_a.state().canonical_bytes() == node_b.state().canonical_bytes()
    assert node_a.journal.verify().clean is True


def test_expired_peer_lease_is_retired_without_a_central_sweep(tmp_path: Path) -> None:
    """A dead node's lease is reclaimable by any observer, honestly attributed.

    With no central server there is no timeout sweep. The ``expire`` receipt is
    signed by the observing node and names the retired claim as referenced
    data, so its declared identity still matches its signer.
    """
    shared = "shared_journal.jsonl"
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1, journal_name=shared, lease_ttl_s=60)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2, journal_name=shared, lease_ttl_s=60)

    held = node_a.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)
    assert held.granted is True

    # Not yet expired.
    assert node_b.expire_stale(now=1030.0) == []
    assert node_b.state().holder("jira", "T-1", "backend") is not None

    emitted = node_b.expire_stale(now=1100.0)
    assert len(emitted) == 1
    assert emitted[0].kind == "expire"
    assert emitted[0].node_id == "node-b"  # signer identity, not the holder's
    assert emitted[0].target_entry_hash == held.entry_hash
    assert node_a.state().holder("jira", "T-1", "backend") is None

    reclaimed = node_b.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-b", now=1101.0)
    assert reclaimed.granted is True
    assert node_a.journal.verify().clean is True


def test_release_does_not_retire_a_later_claim_on_the_same_key(tmp_path: Path) -> None:
    """A stale double-release must not evict whoever holds the key now."""
    shared = "shared_journal.jsonl"
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1, journal_name=shared)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2, journal_name=shared)

    first = node_a.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)
    node_a.release(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1001.0)
    second = node_b.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-b", now=1002.0)
    assert second.granted is True

    # Replay of the first node's release, still naming the *first* claim.
    node_a.release(
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="w-a",
        now=1003.0,
        target_entry_hash=first.entry_hash,
    )
    holder = node_b.state().holder("jira", "T-1", "backend")
    assert holder is not None
    assert holder.entry_hash == second.entry_hash


# ---------------------------------------------------------------------------
# Topology branch + config (phases 3 and 5)
# ---------------------------------------------------------------------------


def test_star_topology_starts_no_mesh_coordinator(tmp_path: Path) -> None:
    """MESH stays opt-in: STAR provisions no journal and no signing identity."""
    star = ClusterConfig(enabled=True, topology=ClusterTopology.STAR)
    assert start_cluster_coordinator(star, sdd_dir=tmp_path / ".sdd") is None
    assert build_mesh_coordinator(star, sdd_dir=tmp_path / ".sdd") is None
    assert start_cluster_coordinator(None, sdd_dir=tmp_path / ".sdd") is None
    assert not (tmp_path / ".sdd" / "cluster").exists()


def test_mesh_topology_starts_a_coordinator_over_a_signed_journal(tmp_path: Path) -> None:
    """``topology: mesh`` starts the leaderless path, not the central one."""
    mesh = ClusterConfig(
        enabled=True,
        topology=ClusterTopology.MESH,
        gossip_peers=("https://peer-b:8052",),
        claim_lease_ttl_s=120,
    )
    coordinator = start_cluster_coordinator(mesh, sdd_dir=tmp_path / ".sdd")
    assert coordinator is not None
    assert coordinator.peers == ("https://peer-b:8052",)
    assert coordinator.lease_ttl_s == 120
    # node_id is the install key's thumbprint, so identity and key are one fact.
    assert coordinator.node_id
    assert (tmp_path / ".sdd" / "cluster" / "identity" / "claim_signing.pem").is_file()

    outcome = coordinator.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)
    assert outcome.granted is True
    assert coordinator.journal.verify().clean is True


def test_seed_parser_accepts_and_validates_mesh_keys(tmp_path: Path) -> None:
    """MESH keys parse; a malformed or misplaced one fails the seed load."""
    from bernstein.core.config.seed_parser import SeedError, _parse_cluster

    cfg = _parse_cluster(
        {
            "enabled": True,
            "topology": "mesh",
            "gossip_peers": ["https://peer-b:8052", " https://peer-c:8052 "],
            "claim_lease_ttl_s": 90,
            "claim_journal_path": str(tmp_path / "j.jsonl"),
        }
    )
    assert cfg is not None
    assert cfg.is_mesh is True
    assert cfg.gossip_peers == ("https://peer-b:8052", "https://peer-c:8052")
    assert cfg.claim_lease_ttl_s == 90

    with pytest.raises(SeedError, match="gossip_peers must be a list"):
        _parse_cluster({"topology": "mesh", "gossip_peers": "peer-b"})
    with pytest.raises(SeedError, match=r"gossip_peers\[0\] must be a non-empty string"):
        _parse_cluster({"topology": "mesh", "gossip_peers": [""]})
    with pytest.raises(SeedError, match="claim_lease_ttl_s must be a positive integer"):
        _parse_cluster({"topology": "mesh", "claim_lease_ttl_s": 0})
    with pytest.raises(SeedError, match="apply to topology 'mesh' only"):
        _parse_cluster({"topology": "star", "gossip_peers": ["https://peer-b:8052"]})


def test_star_cluster_config_keeps_its_defaults() -> None:
    """STAR behaviour is untouched by the new keys."""
    from bernstein.core.config.seed_parser import _parse_cluster

    cfg = _parse_cluster({"enabled": True, "topology": "star", "server_url": "http://central:8052"})
    assert cfg is not None
    assert cfg.topology is ClusterTopology.STAR
    assert cfg.is_mesh is False
    assert cfg.gossip_peers == ()
    assert cfg.claim_journal_path is None
    assert cfg.server_url == "http://central:8052"


# ---------------------------------------------------------------------------
# Gossip transport
# ---------------------------------------------------------------------------


def test_push_to_peers_reports_per_peer_outcomes(tmp_path: Path) -> None:
    """One unreachable peer must not stop the sweep or raise."""

    class _Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def post(self, url: str, **kwargs: Any) -> _Response:
            self.calls.append(url)
            if "peer-down" in url:
                raise RuntimeError("connection refused")
            if "peer-forked" in url:
                return _Response({"head": "sha256:beef", "accepted": 0, "forked": True})
            return _Response({"head": "sha256:cafe", "accepted": 2, "forked": False})

    node = _coordinator(
        tmp_path,
        node_id="node-a",
        seed=1,
        peers=("http://peer-ok:8052", "http://peer-down:8052", "http://peer-forked:8052"),
    )
    node.claim(tracker="jira", ticket_id="T-1", role="backend", claimer_id="w-a", now=1000.0)

    client = _Client()
    results = node.push_to_peers(client=client, auth_token="secret")

    assert len(client.calls) == 3
    assert all(url.endswith("/cluster/claims/gossip") for url in client.calls)
    assert results["http://peer-ok:8052"].ok is True
    assert results["http://peer-ok:8052"].accepted == 2
    assert results["http://peer-down:8052"].ok is False
    assert "connection refused" in (results["http://peer-down:8052"].error or "")
    assert results["http://peer-forked:8052"].forked is True


def test_ingest_many_stops_at_the_first_fork(tmp_path: Path) -> None:
    """One divergence yields one fork receipt, not one per trailing entry."""
    node_a = _coordinator(tmp_path, node_id="node-a", seed=1)
    node_b = _coordinator(tmp_path, node_id="node-b", seed=2)

    node_a.claim(tracker="jira", ticket_id="T-0", role="backend", claimer_id="w-a", now=1000.0)
    batch = [
        node_b.journal.append(
            kind="claim",
            tracker="jira",
            ticket_id=f"T-{index}",
            role="qa",
            claimer_id=f"w-b{index}",
            lease_expires_at=1600.0,
            ts_ns=1_000_000_000 + index,
        )
        for index in range(3)
    ]

    results = node_a.ingest_many(batch, now=1100.0)
    assert [r.status for r in results] == ["forked"]
    assert len([r for r in node_a.journal.read() if r.kind == "fork"]) == 1


# ---------------------------------------------------------------------------
# Receipt wire shape
# ---------------------------------------------------------------------------


def test_receipt_round_trips_the_new_fields(tmp_path: Path) -> None:
    """New schema fields survive the JSON wire form the gossip route uses."""
    node = _coordinator(tmp_path, node_id="node-a", seed=1)
    receipt = node.journal.append(
        kind="fork",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="node-a",
        lease_expires_at=0.0,
        ts_ns=1_000_000_000,
        fork_divergence_index=7,
        fork_entry_hash="sha256:" + "a" * 64,
        fork_local_head="sha256:" + "b" * 64,
    )
    rebuilt = ClaimReceipt.from_dict(receipt.to_dict())
    assert rebuilt == receipt
    assert asdict(rebuilt)["fork_divergence_index"] == 7
    assert compute_claim_entry_hash(rebuilt) == receipt.entry_hash
