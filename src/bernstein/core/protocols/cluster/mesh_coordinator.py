"""Leaderless MESH claim coordination against a signed claim journal (#2558).

``ClusterTopology.STAR`` routes every assignment decision through one central
server: :class:`~bernstein.core.protocols.cluster.cluster.NodeRegistry` holds
who-is-alive and ``POST /cluster/steal`` arbitrates reassignment. That node is a
single point of failure and a single point of trust.

``ClusterTopology.MESH`` removes it. The arbiter is the signed, append-only,
Merkle-chained :class:`~bernstein.core.orchestration.tracker_pipeline.ClaimJournal`,
and this module is the thin coordinator that drives it:

* :meth:`MeshCoordinator.claim` appends a signed ``claim`` receipt, reconciles
  the resulting journal, and reports whether *this* node ended up the holder
  under the deterministic lowest-``entry_hash`` rule. A losing claim comes back
  with the winner named and the chain-anchored ``supersede`` receipt on disk.
* :meth:`MeshCoordinator.release` / :meth:`MeshCoordinator.renew` append the
  matching receipts, each naming the exact claim ``entry_hash`` they act on.
* :meth:`MeshCoordinator.expire_stale` retires leases whose TTL has elapsed --
  including a *peer's*, which is what stops a dead node's lease deadlocking the
  fleet with no central server to reap it. The receipt is signed by the
  observing node and names the retired claim as referenced data, so the
  signature and the declared identity never disagree.
* :meth:`MeshCoordinator.ingest` folds a gossiped receipt only after its
  Ed25519 signature and its chain link both verify, recording a signed ``fork``
  receipt when the chains have diverged instead of merging silently.

No method here consults a registry, a heartbeat, or a central URL. The clock is
always an explicit argument, so the receipts a run produces are reproducible;
the *fold* over those receipts is clock-free, which is what keeps two nodes'
projected state byte-identical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.orchestration.tracker_pipeline import project_claims

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Any

    from bernstein.core.orchestration.tracker_pipeline import (
        ClaimHold,
        ClaimIngestResult,
        ClaimJournal,
        ClaimReceipt,
        ClaimState,
    )
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

DEFAULT_MESH_LEASE_TTL_S: int = 300
"""Default MESH claim lease TTL, in seconds.

Longer than the STAR heartbeat interval because a MESH lease is retired by a
peer observing its expiry through the journal, not by a central timeout sweep.
"""

_MESH_IDENTITY_DIR = "identity"
_MESH_PRIVATE_KEY_NAME = "claim_signing.pem"
_MESH_PUBLIC_KEY_NAME = "claim_signing.pub"
_MESH_KID = "mesh-claim-journal-1"

__all__ = [
    "DEFAULT_MESH_LEASE_TTL_S",
    "MeshClaimOutcome",
    "MeshCoordinator",
    "PeerGossipResult",
    "build_mesh_coordinator",
]


def build_mesh_coordinator(
    cluster_config: object,
    *,
    sdd_dir: Path,
    chain: AuditChainStore | None = None,
) -> MeshCoordinator | None:
    """Build the MESH coordinator for ``cluster_config``, or ``None`` for STAR.

    Returning ``None`` for every non-MESH topology is the branch that keeps
    MESH opt-in: a STAR deployment never materialises a claim journal, never
    provisions a signing identity, and keeps the central ``NodeRegistry`` as
    its only coordination surface.

    The node's install identity is a persisted Ed25519 keypair under
    ``<sdd_dir>/cluster/identity``, and ``node_id`` is that key's RFC 7638
    thumbprint. Deriving the id *from the key* rather than from a hostname or
    an operator-set string means a receipt cannot claim to be from a node whose
    key it does not hold -- the two are the same fact.

    That fact only binds if the verifier knows which key a ``node_id`` should
    have, so the coordinator is always built with a pin map (#2997), never with
    ``None``:

    * ``cluster.gossip_peer_keys`` supplies the peers this node accepts gossip
      from.
    * This node's own public key is pinned to its own ``node_id``, so a peer
      cannot echo back receipts attributed to us under a key of its choosing,
      and a node recovering its journal from a peer still folds its own
      history.

    A MESH node with no configured pins therefore folds no foreign receipts at
    all rather than trusting whatever key a receipt carries. Closed is the
    default; opening it is an explicit act.

    Args:
        cluster_config: The resolved ``ClusterConfig``. Typed loosely so this
            module stays importable without pulling in the task models.
        sdd_dir: The project ``.sdd`` directory.
        chain: Optional HMAC audit chain to anchor every receipt into.

    Returns:
        A ready :class:`MeshCoordinator`, or ``None`` when the topology is not
        MESH or the signing identity cannot be provisioned.
    """
    from bernstein.core.tasks.models import ClusterTopology

    topology = getattr(cluster_config, "topology", None)
    if topology is not ClusterTopology.MESH:
        return None
    try:
        from bernstein.core.identity.http_signing import install_identity_keyid
        from bernstein.core.lineage.identity import load_or_create_signing_identity
        from bernstein.core.orchestration.tracker_pipeline import (
            ClaimJournal,
            default_claim_journal_path,
        )
        from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

        identity_dir = Path(sdd_dir) / "cluster" / _MESH_IDENTITY_DIR
        _, public_pem = load_or_create_signing_identity(
            identity_dir,
            private_name=_MESH_PRIVATE_KEY_NAME,
            public_name=_MESH_PUBLIC_KEY_NAME,
        )
        node_id = install_identity_keyid(public_pem.encode("ascii"))
        trusted_keys = _pinned_peer_keys(cluster_config, node_id=node_id, public_pem=public_pem)
        configured_path = getattr(cluster_config, "claim_journal_path", None)
        journal_path = Path(configured_path) if configured_path else default_claim_journal_path(Path(sdd_dir))
        journal = ClaimJournal(
            journal_path,
            kms_adapter=FileBasedKMSAdapter(identity_dir / _MESH_PRIVATE_KEY_NAME, kid=_MESH_KID),
            node_id=node_id,
            chain=chain,
        )
    except Exception as exc:  # pragma: no cover - environment-dependent
        # Fail loud but non-fatal: a MESH node without a journal has no
        # arbiter, so refusing to coordinate is correct. The gossip route then
        # answers 409 rather than accepting receipts it cannot verify against.
        logger.error("MESH coordinator unavailable, leaderless claiming disabled: %s", exc)
        return None
    if not any(pinned != node_id for pinned in trusted_keys):
        logger.warning(
            "MESH node %s has no pinned gossip peers: every gossiped receipt will be rejected. "
            "Add cluster.gossip_peer_keys to accept a peer's receipts.",
            node_id,
        )
    return MeshCoordinator(
        journal=journal,
        lease_ttl_s=int(getattr(cluster_config, "claim_lease_ttl_s", DEFAULT_MESH_LEASE_TTL_S)),
        peers=tuple(getattr(cluster_config, "gossip_peers", ()) or ()),
        trusted_keys=trusted_keys,
    )


def _pinned_peer_keys(
    cluster_config: object,
    *,
    node_id: str,
    public_pem: str,
) -> dict[str, dict[str, Any]]:
    """Return the ``node_id`` to JWK pin map this coordinator verifies against.

    Always includes this node's own key, so a receipt attributed to us is only
    folded when it was signed by us. Peers come from
    ``cluster.gossip_peer_keys``; a config that pins none yields a map holding
    only our own entry, which rejects every foreign receipt -- the fail-closed
    posture #2997 chose as the default.
    """
    from bernstein.core.identity.http_signing import public_key_jwk_from_pem

    pins: dict[str, dict[str, Any]] = {node_id: dict(public_key_jwk_from_pem(public_pem.encode("ascii")))}
    for peer_key in getattr(cluster_config, "gossip_peer_keys", ()) or ():
        pins[peer_key.node_id] = dict(peer_key.to_jwk())
    return pins


@dataclass(frozen=True, slots=True)
class PeerGossipResult:
    """Outcome of one gossip push to one peer.

    Attributes:
        peer: The peer base URL contacted.
        ok: Whether the peer answered successfully.
        accepted: Receipts the peer folded.
        head: The peer's journal head after the push.
        forked: Whether the peer reported a divergence.
        error: Transport or protocol failure text when ``ok`` is ``False``.
    """

    peer: str
    ok: bool
    accepted: int = 0
    head: str = ""
    forked: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MeshClaimOutcome:
    """Result of one leaderless self-claim attempt.

    Attributes:
        granted: Whether *this* node holds the key after reconciliation.
        entry_hash: The ``entry_hash`` of the claim receipt this node appended.
        holder: The deterministic holder after the fold, or ``None`` when the
            key ended up unheld.
        head: The journal head after the attempt.
        superseded_by: When not granted, the winning claim's ``entry_hash``.
    """

    granted: bool
    entry_hash: str
    holder: ClaimHold | None
    head: str
    superseded_by: str | None = None


class MeshCoordinator:
    """Leaderless claim coordination over a signed :class:`ClaimJournal`.

    Args:
        journal: The signed claim journal. On a shared filesystem this is one
            file every node appends to under an exclusive lock; across machines
            each node keeps its own copy and they converge by gossip.
        lease_ttl_s: Lease duration granted to a successful claim.
        peers: Gossip peer base URLs. Held for the gossip transport to read;
            the coordinator itself never dials them, so every method here works
            with the network down.
        trusted_keys: ``node_id`` to public-key JWK pinning applied to every
            ingested receipt. ``None`` verifies a gossiped receipt against the
            key it carries, which authenticates the bytes but not the identity;
            an empty map trusts no one. :func:`build_mesh_coordinator` always
            supplies a map, so ``None`` is reachable only by constructing a
            coordinator directly (#2997).
    """

    def __init__(
        self,
        *,
        journal: ClaimJournal,
        lease_ttl_s: int = DEFAULT_MESH_LEASE_TTL_S,
        peers: Sequence[str] = (),
        trusted_keys: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self._journal = journal
        self._lease_ttl_s = max(1, int(lease_ttl_s))
        self._peers = tuple(peers)
        self._trusted_keys = dict(trusted_keys) if trusted_keys is not None else None

    # -- introspection -------------------------------------------------

    @property
    def journal(self) -> ClaimJournal:
        """The signed journal this coordinator arbitrates through."""
        return self._journal

    @property
    def node_id(self) -> str:
        """This node's install identity id."""
        return self._journal.node_id

    @property
    def peers(self) -> tuple[str, ...]:
        """Configured gossip peer base URLs."""
        return self._peers

    @property
    def lease_ttl_s(self) -> int:
        """Lease duration granted to a successful claim, in seconds."""
        return self._lease_ttl_s

    def state(self) -> ClaimState:
        """Return the pure fold of the local journal."""
        return project_claims(self._journal.read())

    def head(self) -> str:
        """Return the local journal head hash."""
        return self._journal.head()

    def holder(self, tracker: str, ticket_id: str, role: str) -> ClaimHold | None:
        """Return the deterministic holder of a key, or ``None``."""
        return self.state().holder(tracker, ticket_id, role)

    # -- claim lifecycle -----------------------------------------------

    def claim(
        self,
        *,
        tracker: str,
        ticket_id: str,
        role: str,
        claimer_id: str,
        now: float,
    ) -> MeshClaimOutcome:
        """Self-claim ``(tracker, ticket_id, role)`` with no central arbiter.

        Appends a signed ``claim`` receipt, then reconciles: every key with more
        than one live claim gets a chain-anchored ``supersede`` receipt naming
        the lowest-``entry_hash`` winner. The winner is decided by the fold, not
        by who appended first, so two nodes that append concurrently reach the
        same answer without talking to each other.

        Args:
            tracker: Tracker adapter name.
            ticket_id: Tracker-side ticket id.
            role: Bernstein role name.
            claimer_id: Unique caller identifier within this node.
            now: Explicit wall-clock seconds. Used for the lease expiry and the
                receipt timestamp only; the fold that picks the winner never
                reads a clock.

        The outcome describes the journal *as this node can see it now*. A peer
        claim that has not arrived yet can still supersede this one: that is
        inherent to leaderless coordination, not a defect, and it is why the
        winner is a pure function of content hashes rather than of arrival
        order -- late news changes the answer for every observer identically.
        Call :meth:`confirm` after gossip has settled, and before any
        irreversible work, to re-check against the journal as it then stands.

        Returns:
            A :class:`MeshClaimOutcome`. ``granted`` is ``False`` when another
            node's concurrent claim won, and ``superseded_by`` names it.
        """
        ts_ns = int(now * 1_000_000_000)
        receipt = self._journal.append(
            kind="claim",
            tracker=tracker,
            ticket_id=ticket_id,
            role=role,
            claimer_id=claimer_id,
            lease_expires_at=float(now) + self._lease_ttl_s,
            ts_ns=ts_ns,
        )
        # Idempotent: a no-op when this claim is uncontended.
        self._journal.reconcile(ts_ns=ts_ns + 1)
        state = self.state()
        holder = state.holder(tracker, ticket_id, role)
        granted = holder is not None and holder.entry_hash == receipt.entry_hash
        return MeshClaimOutcome(
            granted=granted,
            entry_hash=receipt.entry_hash,
            holder=holder,
            head=state.head,
            superseded_by=(None if granted or holder is None else holder.entry_hash),
        )

    def confirm(
        self,
        outcome: MeshClaimOutcome,
        *,
        tracker: str,
        ticket_id: str,
        role: str,
        now: float,
    ) -> MeshClaimOutcome:
        """Re-check a claim against the journal as it now stands.

        The second half of the two-phase MESH claim. :meth:`claim` reports what
        the journal said at append time; a peer's competing claim may arrive
        afterwards through gossip and win by the deterministic rule. Confirming
        before irreversible work is what turns "I appended a claim" into "I am
        the holder every observer agrees on".

        Reconciles first, so a competing claim that arrived since gets its
        ``supersede`` receipt, then re-folds.

        Returns:
            A fresh :class:`MeshClaimOutcome` for the same ``entry_hash``.
        """
        self._journal.reconcile(ts_ns=int(now * 1_000_000_000))
        state = self.state()
        holder = state.holder(tracker, ticket_id, role)
        granted = holder is not None and holder.entry_hash == outcome.entry_hash
        return MeshClaimOutcome(
            granted=granted,
            entry_hash=outcome.entry_hash,
            holder=holder,
            head=state.head,
            superseded_by=(None if granted or holder is None else holder.entry_hash),
        )

    def release(
        self,
        *,
        tracker: str,
        ticket_id: str,
        role: str,
        claimer_id: str,
        now: float,
        target_entry_hash: str | None = None,
    ) -> ClaimReceipt | None:
        """Release a held claim by appending a signed ``release`` receipt.

        ``target_entry_hash`` names the exact claim being released; when
        omitted the current holder for the key is resolved from the fold. If
        the key is unheld the call is a no-op returning ``None`` -- releasing
        twice must not append a receipt that retires someone else's later
        claim on the same key.
        """
        target = target_entry_hash
        if target is None:
            holder = self.holder(tracker, ticket_id, role)
            if holder is None:
                return None
            target = holder.entry_hash
        return self._journal.append(
            kind="release",
            tracker=tracker,
            ticket_id=ticket_id,
            role=role,
            claimer_id=claimer_id,
            lease_expires_at=0.0,
            ts_ns=int(now * 1_000_000_000),
            target_entry_hash=target,
        )

    def renew(
        self,
        *,
        tracker: str,
        ticket_id: str,
        role: str,
        claimer_id: str,
        now: float,
    ) -> ClaimReceipt | None:
        """Extend this node's lease on a held claim by ``lease_ttl_s``."""
        holder = self.holder(tracker, ticket_id, role)
        if holder is None or holder.node_id != self.node_id:
            return None
        return self._journal.append(
            kind="renew",
            tracker=tracker,
            ticket_id=ticket_id,
            role=role,
            claimer_id=claimer_id,
            lease_expires_at=float(now) + self._lease_ttl_s,
            ts_ns=int(now * 1_000_000_000),
        )

    def expire_stale(self, *, now: float) -> list[ClaimReceipt]:
        """Retire every hold whose lease has elapsed, including a peer's.

        With no central server there is no timeout sweep, so any node that
        observes an expired lease may retire it. The ``expire`` receipt is
        signed by *this* node and names the retired claim's ``entry_hash`` as
        referenced data, so a verifier pinning keys by ``node_id`` still finds
        every receipt signed by the node it claims to be from.

        Deterministic given ``(journal, now)``: holds are retired in sorted key
        order, and the resulting receipts -- not the clock read that produced
        them -- are what the fold consumes.

        Returns:
            The receipts appended, in the order they were appended.
        """
        state = self.state()
        emitted: list[ClaimReceipt] = []
        ts_ns = int(now * 1_000_000_000)
        for key in sorted(state.holds):
            hold = state.holds[key]
            if hold.lease_expires_at > now:
                continue
            emitted.append(
                self._journal.append(
                    kind="expire",
                    tracker=hold.tracker,
                    ticket_id=hold.ticket_id,
                    role=hold.role,
                    claimer_id=self.node_id,
                    lease_expires_at=0.0,
                    ts_ns=ts_ns,
                    target_entry_hash=hold.entry_hash,
                ),
            )
            ts_ns += 1
        return emitted

    # -- gossip --------------------------------------------------------

    def receipts_since(self, entry_hash: str | None = None) -> list[ClaimReceipt]:
        """Return the receipts a peer is missing after ``entry_hash``.

        ``None`` (or an unknown hash) returns the whole journal, which is what a
        peer joining or recovering from a long partition needs.
        """
        receipts = self._journal.read()
        if entry_hash is None:
            return receipts
        for index, receipt in enumerate(receipts):
            if receipt.entry_hash == entry_hash:
                return receipts[index + 1 :]
        return receipts

    def ingest(self, receipt: ClaimReceipt, *, now: float) -> ClaimIngestResult:
        """Verify and fold one gossiped receipt; record a fork if it diverges.

        Delegates to :meth:`ClaimJournal.ingest`, which checks the Ed25519
        signature and the recomputed entry hash *before* writing anything, then
        mirrors the outcome into the cluster audit log.
        """
        from bernstein.core.protocols.cluster import cluster_audit

        result = self._journal.ingest(
            receipt,
            ts_ns=int(now * 1_000_000_000),
            trusted_keys=self._trusted_keys,
        )
        if result.status == "forked":
            cluster_audit.record_claim_forked(
                entry_hash=receipt.entry_hash,
                local_head=receipt.prev_entry_hash,
                divergence_index=int(result.divergence_index or 0),
                from_node=receipt.node_id,
                observed_by=self.node_id,
            )
        else:
            cluster_audit.record_claim_gossiped(
                entry_hash=receipt.entry_hash,
                kind=receipt.kind,
                from_node=receipt.node_id,
                to_node=self.node_id,
                status=result.status,
            )
        return result

    def push_to_peers(
        self,
        *,
        auth_token: str | None = None,
        timeout_s: float = 10.0,
        client: Any | None = None,
    ) -> dict[str, PeerGossipResult]:
        """Push this node's receipts to every configured gossip peer.

        Each peer is contacted independently and a failure is recorded, not
        raised: a leaderless fleet must keep coordinating when one peer is
        unreachable, which is the whole reason the topology exists. Peers that
        answer with a fork are reported so the caller can surface the
        divergence rather than retry into it.

        Args:
            auth_token: Bearer credential for the peer's cluster surface.
            timeout_s: Per-request timeout.
            client: Optional pre-built ``httpx.Client``. Supplied by tests and
                by callers that need a shared connection pool or mTLS.

        Returns:
            A per-peer :class:`PeerGossipResult`, keyed by peer base URL.
        """
        import httpx

        payload = {
            "receipts": [receipt.to_dict() for receipt in self._journal.read()],
            "head": self.head(),
            "node_id": self.node_id,
        }
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        results: dict[str, PeerGossipResult] = {}
        owns_client = client is None
        http = httpx.Client(timeout=timeout_s) if client is None else client
        try:
            for peer in self._peers:
                url = f"{peer.rstrip('/')}/cluster/claims/gossip"
                try:
                    response = http.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    body = response.json()
                except Exception as exc:  # one peer must not stop the sweep
                    logger.warning("Claim gossip to %s failed: %s", peer, exc)
                    results[peer] = PeerGossipResult(peer=peer, ok=False, error=str(exc))
                    continue
                results[peer] = PeerGossipResult(
                    peer=peer,
                    ok=True,
                    accepted=int(body.get("accepted", 0)),
                    head=str(body.get("head", "")),
                    forked=bool(body.get("forked", False)),
                )
                if results[peer].forked:
                    logger.error("Claim gossip to %s reported a journal fork", peer)
        finally:
            if owns_client:
                http.close()
        return results

    def ingest_many(self, receipts: Iterable[ClaimReceipt], *, now: float) -> list[ClaimIngestResult]:
        """Ingest an ordered batch, stopping at the first fork.

        Stopping is deliberate: once the chains have diverged, every later
        receipt in the batch links to a chain this node has rejected, so
        continuing would mint one redundant fork receipt per entry and bury the
        divergence index that actually locates the split.
        """
        results: list[ClaimIngestResult] = []
        for receipt in receipts:
            result = self.ingest(receipt, now=now)
            results.append(result)
            if result.status == "forked":
                break
        return results
