"""Tests for the sealed receipt of a fan-out's RunGraph (#3759).

A run-graph receipt anchors an entire fan-out's RunGraph into a single signed
artifact that binds together all N branches, solving the fan-out receipt problem.
It mirrors the pattern from ``build_verdict_receipt``:

* anchors into a dedicated lineage spine run (``RUN_GRAPH_RUN_ID``),
* records a mirror in the HMAC audit chain (``EVENT_RUN_GRAPH_SEALED``), and
* signs the canonical bytes as a detached JWS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.run_graph import (
    RUN_GRAPH_RUN_ID,
    RunGraphNode,
    RunGraphNodeStatus,
    RunGraphReceipt,
    RunGraphVerifyResult,
    _node_hash,
    build_run_graph,
    build_run_graph_receipt,
    verify_run_graph_receipt,
)
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair
from bernstein.core.security.audit_chain import (
    EVENT_RUN_GRAPH_SEALED,
    AuditChainStore,
)

HMAC_KEY = b"k" * 32

# Fixed timestamp for deterministic tests
TIMESTAMP = 1_700_000_000

SESSIONS = ("sess-alpha", "sess-beta", "sess-gamma")
HEAD_SHAS = {
    "sess-alpha": "a" * 40,
    "sess-beta": "b" * 40,
    "sess-gamma": "c" * 40,
}
RUN_IDS = {session: f"run-{session.split('-')[1]}" for session in SESSIONS}


@pytest.fixture
def fanout(tmp_path: Path) -> tuple[Path, Path]:
    """Three worktree-shaped sessions, each with a spine holding one write.

    Returns ``(repo_root, lineage_root)``.
    """
    from bernstein.core.lineage.spine import LineageSpine

    repo_root = tmp_path / "repo"
    worktrees = repo_root / ".sdd" / "runtime" / "worktrees"
    worktrees.mkdir(parents=True)
    lineage_root = tmp_path / "lineage"

    for session in SESSIONS:
        (worktrees / session).mkdir()
        spine = LineageSpine(lineage_root, run_id=RUN_IDS[session], hmac_key=HMAC_KEY)
        spine.record(
            artifact_path=f"out/{session}.txt",
            content=f"written by {session}".encode(),
            actor="tester",
            step_id="step-1",
            model="test-model",
            timestamp=TIMESTAMP,
        )
    return repo_root, lineage_root


@pytest.fixture
def private_key_pem() -> bytes:
    """Generate a fixed Ed25519 keypair for deterministic tests."""
    private, _public = generate_ed25519_keypair()
    return private


# ---------------------------------------------------------------------------
# Dataclass structure tests
# ---------------------------------------------------------------------------


def test_run_graph_receipt_has_all_required_fields() -> None:
    """Verify the RunGraphReceipt dataclass has all required fields."""
    receipt = RunGraphReceipt(
        schema_version=1,
        graph_root_hash="sha256:" + "a" * 64,
        node_hashes=("sha256:node1", "sha256:node2"),
        timestamp=TIMESTAMP,
        receipt_hash="",
    )
    assert receipt.schema_version == 1
    assert receipt.graph_root_hash.startswith("sha256:")
    assert len(receipt.node_hashes) == 2
    assert receipt.timestamp == TIMESTAMP
    assert receipt.journal_entry_hash == ""


def test_run_graph_receipt_body_excludes_hash_and_anchor() -> None:
    """Test that body() returns correct fields, excluding receipt_hash and anchor."""
    receipt = RunGraphReceipt(
        schema_version=1,
        graph_root_hash="sha256:abc",
        node_hashes=("sha256:node1", "sha256:node2"),
        timestamp=TIMESTAMP,
        receipt_hash="",
    )
    body = receipt.body()
    assert body["schema_version"] == 1
    assert body["graph_root_hash"] == "sha256:abc"
    assert body["node_hashes"] == ["sha256:node1", "sha256:node2"]
    assert body["timestamp"] == TIMESTAMP
    assert "receipt_hash" not in body
    assert "journal_entry_hash" not in body


def test_run_graph_receipt_to_dict_roundtrip() -> None:
    """Test serialization roundtrip preserves all fields."""
    receipt = RunGraphReceipt(
        schema_version=1,
        graph_root_hash="sha256:def",
        node_hashes=("sha256:node1",),
        timestamp=TIMESTAMP,
        receipt_hash="sha256:xyz",
        journal_entry_hash="sha256:journal123",
    )
    as_dict = receipt.to_dict()
    parsed = RunGraphReceipt.from_dict(as_dict)
    assert parsed.schema_version == receipt.schema_version
    assert parsed.graph_root_hash == receipt.graph_root_hash
    assert parsed.node_hashes == receipt.node_hashes
    assert parsed.timestamp == receipt.timestamp
    assert parsed.receipt_hash == receipt.receipt_hash
    assert parsed.journal_entry_hash == receipt.journal_entry_hash


def test_run_graph_receipt_canonical_bytes_exclude_anchor() -> None:
    """Test canonical bytes exclude the anchor for cross-machine equality."""
    receipt = RunGraphReceipt(
        schema_version=1,
        graph_root_hash="sha256:abc",
        node_hashes=("sha256:node1",),
        timestamp=TIMESTAMP,
        receipt_hash="sha256:xyz",
    )
    payload = receipt.canonical_payload_without_anchor()
    parsed = json.loads(payload)
    assert "journal_entry_hash" not in parsed
    assert parsed["receipt_hash"] == "sha256:xyz"


def test_run_graph_receipt_canonical_bytes_deterministic() -> None:
    """Test canonical bytes are deterministic for identical receipts."""
    receipt = RunGraphReceipt(
        schema_version=1,
        graph_root_hash="sha256:abc",
        node_hashes=("sha256:node1",),
        timestamp=TIMESTAMP,
        receipt_hash="sha256:xyz",
    )
    bytes1 = receipt.canonical_bytes()
    bytes2 = receipt.canonical_bytes()
    assert bytes1 == bytes2


# ---------------------------------------------------------------------------
# Node hash computation tests
# ---------------------------------------------------------------------------


def test_node_hash_deterministic_for_same_node() -> None:
    """Test that _node_hash is deterministic for identical nodes."""
    node1 = RunGraphNode(
        session_id="sess-1",
        head_sha="a" * 40,
        run_id="run-1",
        spine_head_hash="b" * 64,
        status=RunGraphNodeStatus.RESOLVED,
    )
    node2 = RunGraphNode(
        session_id="sess-1",
        head_sha="a" * 40,
        run_id="run-1",
        spine_head_hash="b" * 64,
        status=RunGraphNodeStatus.RESOLVED,
    )
    hash1 = _node_hash(node1)
    hash2 = _node_hash(node2)
    assert hash1 == hash2


def test_node_hash_differs_for_different_nodes() -> None:
    """Test that different nodes produce different hashes."""
    node1 = RunGraphNode(
        session_id="sess-1",
        head_sha="a" * 40,
        run_id="run-1",
        spine_head_hash="b" * 64,
        status=RunGraphNodeStatus.RESOLVED,
    )
    node2 = RunGraphNode(
        session_id="sess-2",  # Different session
        head_sha="a" * 40,
        run_id="run-1",
        spine_head_hash="b" * 64,
        status=RunGraphNodeStatus.RESOLVED,
    )
    assert _node_hash(node1) != _node_hash(node2)


def test_node_hash_handles_unresolved_node() -> None:
    """Test that unresolved nodes produce valid hashes."""
    node = RunGraphNode(
        session_id="sess-1",
        head_sha="a" * 40,
        run_id=None,
        spine_head_hash=None,
        status=RunGraphNodeStatus.UNRESOLVED,
    )
    hash_val = _node_hash(node)
    assert hash_val.startswith("sha256:")


def test_node_hash_includes_session_id() -> None:
    """Test that session_id is part of the hash pre-image."""
    node1 = RunGraphNode(
        session_id="sess-1",
        head_sha="a" * 40,
        run_id="run-1",
        spine_head_hash="b" * 64,
        status=RunGraphNodeStatus.RESOLVED,
    )
    node2 = RunGraphNode(
        session_id="sess-2",  # Different session
        head_sha="a" * 40,
        run_id="run-1",
        spine_head_hash="b" * 64,
        status=RunGraphNodeStatus.RESOLVED,
    )
    assert _node_hash(node1) != _node_hash(node2)


# ---------------------------------------------------------------------------
# build_run_graph_receipt integration tests
# ---------------------------------------------------------------------------


def test_build_run_graph_receipt_basic(fanout: tuple[Path, Path], private_key_pem: bytes) -> None:
    """Test basic functionality of build_run_graph_receipt."""
    repo_root, lineage_root = fanout

    graph = build_run_graph(
        repo_root,
        run_ids=dict(RUN_IDS),
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )

    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )

    # Verify receipt structure
    assert receipt.schema_version == 1
    assert receipt.graph_root_hash == graph.root_hash
    assert len(receipt.node_hashes) == 3
    assert receipt.timestamp == TIMESTAMP
    assert receipt.receipt_hash.startswith("sha256:")
    assert receipt.journal_entry_hash.startswith("sha256:")

    # Verify on-disk receipt
    receipt_dir = repo_root / ".sdd" / "run-graph"
    assert receipt_dir.exists()
    assert len(list(receipt_dir.glob("*.json"))) == 1

    # Verify receipt contents
    receipt_file = next(iter(receipt_dir.glob("*.json")))
    data = json.loads(receipt_file.read_text())
    assert data["receipt_hash"] == receipt.receipt_hash
    assert data["graph_root_hash"] == graph.root_hash
    assert data["schema_version"] == 1
    assert "signed_jws" in data


def test_build_run_graph_receipt_with_audit_chain(fanout: tuple[Path, Path], private_key_pem: bytes) -> None:
    """Test audit chain mirroring."""
    repo_root, lineage_root = fanout
    chain = AuditChainStore(repo_root / "audit", key=HMAC_KEY)

    graph = build_run_graph(
        repo_root,
        run_ids=dict(RUN_IDS),
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )

    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=chain,
    )

    # Verify chain event
    events = chain.query(event_type=EVENT_RUN_GRAPH_SEALED)
    assert len(events) == 1
    event = events[0]
    assert event.resource_id == receipt.receipt_hash
    assert event.details["graph_root_hash"] == graph.root_hash
    assert event.details["receipt_hash"] == receipt.receipt_hash
    assert event.details["journal_entry_hash"] == receipt.journal_entry_hash

    # Verify chain integrity
    ok, errors = chain.verify()
    assert ok, errors


def test_build_run_graph_receipt_determinism(fanout: tuple[Path, Path], private_key_pem: bytes) -> None:
    """Test that identical inputs produce byte-identical receipts."""
    repo_root, lineage_root = fanout

    graph = build_run_graph(
        repo_root,
        run_ids=dict(RUN_IDS),
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )

    # Build receipt twice on separate workdirs
    receipt_a = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root / "a",
        lineage_root=lineage_root / "a",
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )

    receipt_b = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root / "b",
        lineage_root=lineage_root / "b",
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )

    # Receipt hashes must match
    assert receipt_a.receipt_hash == receipt_b.receipt_hash
    # Canonical bytes must match
    assert receipt_a.canonical_payload_without_anchor() == receipt_b.canonical_payload_without_anchor()
    # Spine anchors must match (same content, same HMAC key)
    assert receipt_a.journal_entry_hash == receipt_b.journal_entry_hash


def test_build_run_graph_receipt_includes_all_nodes(fanout: tuple[Path, Path], private_key_pem: bytes) -> None:
    """Test that receipt includes hashes for all nodes including unresolved."""
    repo_root, lineage_root = fanout

    # Partial run_ids (sess-beta is unresolved)
    partial_run_ids = {k: v for k, v in RUN_IDS.items() if k != "sess-beta"}

    graph = build_run_graph(
        repo_root,
        run_ids=partial_run_ids,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )

    # Verify graph has 3 nodes
    assert len(graph.nodes) == 3

    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )

    # Receipt should have 3 node hashes
    assert len(receipt.node_hashes) == 3


def test_build_run_graph_receipt_anchors_under_dedicated_run_id(
    fanout: tuple[Path, Path], private_key_pem: bytes
) -> None:
    """Test that receipt anchors under RUN_GRAPH_RUN_ID, not per-task run ids."""
    repo_root, lineage_root = fanout
    from bernstein.core.lineage.spine import LineageSpine

    graph = build_run_graph(
        repo_root,
        run_ids=dict(RUN_IDS),
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )

    # Record something under a per-task run id (should not be affected)
    task_spine = LineageSpine(lineage_root, run_id="run-alpha", hmac_key=HMAC_KEY)
    task_spine.record(
        artifact_path="task-artifact.txt",
        content=b"task artifact",
        actor="test",
        step_id="step-task",
        model="test",
        timestamp=TIMESTAMP,
    )
    task_head_before = task_spine.head_hash()

    # Build the run-graph receipt
    build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )

    # Per-task spine should be unchanged
    task_head_after = task_spine.head_hash()
    assert task_head_before == task_head_after

    # Dedicated run-graph spine should exist
    graph_spine = LineageSpine(lineage_root, run_id=RUN_GRAPH_RUN_ID, hmac_key=HMAC_KEY)
    assert graph_spine.head_hash() != ""


def test_build_run_graph_receipt_is_signed(fanout: tuple[Path, Path], private_key_pem: bytes) -> None:
    """Test that receipt is signed with Ed25519 JWS."""
    repo_root, lineage_root = fanout

    graph = build_run_graph(
        repo_root,
        run_ids=dict(RUN_IDS),
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )

    build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )

    # Read the on-disk receipt and verify signature
    receipt_dir = repo_root / ".sdd" / "run-graph"
    receipt_file = next(iter(receipt_dir.glob("*.json")))
    data = json.loads(receipt_file.read_text())

    assert "signed_jws" in data
    signed_jws = data["signed_jws"]
    # Should be a valid JWS format: header.payload.signature (payload is empty for detached)
    parts = signed_jws.split(".")
    assert len(parts) == 3
    # Second part should be empty for detached signature
    assert parts[1] == ""


def test_build_run_graph_receipt_graph_root_hash_matches(fanout: tuple[Path, Path], private_key_pem: bytes) -> None:
    """Test that the sealed graph root hash matches the input graph."""
    repo_root, lineage_root = fanout

    graph = build_run_graph(
        repo_root,
        run_ids=dict(RUN_IDS),
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )

    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )

    assert receipt.graph_root_hash == graph.root_hash


def test_run_graph_receipt_has_correct_run_id_constant() -> None:
    """Test RUN_GRAPH_RUN_ID constant exists and has expected value."""
    assert RUN_GRAPH_RUN_ID == "run-graph"
    assert RUN_GRAPH_RUN_ID != "eval-gate"  # Different from gate receipts


# ---------------------------------------------------------------------------
# verify_run_graph_receipt (#3760)
# ---------------------------------------------------------------------------
#
# A receipt that only replays its own stored fields proves nothing: every hash
# inside it was written by the pass that wrote the fields. These tests damage
# the tree and the receipt in turn, and assert the verifier notices -- which is
# the only way to tell re-derivation from a self-consistency check.


def _seal(
    repo_root: Path,
    lineage_root: Path,
    private_key_pem: bytes,
    *,
    sessions: tuple[str, ...] = SESSIONS,
) -> tuple[RunGraphReceipt, Path]:
    """Seal the fan-out and return the receipt with its on-disk path."""
    graph = build_run_graph(
        repo_root,
        run_ids={s: RUN_IDS[s] for s in sessions},
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )
    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )
    return receipt, repo_root / ".sdd" / "run-graph" / f"{receipt.receipt_hash}.json"


def _verify(path: Path, repo_root: Path, lineage_root: Path, public_key_pem: bytes) -> RunGraphVerifyResult:
    return verify_run_graph_receipt(
        receipt_path=path,
        repo_root=repo_root,
        run_ids=dict(RUN_IDS),
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        public_key_pem=public_key_pem,
        head_sha_resolver=lambda p: HEAD_SHAS.get(p.name),
    )


@pytest.fixture
def keypair() -> tuple[bytes, bytes]:
    private, public = generate_ed25519_keypair()
    return private, public


def test_a_sealed_receipt_verifies_against_the_tree_it_sealed(
    fanout: tuple[Path, Path], keypair: tuple[bytes, bytes]
) -> None:
    repo_root, lineage_root = fanout
    private, public = keypair
    _receipt, path = _seal(repo_root, lineage_root, private)

    result = _verify(path, repo_root, lineage_root, public)

    assert result.ok is True
    assert result.status == "verified"
    assert "3 branch" in result.reason


def test_a_flipped_byte_in_a_branch_spine_names_that_branch(
    fanout: tuple[Path, Path], keypair: tuple[bytes, bytes]
) -> None:
    """The assertion #3760 exists for: damage the tree, not the receipt.

    Nothing inside the receipt changes here. Only re-derivation can see it,
    and the message has to name the branch or an operator has N places to look.
    """
    repo_root, lineage_root = fanout
    private, public = keypair
    _receipt, path = _seal(repo_root, lineage_root, private)

    spine_file = next((lineage_root / RUN_IDS["sess-beta"]).rglob("*.jsonl"))
    raw = spine_file.read_bytes()
    assert b'"actor":"tester"' in raw
    spine_file.write_bytes(raw.replace(b'"actor":"tester"', b'"actor":"testeR"'))

    result = _verify(path, repo_root, lineage_root, public)

    assert result.ok is False
    assert result.status == "diverged"
    assert "sess-beta" in result.reason


def test_a_branch_that_disappeared_is_reported_as_a_count(
    fanout: tuple[Path, Path], keypair: tuple[bytes, bytes]
) -> None:
    """With a branch gone there is no pairing to read a name out of."""
    repo_root, lineage_root = fanout
    private, public = keypair
    _receipt, path = _seal(repo_root, lineage_root, private)

    import shutil

    shutil.rmtree(repo_root / ".sdd" / "runtime" / "worktrees" / "sess-gamma")

    result = _verify(path, repo_root, lineage_root, public)

    assert result.ok is False
    assert result.status == "diverged"
    assert "3 branch(es)" in result.reason
    assert "2" in result.reason


def test_an_edited_receipt_body_fails_before_any_re_derivation(
    fanout: tuple[Path, Path], keypair: tuple[bytes, bytes]
) -> None:
    repo_root, lineage_root = fanout
    private, public = keypair
    _receipt, path = _seal(repo_root, lineage_root, private)

    data = json.loads(path.read_text())
    data["timestamp"] = TIMESTAMP + 1
    path.write_text(json.dumps(data))

    result = _verify(path, repo_root, lineage_root, public)

    assert result.ok is False
    assert result.status == "tampered"
    assert "receipt_hash" in result.reason


def test_a_receipt_signed_by_another_key_is_refused(fanout: tuple[Path, Path], keypair: tuple[bytes, bytes]) -> None:
    """The body is intact and self-consistent; only the signer is wrong."""
    repo_root, lineage_root = fanout
    private, _public = keypair
    _receipt, path = _seal(repo_root, lineage_root, private)
    _other_private, other_public = generate_ed25519_keypair()

    result = _verify(path, repo_root, lineage_root, other_public)

    assert result.ok is False
    assert result.status == "tampered"
    assert "signature" in result.reason


def test_a_receipt_stripped_of_its_signature_is_refused(
    fanout: tuple[Path, Path], keypair: tuple[bytes, bytes]
) -> None:
    """Absent evidence must not read as satisfied evidence."""
    repo_root, lineage_root = fanout
    private, public = keypair
    _receipt, path = _seal(repo_root, lineage_root, private)

    data = json.loads(path.read_text())
    del data["signed_jws"]
    path.write_text(json.dumps(data))

    result = _verify(path, repo_root, lineage_root, public)

    assert result.ok is False
    assert result.status == "unreadable"


def test_an_empty_fan_out_is_refused_rather_than_passing_on_nothing(
    tmp_path: Path, keypair: tuple[bytes, bytes]
) -> None:
    """Zero branches means zero disagreements, which is not the same as agreement."""
    private, public = keypair
    repo_root = tmp_path / "repo"
    (repo_root / ".sdd" / "runtime" / "worktrees").mkdir(parents=True)
    lineage_root = tmp_path / "lineage"

    graph = build_run_graph(
        repo_root,
        run_ids={},
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        head_sha_resolver=lambda p: None,
    )
    assert graph.nodes == ()
    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=repo_root,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        timestamp=TIMESTAMP,
        private_key_pem=private,
        chain=None,
    )
    path = repo_root / ".sdd" / "run-graph" / f"{receipt.receipt_hash}.json"

    result = verify_run_graph_receipt(
        receipt_path=path,
        repo_root=repo_root,
        run_ids={},
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        public_key_pem=public,
        head_sha_resolver=lambda p: None,
    )

    assert result.ok is False
    assert result.status == "empty"


def test_a_receipt_whose_spine_anchor_is_gone_is_refused(
    fanout: tuple[Path, Path], keypair: tuple[bytes, bytes]
) -> None:
    """The receipt and the tree agree; only the anchor binding them is missing."""
    repo_root, lineage_root = fanout
    private, public = keypair
    _receipt, path = _seal(repo_root, lineage_root, private)

    anchor_spine = next((lineage_root / RUN_GRAPH_RUN_ID).rglob("*.jsonl"))
    anchor_spine.write_bytes(b"")

    result = _verify(path, repo_root, lineage_root, public)

    assert result.ok is False
    assert result.status == "unanchored"


def test_an_unreadable_receipt_is_refused_rather_than_skipped(tmp_path: Path, keypair: tuple[bytes, bytes]) -> None:
    _private, public = keypair
    missing = tmp_path / "nope.json"

    result = verify_run_graph_receipt(
        receipt_path=missing,
        repo_root=tmp_path,
        run_ids={},
        lineage_root=tmp_path / "lineage",
        hmac_key=HMAC_KEY,
        public_key_pem=public,
    )

    assert result.ok is False
    assert result.status == "unreadable"
    assert result.receipt is None
