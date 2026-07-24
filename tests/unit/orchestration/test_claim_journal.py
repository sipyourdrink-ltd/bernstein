"""Unit tests for the signed, Merkle-chained claim journal (issue #2558).

Phases 1-2 of leaderless MESH cluster mode land a signed, append-only
claim journal beside the SQLite :class:`ClaimLedger`, plus a pure fold
``project_claims`` that turns an ordered set of receipts into a
:class:`ClaimState`. The tests below assert the two binding criteria from
the issue:

* **Determinism** -- two independently instantiated nodes given the same
  ordered receipt set produce a byte-identical projected ``ClaimState``
  and an identical journal head hash (serialise both and byte-compare).
* **Leaderless convergence** -- two nodes concurrently self-claiming the
  same ``(tracker, ticket_id, role)`` converge on exactly one holder by
  the deterministic lowest-``entry_hash`` rule, and the loser holds a
  chain-anchored ``claim_superseded`` receipt naming the winner.

The verifiability / tamper-evidence path (offline replay checking every
chain link and Ed25519 signature) is exercised too: a single flipped byte
fails verification at the exact entry index.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

import pytest

from bernstein.core.lineage.tracker_audit import (
    GENESIS_PREV_HASH,
    _canonical_bytes,
)
from bernstein.core.orchestration.tracker_pipeline import (
    CLAIM_JOURNAL_SCHEMA_VERSION,
    CLAIM_RECEIPT_KINDS,
    ClaimJournal,
    ClaimLedger,
    ClaimReceipt,
    ClaimState,
    compute_claim_entry_hash,
    project_claims,
)
from bernstein.core.security.audit_chain import (
    EVENT_CLAIM_JOURNAL_RECEIPT,
    AuditChainStore,
)
from bernstein.core.security.audit_head_signature import build_head_signature
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

if TYPE_CHECKING:
    from pathlib import Path

_HMAC_KEY = b"0" * 32


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _kms(tmp_path: Path, *, seed: int, name: str) -> FileBasedKMSAdapter:
    """Return a deterministic file-backed Ed25519 signer for one node.

    A distinct ``seed`` models a distinct node install identity, so two
    nodes sign the same receipt bytes with different keys -- exactly the
    leaderless shape.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_path = tmp_path / f"{name}.pem"
    if not key_path.exists():
        private_key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return FileBasedKMSAdapter(key_path, kid=name)


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_HMAC_KEY)


def _journal(
    tmp_path: Path,
    *,
    path_name: str = "claim_journal.jsonl",
    node_id: str = "node-a",
    seed: int = 1,
    chain: AuditChainStore | None = None,
) -> ClaimJournal:
    return ClaimJournal(
        tmp_path / path_name,
        kms_adapter=_kms(tmp_path, seed=seed, name=f"{node_id}-key"),
        node_id=node_id,
        chain=chain,
    )


# ---------------------------------------------------------------------------
# Receipt hashing
# ---------------------------------------------------------------------------


def test_entry_hash_excludes_signature(tmp_path: Path) -> None:
    """The chain hash must be signature-independent.

    Two nodes signing the same receipt body with different keys must
    compute the same ``entry_hash`` -- otherwise the journal head would
    depend on who signed, breaking the byte-identical-fold guarantee.
    """
    journal_a = _journal(tmp_path, node_id="node-a", seed=1)
    receipt = journal_a.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    # Recompute over the same body with the signature blanked.
    assert compute_claim_entry_hash(receipt) == receipt.entry_hash
    # A receipt with an identical body but a foreign signature block still
    # hashes to the same entry_hash.
    from dataclasses import replace

    forged_sig = replace(receipt, signature={"alg": "EdDSA", "signature_b64": "AAAA"})
    assert compute_claim_entry_hash(forged_sig) == receipt.entry_hash


def test_append_chains_prev_entry_hash(tmp_path: Path) -> None:
    """Each receipt links its predecessor; the head advances."""
    journal = _journal(tmp_path)
    r1 = journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    r2 = journal.append(
        kind="release",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=0.0,
        ts_ns=1_001_000_000,
    )
    assert r1.prev_entry_hash.startswith("sha256:")
    assert r2.prev_entry_hash == r1.entry_hash
    assert journal.head() == r2.entry_hash
    assert set(CLAIM_RECEIPT_KINDS) == {"claim", "release", "renew", "expire", "supersede"}


def test_signature_verifies_and_tamper_fails(tmp_path: Path) -> None:
    """The Ed25519 node signature authenticates the receipt binding."""
    journal = _journal(tmp_path, node_id="node-a", seed=7)
    receipt = journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    assert journal.verify().ok

    from dataclasses import replace

    # Flip the claimer without re-signing: entry_hash no longer matches.
    tampered = replace(receipt, claimer_id="worker-evil")
    assert compute_claim_entry_hash(tampered) != receipt.entry_hash


# ---------------------------------------------------------------------------
# Determinism (byte-identical fold) -- the heart
# ---------------------------------------------------------------------------


def test_project_claims_is_byte_identical_across_nodes(tmp_path: Path) -> None:
    """Two nodes folding the same ordered receipts agree byte-for-byte."""
    journal = _journal(tmp_path)
    journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-2",
        role="qa",
        claimer_id="worker-b",
        lease_expires_at=1700.0,
        ts_ns=1_001_000_000,
    )
    journal.append(
        kind="release",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=0.0,
        ts_ns=1_002_000_000,
    )
    journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-c",
        lease_expires_at=1800.0,
        ts_ns=1_003_000_000,
    )

    # Node 1 reads the receipts; node 2 receives them over the wire (JSON
    # round-trip) and reparses -- modelling a gossiped receipt set.
    receipts_node1 = journal.read()
    receipts_node2 = [ClaimReceipt.from_dict(r.to_dict()) for r in receipts_node1]

    state1 = project_claims(receipts_node1)
    state2 = project_claims(receipts_node2)

    # Literal byte-comparison of the canonical serialisation.
    assert state1.canonical_bytes() == state2.canonical_bytes()
    assert state1.head == state2.head

    # T-1 ended up with worker-c (T-1's first claim was released), T-2 with
    # worker-b.
    hold_t1 = state1.holder("jira", "T-1", "backend")
    hold_t2 = state1.holder("jira", "T-2", "qa")
    assert hold_t1 is not None and hold_t1.claimer_id == "worker-c"
    assert hold_t2 is not None and hold_t2.claimer_id == "worker-b"


def test_empty_fold_is_genesis(tmp_path: Path) -> None:
    state = project_claims([])
    assert isinstance(state, ClaimState)
    assert state.head.endswith("0" * 64)
    assert state.holder("jira", "T-1", "backend") is None


# ---------------------------------------------------------------------------
# Deterministic conflict rule -- lowest entry_hash wins
# ---------------------------------------------------------------------------


def test_lowest_entry_hash_wins_regardless_of_merge_order(tmp_path: Path) -> None:
    """Two concurrent claims resolve to the same holder in either order.

    Node A and node B each mint an independent claim for the same key. A
    merge that saw A-then-B and a merge that saw B-then-A must converge on
    the same winner (lowest ``entry_hash``), even though the journal head
    hash differs between the two orderings (that divergence is the fork
    surface deferred to a later phase).
    """
    journal_a = _journal(tmp_path, path_name="a.jsonl", node_id="node-a", seed=1)
    journal_b = _journal(tmp_path, path_name="b.jsonl", node_id="node-b", seed=2)
    claim_a = journal_a.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    claim_b = journal_b.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    assert claim_a.entry_hash != claim_b.entry_hash

    winner = min(claim_a, claim_b, key=lambda r: r.entry_hash)
    loser = max(claim_a, claim_b, key=lambda r: r.entry_hash)

    state_ab = project_claims([claim_a, claim_b])
    state_ba = project_claims([claim_b, claim_a])

    hold_ab = state_ab.holder("jira", "T-1", "backend")
    hold_ba = state_ba.holder("jira", "T-1", "backend")
    assert hold_ab is not None and hold_ba is not None
    assert hold_ab.entry_hash == hold_ba.entry_hash == winner.entry_hash
    assert hold_ab.claimer_id == winner.claimer_id
    assert loser.entry_hash in state_ab.superseded
    assert loser.entry_hash in state_ba.superseded


# ---------------------------------------------------------------------------
# Leaderless convergence -- chain-anchored supersede naming the winner
# ---------------------------------------------------------------------------


def test_reconcile_emits_chain_anchored_supersede_naming_winner(tmp_path: Path) -> None:
    """A double-claim converges via a signed, anchored supersede receipt."""
    chain = _chain(tmp_path)
    # One shared journal file; two node identities append to it (shared
    # workspace / shared filesystem -- the phase 1-2 substrate).
    journal_a = ClaimJournal(
        tmp_path / "claim_journal.jsonl",
        kms_adapter=_kms(tmp_path, seed=1, name="node-a-key"),
        node_id="node-a",
        chain=chain,
    )
    journal_b = ClaimJournal(
        tmp_path / "claim_journal.jsonl",
        kms_adapter=_kms(tmp_path, seed=2, name="node-b-key"),
        node_id="node-b",
        chain=chain,
    )
    claim_a = journal_a.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    claim_b = journal_b.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_100_000,
    )
    winner = min(claim_a, claim_b, key=lambda r: r.entry_hash)
    loser = max(claim_a, claim_b, key=lambda r: r.entry_hash)

    # Before reconciliation both claims are live in the fold.
    pre = journal_a.project()
    assert loser.entry_hash in pre.superseded  # fold already resolves the winner

    supersedes = journal_a.reconcile(ts_ns=1_000_200_000)
    assert len(supersedes) == 1
    receipt = supersedes[0]
    assert receipt.kind == "supersede"
    # The supersede is a statement BY the reconciling node (node-a) ABOUT the
    # loser's claim: its own identity names the reconciler that signs it, the
    # loser is carried as referenced data.
    assert receipt.node_id == "node-a"
    assert receipt.claimer_id == "node-a"
    assert receipt.supersedes == loser.entry_hash
    assert receipt.superseded_node_id == loser.node_id
    assert receipt.superseded_claimer_id == loser.claimer_id
    assert receipt.winner_claimer_id == winner.claimer_id
    assert receipt.winner_entry_hash == winner.entry_hash

    # The supersede receipt is anchored in the HMAC audit chain.
    rows = chain.query(event_type=EVENT_CLAIM_JOURNAL_RECEIPT)
    supersede_rows = [r for r in rows if r.details.get("kind") == "supersede"]
    assert len(supersede_rows) == 1
    assert supersede_rows[0].details["journal_entry_hash"] == receipt.entry_hash
    assert supersede_rows[0].details["node_id"] == "node-a"
    assert supersede_rows[0].details["winner_claimer_id"] == winner.claimer_id
    assert supersede_rows[0].details["superseded_claimer_id"] == loser.claimer_id
    assert "prev_chain_digest" in supersede_rows[0].details

    # Re-folding the journal (now carrying the supersede) leaves one holder,
    # and re-reconciling is a no-op (idempotent convergence).
    post = journal_a.project()
    hold = post.holder("jira", "T-1", "backend")
    assert hold is not None and hold.claimer_id == winner.claimer_id
    assert loser.entry_hash in post.superseded
    assert journal_a.reconcile(ts_ns=1_000_300_000) == []


def test_reconcile_supersede_is_attributed_to_its_signing_node(tmp_path: Path) -> None:
    """A reconcile-emitted supersede must be attributed to the node that signs it.

    A ``claim_superseded`` receipt is a statement *by* the reconciling node
    *about* the loser's claim -- so the ``node_id`` / ``claimer_id`` identity
    fields it carries and the Ed25519 signature that seals them must name the
    same node. If the receipt carried the loser's identity while being signed
    with the reconciler's install key, a verifier that pins each node's public
    key by ``node_id`` would reject the entry: the embedded JWK would not match
    the pinned key for the declared node. Here a *third* node (``node-c``)
    reconciles a ``node-a`` / ``node-b`` conflict, so the loser is always a
    different node than the signer -- exposing any mis-attribution unambiguously.
    """
    chain = _chain(tmp_path)
    kms_a = _kms(tmp_path, seed=1, name="node-a-key")
    kms_b = _kms(tmp_path, seed=2, name="node-b-key")
    kms_c = _kms(tmp_path, seed=3, name="node-c-key")
    journal_a = ClaimJournal(tmp_path / "claim_journal.jsonl", kms_adapter=kms_a, node_id="node-a", chain=chain)
    journal_b = ClaimJournal(tmp_path / "claim_journal.jsonl", kms_adapter=kms_b, node_id="node-b", chain=chain)
    journal_c = ClaimJournal(tmp_path / "claim_journal.jsonl", kms_adapter=kms_c, node_id="node-c", chain=chain)

    claim_a = journal_a.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    claim_b = journal_b.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-b",
        lease_expires_at=1600.0,
        ts_ns=1_000_100_000,
    )
    winner = min(claim_a, claim_b, key=lambda r: r.entry_hash)
    loser = max(claim_a, claim_b, key=lambda r: r.entry_hash)

    emitted = journal_c.reconcile(ts_ns=1_000_200_000)
    assert len(emitted) == 1
    supersede = emitted[0]

    # The receipt is a statement BY node-c: its own identity fields name node-c,
    # the actual signer -- never the loser it speaks about.
    assert supersede.node_id == "node-c"
    assert supersede.node_id != loser.node_id

    # The embedded signing JWK belongs to node-c, matching the declared node_id:
    # signature and attribution agree.
    assert supersede.signature["public_key_jwk"] == kms_c.public_key_jwk()

    # A verifier pinning each node's key by node_id accepts the whole journal:
    # every receipt's signature matches the identity the receipt claims to be
    # from. This is the attribution invariant the mis-signed supersede broke.
    trusted_keys = {
        "node-a": kms_a.public_key_jwk(),
        "node-b": kms_b.public_key_jwk(),
        "node-c": kms_c.public_key_jwk(),
    }
    result = journal_c.verify(trusted_keys=trusted_keys)
    assert result.ok, result.failures

    # The loser's identity survives as *referenced data* (what the receipt is
    # about), not as the receipt's own identity (who it is from).
    assert supersede.supersedes == loser.entry_hash
    assert supersede.superseded_node_id == loser.node_id
    assert supersede.superseded_claimer_id == loser.claimer_id
    # The winner naming is unchanged: lowest-entry-hash winner rule intact.
    assert supersede.winner_entry_hash == winner.entry_hash
    assert supersede.winner_claimer_id == winner.claimer_id


def test_reconcile_is_noop_without_conflict(tmp_path: Path) -> None:
    journal = _journal(tmp_path, chain=_chain(tmp_path))
    journal.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )
    assert journal.reconcile(ts_ns=1_000_100_000) == []


# ---------------------------------------------------------------------------
# Concurrent multi-writer safety on a shared journal (the phase 1-2 target)
# ---------------------------------------------------------------------------


def _shared_journal(tmp_path: Path, *, node_id: str, seed: int, chain: AuditChainStore | None = None) -> ClaimJournal:
    """A journal for ``node_id`` bound to the single shared ``shared.jsonl``."""
    return ClaimJournal(
        tmp_path / "shared.jsonl",
        kms_adapter=_kms(tmp_path, seed=seed, name=f"{node_id}-key"),
        node_id=node_id,
        chain=chain,
    )


def test_concurrent_shared_journal_appends_stay_linear(tmp_path: Path) -> None:
    """Two nodes appending to one shared journal never fork the chain.

    Two :class:`ClaimJournal` instances (distinct install identities, own
    process-local locks) hammer the same on-disk file from two threads,
    released together by a barrier so their appends genuinely overlap -- the
    literal shared-filesystem multi-writer shape phase 1-2 targets. The
    read-modify-write of ``prev_entry_hash`` must be serialised by the
    exclusive file lock; if the tail were read outside that lock both writers
    could link to the same predecessor and fork the linear chain, which
    offline :meth:`ClaimJournal.verify` cannot tell apart from tampering.
    """
    journal_a = _shared_journal(tmp_path, node_id="node-a", seed=1)
    journal_b = _shared_journal(tmp_path, node_id="node-b", seed=2)

    rounds = 24
    barrier = threading.Barrier(2)

    def hammer(journal: ClaimJournal, worker: str, base_ticket: int) -> None:
        for i in range(rounds):
            barrier.wait()
            journal.append(
                kind="claim",
                tracker="jira",
                ticket_id=f"T-{base_ticket + i}",
                role="backend",
                claimer_id=worker,
                lease_expires_at=1600.0,
                ts_ns=1_000_000_000 + i,
            )

    ta = threading.Thread(target=hammer, args=(journal_a, "worker-a", 0))
    tb = threading.Thread(target=hammer, args=(journal_b, "worker-b", 1000))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    # No fork: every receipt sits on one linear, offline-verifiable chain.
    result = journal_a.verify()
    assert result.ok, result.failures
    assert result.entry_count == 2 * rounds


def test_two_nodes_concurrently_claim_same_key_converge(tmp_path: Path) -> None:
    """Issue criterion 3: two nodes *concurrently* self-claim one key.

    Both nodes race an honest claim for the same ``(tracker, ticket_id,
    role)`` against the shared journal at the same instant. The chain must
    stay linear (both claims land, neither forks), the deterministic fold
    must already resolve a single winner, and ``reconcile`` must emit exactly
    one chain-anchored ``supersede`` naming that winner.
    """
    chain = _chain(tmp_path)
    journal_a = _shared_journal(tmp_path, node_id="node-a", seed=1, chain=chain)
    journal_b = _shared_journal(tmp_path, node_id="node-b", seed=2, chain=chain)

    barrier = threading.Barrier(2)
    minted: dict[str, ClaimReceipt] = {}

    def claim(journal: ClaimJournal, worker: str) -> None:
        barrier.wait()
        minted[worker] = journal.append(
            kind="claim",
            tracker="jira",
            ticket_id="T-1",
            role="backend",
            claimer_id=worker,
            lease_expires_at=1600.0,
            ts_ns=1_000_000_000,
        )

    ta = threading.Thread(target=claim, args=(journal_a, "worker-a"))
    tb = threading.Thread(target=claim, args=(journal_b, "worker-b"))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    result = journal_a.verify()
    assert result.ok, result.failures
    assert result.entry_count == 2

    winner = min(minted.values(), key=lambda r: r.entry_hash)
    loser = max(minted.values(), key=lambda r: r.entry_hash)

    pre = journal_a.project()
    hold = pre.holder("jira", "T-1", "backend")
    assert hold is not None and hold.entry_hash == winner.entry_hash
    assert loser.entry_hash in pre.superseded

    supersedes = journal_a.reconcile(ts_ns=2_000_000_000)
    assert len(supersedes) == 1
    assert supersedes[0].supersedes == loser.entry_hash
    assert supersedes[0].winner_entry_hash == winner.entry_hash
    # Convergence survives the extra supersede receipt and stays linear.
    assert journal_a.verify().ok
    post = journal_a.project().holder("jira", "T-1", "backend")
    assert post is not None and post.claimer_id == winner.claimer_id


def test_append_resolves_tail_under_the_write_lock(tmp_path: Path) -> None:
    """The tail read and the byte-append are one lock-guarded critical section.

    Deterministic proof (no scheduling luck): while another writer holds the
    exclusive lock and commits a fresh entry before releasing, a concurrent
    append must observe that new entry as its predecessor. If ``append`` read
    the tail *before* taking the lock it would link to the stale tail and fork
    the chain. We gate the interleaving with the file lock itself.
    """
    fcntl = pytest.importorskip("fcntl")

    journal_a = _shared_journal(tmp_path, node_id="node-a", seed=1)
    journal_b = _shared_journal(tmp_path, node_id="node-b", seed=2)

    # One committed entry so the tail is a real hash, not genesis.
    r1 = journal_a.append(
        kind="claim",
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        lease_expires_at=1600.0,
        ts_ns=1_000_000_000,
    )

    started = threading.Event()
    done = threading.Event()
    captured: dict[str, ClaimReceipt] = {}

    def append_b() -> None:
        started.set()
        captured["b"] = journal_b.append(
            kind="claim",
            tracker="jira",
            ticket_id="T-2",
            role="backend",
            claimer_id="worker-b",
            lease_expires_at=1700.0,
            ts_ns=1_002_000_000,
        )
        done.set()

    holder = (tmp_path / "shared.jsonl").open("a+b")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        thread = threading.Thread(target=append_b)
        thread.start()
        started.wait(timeout=2.0)
        # Give B time to reach the lock; a correct append is now blocked
        # *before* reading the tail, a buggy one has already read tail == r1.
        time.sleep(0.25)

        # A third node commits an entry (r_mid) while we hold the lock, moving
        # the true tail forward. Build it with the production helpers so it is a
        # fully valid, signed, chain-linked receipt.
        kms_c = _kms(tmp_path, seed=3, name="node-c-key")
        unsigned = ClaimReceipt(
            schema_version=CLAIM_JOURNAL_SCHEMA_VERSION,
            kind="claim",
            ts_ns=1_001_000_000,
            tracker="jira",
            ticket_id="T-9",
            role="backend",
            claimer_id="worker-c",
            node_id="node-c",
            lease_expires_at=1650.0,
            prev_entry_hash=r1.entry_hash,
            entry_hash=GENESIS_PREV_HASH,
            signature={},
        )
        digest = compute_claim_entry_hash(unsigned)
        signed = replace(unsigned, entry_hash=digest)
        signature = build_head_signature(digest.split(":", 1)[1], kms_adapter=kms_c)
        r_mid = replace(signed, signature=signature)
        holder.write(_canonical_bytes(asdict(r_mid)) + b"\n")
        holder.flush()

        assert not done.wait(timeout=0.2)  # B is still blocked on the lock
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    finally:
        holder.close()

    assert done.wait(timeout=3.0)
    thread.join(timeout=3.0)

    # B linked to r_mid (the tail it saw *under* the lock), not the stale r1.
    assert captured["b"].prev_entry_hash == r_mid.entry_hash
    # The whole chain -- r1 -> r_mid -> r_b -- stays linear and verifiable.
    assert journal_a.verify().ok


# ---------------------------------------------------------------------------
# Verifiability / tamper-evidence (offline replay)
# ---------------------------------------------------------------------------


def test_verify_flags_flipped_byte_at_exact_index(tmp_path: Path) -> None:
    """A single flipped byte fails verification at the offending entry."""
    journal = _journal(tmp_path)
    for i in range(3):
        journal.append(
            kind="claim",
            tracker="jira",
            ticket_id=f"T-{i}",
            role="backend",
            claimer_id=f"worker-{i}",
            lease_expires_at=1600.0 + i,
            ts_ns=1_000_000_000 + i,
        )
    assert journal.verify().ok

    # Corrupt the second on-disk record's claimer_id.
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    assert "worker-1" in lines[1]
    lines[1] = lines[1].replace("worker-1", "worker-X")
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = journal.verify()
    assert not result.ok
    assert result.bad_index == 1
    assert result.failures


def test_verify_detects_broken_chain_link(tmp_path: Path) -> None:
    """Dropping a middle receipt breaks the prev_entry_hash linkage."""
    journal = _journal(tmp_path)
    for i in range(3):
        journal.append(
            kind="claim",
            tracker="jira",
            ticket_id=f"T-{i}",
            role="backend",
            claimer_id=f"worker-{i}",
            lease_expires_at=1600.0,
            ts_ns=1_000_000_000 + i,
        )
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    # Delete the middle entry: entry 2's prev no longer matches entry 0.
    del lines[1]
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = journal.verify()
    assert not result.ok
    assert result.bad_index == 1


# ---------------------------------------------------------------------------
# ClaimLedger opt-in journal path (STAR untouched)
# ---------------------------------------------------------------------------


def test_ledger_without_journal_writes_no_receipts(tmp_path: Path) -> None:
    """The default STAR ledger path must not touch a journal."""
    ledger = ClaimLedger(tmp_path / "claims.db")
    assert ledger.journal is None
    outcome = ledger.try_claim(
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        ttl_seconds=600,
        per_role_max_in_flight=1,
        now=1000.0,
    )
    assert outcome.granted
    assert not (tmp_path / "claim_journal.jsonl").exists()


def test_ledger_journal_path_appends_receipt_and_materialises_row(tmp_path: Path) -> None:
    """On the opt-in journal path a grant appends a signed claim receipt."""
    chain = _chain(tmp_path)
    journal = _journal(tmp_path, chain=chain)
    ledger = ClaimLedger(tmp_path / "claims.db", journal=journal)
    assert ledger.journal is journal

    outcome = ledger.try_claim(
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        ttl_seconds=600,
        per_role_max_in_flight=1,
        now=1000.0,
    )
    assert outcome.granted
    assert outcome.lease_expires_at == 1600.0

    receipts = journal.read()
    assert len(receipts) == 1
    assert receipts[0].kind == "claim"
    assert receipts[0].claimer_id == "worker-a"
    assert receipts[0].lease_expires_at == 1600.0

    # The SQLite projection reflects the same holder.
    live = ledger.live_claims(now=1000.0)
    assert len(live) == 1
    assert live[0]["claimer_id"] == "worker-a"

    # The receipt is anchored in the audit chain.
    rows = chain.query(event_type=EVENT_CLAIM_JOURNAL_RECEIPT)
    assert len(rows) == 1
    assert rows[0].details["kind"] == "claim"

    # The fold over the journal agrees with the SQLite row.
    hold = journal.project().holder("jira", "T-1", "backend")
    assert hold is not None and hold.claimer_id == "worker-a"


def test_ledger_journal_replay_is_deterministic(tmp_path: Path) -> None:
    """The injected ``now`` clock makes the claim receipt replay-stable."""
    journal1 = _journal(tmp_path, path_name="j1.jsonl", node_id="node-a", seed=1)
    journal2 = _journal(tmp_path, path_name="j2.jsonl", node_id="node-a", seed=1)
    ledger1 = ClaimLedger(tmp_path / "c1.db", journal=journal1)
    ledger2 = ClaimLedger(tmp_path / "c2.db", journal=journal2)

    ledger1.try_claim(
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        ttl_seconds=600,
        per_role_max_in_flight=1,
        now=1000.0,
    )
    ledger2.try_claim(
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        ttl_seconds=600,
        per_role_max_in_flight=1,
        now=1000.0,
    )
    r1 = journal1.read()[0]
    r2 = journal2.read()[0]
    # Same node identity + same injected clock + same inputs -> byte-identical
    # receipt, entry_hash, and signature.
    assert r1.to_dict() == r2.to_dict()
    assert r1.entry_hash == r2.entry_hash


def test_journal_is_source_of_truth_no_receiptless_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant that cannot mint a receipt must not leave a held SQLite row.

    The journal is the source of truth and the ledger is its projection: a
    committed claim row with no backing receipt is a phantom holder the fold
    can never explain. When the receipt append fails (disk / signing error),
    ``try_claim`` must roll the SQLite transaction back so the claim is *not*
    held -- caller-sees-failure and ledger-does-not-hold must agree.
    """
    chain = _chain(tmp_path)
    journal = _journal(tmp_path, chain=chain)
    ledger = ClaimLedger(tmp_path / "claims.db", journal=journal)

    def boom(*_args: object, **_kwargs: object) -> ClaimReceipt:
        raise RuntimeError("disk full while signing the claim receipt")

    monkeypatch.setattr(journal, "append", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        ledger.try_claim(
            tracker="jira",
            ticket_id="T-1",
            role="backend",
            claimer_id="worker-a",
            ttl_seconds=600,
            per_role_max_in_flight=1,
            now=1000.0,
        )

    # No phantom: the ledger holds nothing and no receipt was recorded.
    assert ledger.live_claims(now=1000.0) == []
    assert journal.read() == []
    assert chain.query(event_type=EVENT_CLAIM_JOURNAL_RECEIPT) == []

    # The rollback left the connection usable: an honest claim still lands, and
    # now the row is backed by exactly one receipt (materialised from it).
    monkeypatch.undo()
    outcome = ledger.try_claim(
        tracker="jira",
        ticket_id="T-1",
        role="backend",
        claimer_id="worker-a",
        ttl_seconds=600,
        per_role_max_in_flight=1,
        now=1000.0,
    )
    assert outcome.granted
    receipts = journal.read()
    assert len(receipts) == 1
    live = ledger.live_claims(now=1000.0)
    assert len(live) == 1
    assert live[0]["claimer_id"] == receipts[0].claimer_id == "worker-a"
    assert receipts[0].lease_expires_at == outcome.lease_expires_at


def test_reject_unknown_receipt_kind(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(ValueError, match="unknown claim-receipt kind"):
        journal.append(
            kind="bogus",
            tracker="jira",
            ticket_id="T-1",
            role="backend",
            claimer_id="worker-a",
            lease_expires_at=1600.0,
            ts_ns=1,
        )
