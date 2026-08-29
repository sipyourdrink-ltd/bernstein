"""Pair each worktree branch of a fan-out with the spine that recorded it.

A fan-out leaves N worktrees behind, and a :class:`~bernstein.core.lineage.spine.LineageSpine`
per run records every artifact write. The two halves were never joined: a
:class:`~bernstein.core.worktrees.classifier.ClassifiedWorktree` carries no
``head_sha`` and no ``run_id``, and a spine is indexed by ``run_id`` with no
back-reference to the worktree whose writes it holds. So no single call could
answer, per branch, *what git state it held* and *which spine attested it*.

:func:`build_run_graph` composes the existing primitives into that answer. It
adds no storage: the branch list comes from ``classify_worktrees``, the head
sha from git, and the spine head from ``LineageSpine.head_hash()``. The graph
root is a content hash over the sorted ``(head_sha, spine_head_hash)`` pairs,
so two runs over byte-identical inputs produce the same root.

Resolving ``session_id`` to ``run_id``
-------------------------------------

Nothing in the repository records that mapping, so it is supplied by the
caller as ``run_ids``. The alternative - teaching the spawner to write a
``run_id`` into the PID record that ``_read_pid_record`` already parses - was
rejected for two reasons. It changes the spawn path, which is outside this
slice; and it is only true going forward, so every worktree created before
the change would resolve as unresolved. That would bake a migration into the
data rather than into one caller. Passing the mapping in also keeps this
function pure, which is what makes the root hash reproducible under test.

A session with no entry in ``run_ids`` is **not** dropped. It becomes a node
with :data:`RunGraphNodeStatus.UNRESOLVED` and contributes to the root hash
under its own sentinel, so a fan-out that silently lost a spine hashes
differently from one that never had that branch.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.security.agent_card_signer import sign_detached_jws_over_canonical
from bernstein.core.worktrees.classifier import _git_head_sha, classify_worktrees

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

#: Stands in for a missing ``head_sha`` or ``spine_head_hash`` in the root
#: pre-image. A literal empty string would let "absent" and "recorded as
#: empty" collide, and an empty spine head *is* the empty string.
ABSENT = "\x00absent"

#: Lineage run id under which every run-graph receipt is anchored, kept separate
#: so fan-out receipts never interleave with per-task journals.
RUN_GRAPH_RUN_ID = "run-graph"


class RunGraphNodeStatus(enum.Enum):
    """Whether a branch could be paired with a spine."""

    #: ``run_id`` known and its spine read.
    RESOLVED = "resolved"
    #: No ``run_id`` for this session; the node is kept and marked.
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class RunGraphNode:
    """One branch of a fan-out, paired with the spine that recorded it.

    Attributes:
        session_id: Worktree session id, from the classifier.
        head_sha: Full git HEAD sha of the worktree, or ``None`` when git
            could not resolve one (detached, unborn, corrupt, or missing).
        run_id: Run whose spine recorded this branch's writes, or ``None``
            when the caller supplied no mapping for this session.
        spine_head_hash: That spine's current chain head, or ``None`` when
            ``run_id`` is ``None``. An empty string is a *valid* value - it
            is what an empty run's spine returns.
        status: :class:`RunGraphNodeStatus`.
    """

    session_id: str
    head_sha: str | None
    run_id: str | None
    spine_head_hash: str | None
    status: RunGraphNodeStatus


@dataclass(frozen=True, slots=True)
class RunGraph:
    """Every branch of one fan-out, plus a hash over their pairs.

    Attributes:
        nodes: One :class:`RunGraphNode` per worktree, ordered by
            ``session_id`` so the sequence does not depend on directory
            iteration order.
        root_hash: ``sha256:``-prefixed hash over the sorted
            ``(session_id, head_sha, spine_head_hash)`` triples.
    """

    nodes: tuple[RunGraphNode, ...]
    root_hash: str


def compute_root_hash(nodes: tuple[RunGraphNode, ...]) -> str:
    """Hash the ``(session_id, head_sha, spine_head_hash)`` triples.

    ``session_id`` is part of the pre-image, not just the sort key: two
    branches that happen to share a head sha and a spine head are still
    distinct branches, and a root that ignored the id could not tell a
    renamed session from an unchanged one.
    """
    payload = [
        [
            node.session_id,
            ABSENT if node.head_sha is None else node.head_sha,
            ABSENT if node.spine_head_hash is None else node.spine_head_hash,
        ]
        for node in sorted(nodes, key=lambda n: n.session_id)
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return content_hash_of(canonical.encode("utf-8"))


def build_run_graph(
    repo_root: Path,
    *,
    run_ids: Mapping[str, str],
    lineage_root: Path,
    hmac_key: bytes,
    head_sha_resolver: Callable[[Path], str | None] = _git_head_sha,
) -> RunGraph:
    """Assemble a :class:`RunGraph` for every worktree under ``repo_root``.

    Args:
        repo_root: Repository root whose worktrees are classified.
        run_ids: ``session_id -> run_id``. Sessions absent from this mapping
            become ``UNRESOLVED`` nodes rather than being dropped.
        lineage_root: Root under which each run's spine directory lives.
        hmac_key: Key the spines were written with, needed to open them.
        head_sha_resolver: Injection point for git HEAD resolution; defaults
            to the classifier's own resolver.

    Returns:
        A :class:`RunGraph` whose ``nodes`` are ordered by ``session_id``.
    """
    nodes: list[RunGraphNode] = []
    for worktree in classify_worktrees(repo_root):
        run_id = run_ids.get(worktree.session_id)
        if run_id is None:
            nodes.append(
                RunGraphNode(
                    session_id=worktree.session_id,
                    head_sha=head_sha_resolver(worktree.path),
                    run_id=None,
                    spine_head_hash=None,
                    status=RunGraphNodeStatus.UNRESOLVED,
                )
            )
            continue
        spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
        nodes.append(
            RunGraphNode(
                session_id=worktree.session_id,
                head_sha=head_sha_resolver(worktree.path),
                run_id=run_id,
                spine_head_hash=spine.head_hash(),
                status=RunGraphNodeStatus.RESOLVED,
            )
        )

    ordered = tuple(sorted(nodes, key=lambda n: n.session_id))
    return RunGraph(nodes=ordered, root_hash=compute_root_hash(ordered))


@dataclass(frozen=True, slots=True)
class RunGraphReceipt:
    """A sealed receipt that anchors an entire fan-out's RunGraph.

    The body (everything the ``receipt_hash`` covers) binds the graph root hash,
    per-node hashes, and timestamp. The ``journal_entry_hash`` is assigned post-seal.
    """

    schema_version: int
    graph_root_hash: str
    node_hashes: tuple[str, ...]
    timestamp: int
    receipt_hash: str
    journal_entry_hash: str = ""

    def body(self) -> dict[str, object]:
        """The hashed body: every field except the receipt hash and anchor."""
        return {
            "schema_version": self.schema_version,
            "graph_root_hash": self.graph_root_hash,
            "node_hashes": list(self.node_hashes),
            "timestamp": self.timestamp,
        }

    def canonical_payload_without_anchor(self) -> str:
        """Canonical JSON of the body plus receipt hash (excludes the anchor)."""
        payload = self.body()
        payload["receipt_hash"] = self.receipt_hash
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def canonical_bytes(self) -> bytes:
        """Canonical bytes sealed into the lineage spine (body + hash)."""
        return self.canonical_payload_without_anchor().encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RunGraphReceipt:
        return cls(
            schema_version=int(raw["schema_version"]),
            graph_root_hash=str(raw["graph_root_hash"]),
            node_hashes=tuple(str(h) for h in raw["node_hashes"]),
            timestamp=int(raw["timestamp"]),
            receipt_hash=str(raw["receipt_hash"]),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "graph_root_hash": self.graph_root_hash,
            "node_hashes": list(self.node_hashes),
            "timestamp": self.timestamp,
            "receipt_hash": self.receipt_hash,
            "journal_entry_hash": self.journal_entry_hash,
        }


@dataclass(frozen=True, slots=True)
class RunGraphReceiptSchema:
    """Canonical bytes revision for RunGraphReceipt schema.

    Bump only on a wire-format change.
    """

    version: int = 1


def _hash_obj(obj: object) -> str:
    """Canonical SHA256 hash over canonical JSON (mirror gate_receipt._hash_obj)."""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_run_graph_receipt(
    *,
    graph: RunGraph,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    timestamp: int,
    private_key_pem: bytes,
    chain: AuditChainStore | None = None,
    actor: str = "bernstein.run_graph",
) -> RunGraphReceipt:
    """Classify a fan-out's RunGraph and seal it into a receipt.

    The receipt anchors the entire fan-out's graph root into the ``run-graph``
    lineage spine, mirrors the identity into the HMAC audit chain, and signs the
    canonical bytes with the Ed25519 agent identity. This provides a single
    artifact that binds together all N branches, solving the fan-out receipt
    problem described in issue #3759.

    Args:
        graph: The RunGraph to seal (produced by the earlier slice of #2929).
        workdir: Project root (receipt written under ``.sdd/run-graph``).
        lineage_root: ``.sdd/lineage`` root for the spine.
        hmac_key: Audit-chain HMAC key for the spine seal.
        timestamp: Integer timestamp anchored into the spine entry (stable).
        private_key_pem: PEM-encoded PKCS#8 Ed25519 private key for signing.
        chain: Optional :class:`AuditChainStore` accepting the mirror.
        actor: Recorded actor; defaults to ``"bernstein.run_graph"``.

    Returns:
        The sealed :class:`RunGraphReceipt`.
    """
    # Compute deterministic node hashes from the graph's ordered nodes
    node_hashes = tuple(_node_hash(node) for node in graph.nodes)
    schema = RunGraphReceiptSchema()
    unsealed = RunGraphReceipt(
        schema_version=schema.version,
        graph_root_hash=graph.root_hash,
        node_hashes=node_hashes,
        timestamp=timestamp,
        receipt_hash="",
    )
    receipt_hash = _hash_obj(unsealed.body())
    sealed_no_anchor = RunGraphReceipt(
        schema_version=unsealed.schema_version,
        graph_root_hash=unsealed.graph_root_hash,
        node_hashes=unsealed.node_hashes,
        timestamp=unsealed.timestamp,
        receipt_hash=receipt_hash,
    )

    # Anchor into the lineage spine under the dedicated run id
    spine = LineageSpine(lineage_root, run_id=RUN_GRAPH_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join(("run-graph", f"{receipt_hash}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=sealed_no_anchor.canonical_bytes(),
        actor=actor,
        step_id=receipt_hash,
        model="run-graph-receipt",
        timestamp=timestamp,
    )

    # Sign the canonical bytes (excluding the spine anchor)
    signed_jws = sign_detached_jws_over_canonical(
        canonical_body=sealed_no_anchor.canonical_bytes(),
        private_key_pem=private_key_pem,
        typ="run-graph-receipt+jws",
        kid="bernstein.run_graph",
    )

    # Write signed receipt JSON under .sdd/run-graph/, carrying the anchor.
    # The anchor is what ties the file to the spine entry covering it; a file
    # written from ``sealed_no_anchor`` carries an empty one, and a reader
    # then has no way to find the entry that seals it.
    anchored = RunGraphReceipt(
        schema_version=sealed_no_anchor.schema_version,
        graph_root_hash=sealed_no_anchor.graph_root_hash,
        node_hashes=sealed_no_anchor.node_hashes,
        timestamp=sealed_no_anchor.timestamp,
        receipt_hash=receipt_hash,
        journal_entry_hash=anchor,
    )
    receipt_dir = workdir / ".sdd" / "run-graph"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{receipt_hash}.json"
    receipt_path.write_text(
        json.dumps(
            {**anchored.to_dict(), "signed_jws": signed_jws},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    # Mirror into audit chain if requested
    if chain is not None:
        _record_run_graph_receipt(
            chain=chain,
            receipt_hash=receipt_hash,
            graph_root_hash=sealed_no_anchor.graph_root_hash,
            node_hashes=sealed_no_anchor.node_hashes,
            timestamp=sealed_no_anchor.timestamp,
            journal_entry_hash=anchor,
            actor=actor,
        )

    sealed = RunGraphReceipt(
        schema_version=sealed_no_anchor.schema_version,
        graph_root_hash=sealed_no_anchor.graph_root_hash,
        node_hashes=sealed_no_anchor.node_hashes,
        timestamp=sealed_no_anchor.timestamp,
        receipt_hash=receipt_hash,
        journal_entry_hash=anchor,
    )
    return sealed


def _node_hash(node: RunGraphNode) -> str:
    """Compute deterministic hash for a RunGraphNode.

    The hash includes session_id, head_sha, spine_head_hash, and status.
    This ensures the receipt commits to exactly which nodes were sealed.
    """
    payload = {
        "session_id": node.session_id,
        "head_sha": node.head_sha if node.head_sha is not None else None,
        "run_id": node.run_id if node.run_id is not None else None,
        "spine_head_hash": node.spine_head_hash if node.spine_head_hash is not None else None,
        "status": node.status.value,
    }
    return _hash_obj(payload)


@dataclass(frozen=True, slots=True)
class RunGraphVerifyResult:
    """The outcome of checking one sealed receipt against the tree it claims.

    Attributes:
        ok: True only when every check below passed.
        status: ``verified``, or which check refused: ``unreadable``,
            ``empty``, ``tampered``, ``diverged``, ``unanchored``.
        reason: One sentence naming what failed, and where. For a divergence
            this names the branch, because "the graph changed" is not
            actionable and "session X's head moved" is.
        receipt: The receipt as read, or ``None`` when it could not be parsed.
    """

    ok: bool
    status: str
    reason: str
    receipt: RunGraphReceipt | None = None


def verify_run_graph_receipt(
    *,
    receipt_path: Path,
    repo_root: Path,
    run_ids: Mapping[str, str],
    lineage_root: Path,
    hmac_key: bytes,
    public_key_pem: bytes,
    head_sha_resolver: Callable[[Path], str | None] = _git_head_sha,
) -> RunGraphVerifyResult:
    """Check a sealed receipt by rebuilding what it claims, not by reading it.

    A receipt that only replays its own stored fields proves nothing: every
    hash inside it was written by the same pass that wrote the fields, so a
    hand-edited receipt whose internals agree with each other passes such a
    check. The sibling patterns in this package re-derive for that reason --
    :meth:`LineageSpine.verify` walks the chain rather than trusting a cached
    head -- and this does the same for a fan-out.

    Five checks, in cost order so a cheap refusal never pays for an expensive
    one, each fail-closed:

    1. The receipt parses, and carries a signature.
    2. It covers at least one branch. An empty fan-out is refused rather than
       passing on nothing to disagree with.
    3. ``receipt_hash`` recomputes from the body.
    4. The detached JWS verifies over the canonical bytes.
    5. The graph is rebuilt from the worktrees and the spines, and its root
       hash and per-node hashes must equal the sealed ones.
    6. The spine entry named by ``journal_entry_hash`` exists and covers those
       same canonical bytes.

    Checks 3 and 4 catch an edited receipt; check 5 is the one that catches an
    edited *tree* -- a flipped byte in a branch's spine moves that branch's
    head, so the rebuilt node hash stops matching and the branch is named.

    Args:
        receipt_path: The ``.sdd/run-graph/<hash>.json`` file to check.
        repo_root: Repository root whose worktrees are re-classified.
        run_ids: ``session_id -> run_id``, as passed when the graph was built.
        lineage_root: Root under which the spines live.
        hmac_key: Key the spines were written with.
        public_key_pem: SPKI PEM of the Ed25519 public key that sealed it.
        head_sha_resolver: Injection point for the git head lookup.

    Returns:
        A :class:`RunGraphVerifyResult`; ``ok`` is true only for ``verified``.
    """
    from bernstein.core.security.agent_card_signer import verify_detached_jws_over_canonical

    try:
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return RunGraphVerifyResult(
            ok=False, status="unreadable", reason=f"receipt at {receipt_path} could not be read: {exc}"
        )
    if not isinstance(raw, dict):
        return RunGraphVerifyResult(ok=False, status="unreadable", reason="receipt is not a JSON object")
    signed_jws = raw.get("signed_jws")
    if not isinstance(signed_jws, str) or not signed_jws:
        return RunGraphVerifyResult(ok=False, status="unreadable", reason="receipt carries no signature")
    try:
        receipt = RunGraphReceipt.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        return RunGraphVerifyResult(ok=False, status="unreadable", reason=f"receipt fields are malformed: {exc}")

    # An empty fan-out has nothing to disagree with, so every check below would
    # pass on it. Refuse instead: sealing zero branches is a defect at the
    # sealing end, and reporting it as verified hides that.
    if not receipt.node_hashes:
        return RunGraphVerifyResult(ok=False, status="empty", reason="receipt seals no branches", receipt=receipt)

    stored_hash = receipt.receipt_hash
    if _hash_obj(receipt.body()) != stored_hash:
        return RunGraphVerifyResult(
            ok=False, status="tampered", reason="receipt_hash does not match the body it covers", receipt=receipt
        )
    if not verify_detached_jws_over_canonical(
        canonical_body=receipt.canonical_bytes(),
        detached_jws=signed_jws,
        public_key_pem=public_key_pem,
        expected_typ="run-graph-receipt+jws",
    ):
        return RunGraphVerifyResult(
            ok=False, status="tampered", reason="signature does not verify over the receipt body", receipt=receipt
        )

    rederived = build_run_graph(
        repo_root,
        run_ids=run_ids,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        head_sha_resolver=head_sha_resolver,
    )
    rederived_hashes = tuple(_node_hash(node) for node in rederived.nodes)
    if rederived_hashes != receipt.node_hashes:
        return RunGraphVerifyResult(
            ok=False, status="diverged", reason=_diverged_reason(receipt, rederived, rederived_hashes), receipt=receipt
        )
    # A node hash carries the spine's *stored* head, so a branch whose journal
    # was edited underneath it still hashes the same: the head file is not
    # rewritten by an edit to the rows. Only walking the chain sees that, which
    # is why this is a separate check and not folded into the comparison above.
    for node in rederived.nodes:
        if node.run_id is None:
            continue
        branch = LineageSpine(lineage_root, run_id=node.run_id, hmac_key=hmac_key)
        if not branch.verify().ok:
            return RunGraphVerifyResult(
                ok=False,
                status="diverged",
                reason=f"branch {node.session_id} has a spine that no longer verifies",
                receipt=receipt,
            )

    if rederived.root_hash != receipt.graph_root_hash:
        # Every node agreed and the root did not, so the root covers something
        # the nodes do not -- ordering. Worth its own sentence rather than
        # being folded into the node message, which would misdescribe it.
        return RunGraphVerifyResult(
            ok=False,
            status="diverged",
            reason="every branch matches but the graph root does not; the sealed branch order differs",
            receipt=receipt,
        )

    spine = LineageSpine(lineage_root, run_id=RUN_GRAPH_RUN_ID, hmac_key=hmac_key)
    expected_content = content_hash_of(receipt.canonical_bytes())
    anchored = any(
        entry.entry_hash == receipt.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return RunGraphVerifyResult(
            ok=False,
            status="unanchored",
            reason=f"no entry in the {RUN_GRAPH_RUN_ID} spine anchors this receipt",
            receipt=receipt,
        )

    return RunGraphVerifyResult(
        ok=True,
        status="verified",
        reason=f"{len(receipt.node_hashes)} branch(es) re-derived and matched",
        receipt=receipt,
    )


def _diverged_reason(
    receipt: RunGraphReceipt,
    rederived: RunGraph,
    rederived_hashes: tuple[str, ...],
) -> str:
    """Name the branch that moved, rather than reporting that something did.

    The sealed side carries hashes, not nodes, so the session id has to come
    from the re-derived side. When the counts differ there is no pairing to
    read a name out of, and the count itself is the finding.
    """
    if len(rederived_hashes) != len(receipt.node_hashes):
        return f"receipt seals {len(receipt.node_hashes)} branch(es), the tree now has {len(rederived_hashes)}"
    for index, (sealed, current) in enumerate(zip(receipt.node_hashes, rederived_hashes, strict=True)):
        if sealed != current:
            return f"branch {rederived.nodes[index].session_id} no longer matches the sealed receipt"
    return "branch hashes differ from the sealed receipt"


def _record_run_graph_receipt(
    *,
    chain: AuditChainStore,
    receipt_hash: str,
    graph_root_hash: str,
    node_hashes: tuple[str, ...],
    timestamp: int,
    journal_entry_hash: str = "",
    actor: str = "bernstein.run_graph",
) -> None:
    """Append a ``run_graph.sealed`` event into *chain*.

    Mirrors one sealed fan-out receipt into the HMAC chain so an operator can
    prove, from the chain alone, that the exact set of N branches came from one
    fan-out, anchored by a single object. Only hashes and the anchor are
    recorded -- never the raw worktree or spine content.

    Args:
        chain: The audit chain store accepting the entry.
        receipt_hash: Content hash pinning the whole run-graph receipt.
        graph_root_hash: The RunGraph root hash that was sealed.
        node_hashes: Deterministic hashes of each node in the sealed graph.
        timestamp: Integer timestamp when the receipt was sealed.
        journal_entry_hash: Lineage-spine entry hash anchoring the sealed receipt.
        actor: Recorded actor; defaults to ``"bernstein.run_graph"``.
    """
    from bernstein.core.security.audit_chain import record_run_graph_receipt

    record_run_graph_receipt(
        chain=chain,
        receipt_hash=receipt_hash,
        graph_root_hash=graph_root_hash,
        node_hashes=node_hashes,
        timestamp=timestamp,
        journal_entry_hash=journal_entry_hash,
        actor=actor,
    )


__all__ = [
    "ABSENT",
    "RUN_GRAPH_RUN_ID",
    "RunGraph",
    "RunGraphNode",
    "RunGraphNodeStatus",
    "RunGraphReceipt",
    "RunGraphReceiptSchema",
    "RunGraphVerifyResult",
    "_node_hash",
    "_record_run_graph_receipt",
    "build_run_graph",
    "build_run_graph_receipt",
    "compute_root_hash",
    "verify_run_graph_receipt",
]
