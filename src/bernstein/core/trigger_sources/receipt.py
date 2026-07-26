"""Automation bridge: signed trigger receipts and chain-anchored status proofs (#2512).

Operators embed runs inside larger workflows: a form submission starts a run, a
completed run closes a ticket or advances a deploy step. When the workflow
platform is the system of record for that chain of events, both directions have
to be checkable rather than asserted. A task id in a webhook response says
nothing about which payload was admitted under which scope, and an unsigned
status callback is the weakest link in any later incident review.

The artefact IS the proof
-------------------------
* A :class:`TriggerReceipt` binds ``{payload_digest, graph_digest, scope,
  admission_chain_head}`` for one inbound trigger, is signed with the install's
  Ed25519 identity, and is anchored in the HMAC audit chain. The platform
  stores the receipt alongside its run reference; the receipt alone is enough to
  prove offline who asked for the run, with what payload, at what chain
  position.
* A refusal is a receipt too. An unauthenticated, stale, or replayed trigger
  mints a receipt with ``outcome="refused"`` and its own chain anchor, so the
  negative path is exactly as discoverable as the positive one -- never a
  silent drop.
* A :class:`StatusProof` binds ``{status, producing_event_digest, chain_head}``
  and travels as an additive key inside the existing callback payload. The
  receiving system verifies the status it was told against the chain without
  trusting the transport.

Determinism
-----------
:func:`project_task_graph` is a pure function of the normalised trigger intent:
node ids are content-addressed, ordering is canonical, and no clock or random
source participates. Two operators firing byte-identical payloads therefore
carry receipts bearing the same ``graph_digest``, which is what makes "we fired
the identical graph" a comparison rather than a claim. Ed25519 is deterministic
(RFC 8032), so a re-sent callback reproduces byte-identical envelope bytes.

Strip the chain, the signature, and the content addressing and the receipts are
just JSON files; anchored and signed they are the platform's independently
checkable copy of what Bernstein actually did.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.sanitize import sanitize_log

if sys.platform == "win32":
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

#: Version stamped into every receipt and proof binding preimage.
AUTOMATION_BRIDGE_SCHEMA_VERSION = 1

#: Additive key the status proof travels under inside the callback payload.
#: Existing consumers of the plain payload keep parsing it unchanged.
PROOF_ENVELOPE_KEY = "automation_bridge_proof"

#: Environment override for the bridge state root (receipts, nonce ledger,
#: install identity). Defaults to ``.sdd/automation-bridge`` under the cwd.
BRIDGE_ROOT_ENV = "BERNSTEIN_AUTOMATION_BRIDGE_ROOT"

TRIGGER_OUTCOME_ADMITTED = "admitted"
TRIGGER_OUTCOME_REFUSED = "refused"

#: Canonical refusal reasons. Recorded verbatim on the refusal receipt so an
#: operator reading the chain sees why a trigger was turned away.
REFUSAL_UNAUTHENTICATED = "signature_did_not_verify"
REFUSAL_STALE_TIMESTAMP = "timestamp_outside_replay_window"
REFUSAL_REPLAYED_TRIGGER = "replayed_trigger_id"
REFUSAL_MALFORMED_TRIGGER = "malformed_trigger_payload"

_IDENTITY_PRIVATE_NAME = "automation-bridge-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "automation-bridge-identity-public.pem"

_TRIGGERS_SUBDIR = "triggers"
_STATUS_SUBDIR = "status"

_ROLES = ("dev", "qa", "docs", "ops", "architect")
_DEFAULT_ROLE = "dev"
_DEFAULT_PRIORITY = "2"


class AutomationBridgeError(RuntimeError):
    """Raised when the bridge cannot mint or anchor a receipt."""


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def compute_payload_digest(body: bytes) -> str:
    """Return the content hash of a raw trigger body.

    Admission identity is the payload bytes and nothing else, so identical
    bytes digest identically across runs, hosts, and operators.
    """
    return _sha256_bytes(body)


def compute_document_digest(document: dict[str, Any]) -> str:
    """Return the content hash of a canonicalised JSON document."""
    return _sha256_bytes(_canonical_bytes(document))


# ---------------------------------------------------------------------------
# Bridge state locations and install identity
# ---------------------------------------------------------------------------


def bridge_root(default: Path | None = None) -> Path:
    """Return the bridge state root.

    Resolution order: :data:`BRIDGE_ROOT_ENV`, then the caller's ``default``,
    then ``.sdd/automation-bridge`` relative to the working directory. The
    environment wins so an operator can relocate bridge state without touching
    the server's data layout.
    """
    override = os.environ.get(BRIDGE_ROOT_ENV, "")
    if override:
        return Path(override).expanduser()
    if default is not None:
        return default
    return Path(".sdd") / "automation-bridge"


def load_or_create_bridge_identity(root: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's Ed25519 bridge identity.

    The keypair is persisted under ``root`` so the same install signs every
    receipt and a verifier checks the signature offline against the public key
    embedded in the receipt. The private key file is written with ``0600`` mode.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    from bernstein.core.lineage.identity import generate_keypair

    private_path = root / _IDENTITY_PRIVATE_NAME
    public_path = root / _IDENTITY_PUBLIC_NAME
    if private_path.is_file() and public_path.is_file():
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    root.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(".pem.tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


def _safe_name(value: str) -> str:
    """Return a filesystem-safe basename for an arbitrary caller-supplied id.

    The id is content-hashed so the name is portable and cannot introduce a
    path separator regardless of the id's shape.
    """
    if not value:
        raise ValueError("empty identifier")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Per-identity decision exclusivity
# ---------------------------------------------------------------------------

#: Per-lock-file re-entrancy state for :func:`_decision_lock`.
#:
#: Mirrors the audit chain's append guard for the same reasons. ``flock``
#: attaches to an *open file description*, so a second ``os.open`` +
#: ``flock(LOCK_EX)`` from the thread that already holds the lock blocks on
#: itself; the per-thread depth counter lets the outermost acquisition own the
#: ``flock`` while inner ones pass through, and the per-file ``RLock`` keeps a
#: different thread out for the whole nested section so re-entrancy never
#: widens the window it exists to close.
#:
#: Guards are keyed by the lock file's ``(st_dev, st_ino)`` and never evicted.
#: The identity is load-bearing: two spellings of one path must map to one
#: guard, or a thread that entered under one and re-entered under the other
#: sees depth 0 and re-takes a ``flock`` it already holds. The live set is one
#: entry per identity this process actually adjudicates.
_DECISION_GUARDS: dict[str, threading.RLock] = {}
_DECISION_GUARDS_LOCK = threading.Lock()
_DECISION_DEPTH = threading.local()


def _decision_guard(lock_key: str) -> threading.RLock:
    """Return the process-wide re-entrant guard for one lock file."""
    with _DECISION_GUARDS_LOCK:
        guard = _DECISION_GUARDS.get(lock_key)
        if guard is None:
            guard = threading.RLock()
            _DECISION_GUARDS[lock_key] = guard
        return guard


@contextlib.contextmanager
def _decision_lock(lock_path: Path) -> Iterator[None]:
    """Hold one inbound identity exclusively across read-decide-record.

    Both bridge paths decide from state they read (the replay ledger, the proof
    cache) and record that decision afterwards. The audit chain's own section
    serialises the appends in between, but its domain is the audit dir and its
    subject is the chain head; it says nothing about whether two callers reached
    the same decision from the same read. This lock's domain is the bridge root
    and its subject is one ``trigger_id`` or one ``event_id``, so the read, the
    decision, and the record of it are one section against every other thread
    and every other process.

    Scoped per identity rather than per root on purpose: two workers
    adjudicating *different* triggers never contend, which is the same property
    the one-file-per-id ledger layout was chosen for. One lock file accompanies
    each ledger or cache entry, so the directory's file count keeps the order it
    already had.

    Blocking ``flock(LOCK_EX)``, matching the chain's append lock: a waiter
    waits rather than polling, so a contended decision is delayed and never
    dropped. Falls back to a no-op on platforms without ``fcntl`` (Windows),
    where the in-process guard remains the only ordering, exactly as the chain
    append lock degrades.

    Args:
        lock_path: The lock file standing for the identity being adjudicated.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        stat_result = os.fstat(fd)
        lock_key = f"{stat_result.st_dev}:{stat_result.st_ino}"
        depths: dict[str, int] | None = getattr(_DECISION_DEPTH, "depths", None)
        if depths is None:
            depths = {}
            _DECISION_DEPTH.depths = depths
        if depths.get(lock_key, 0):
            # Already inside this thread's section: the outermost frame holds
            # the flock, so re-taking it would block on ourselves.
            yield
            return
        with _decision_guard(lock_key):
            depths[lock_key] = 1
            try:
                if fcntl is None:  # pragma: no cover - Windows path
                    yield
                    return
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                del depths[lock_key]
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Deterministic task-graph projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskGraphNode:
    """One node of the canonical task graph a trigger payload projects.

    Attributes:
        node_id: Content-addressed id derived from the node body; stable
            across runs so two operators compare graphs by value.
        title: Task title.
        description: Task description.
        role: Worker role; normalised to a known role.
        priority: Task priority as a decimal string.
        depends_on: Node ids this node waits on.
    """

    node_id: str
    title: str
    description: str
    role: str
    priority: str
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class TaskGraphProjection:
    """The canonical task graph a trigger payload projects.

    Attributes:
        nodes: Graph nodes in canonical order.
        graph_digest: Content hash over the canonical node list. Two operators
            firing identical payloads carry receipts bearing this same value.
    """

    nodes: tuple[TaskGraphNode, ...]
    graph_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": AUTOMATION_BRIDGE_SCHEMA_VERSION,
            "nodes": [node.to_dict() for node in self.nodes],
            "graph_digest": self.graph_digest,
        }


def _normalise_role(raw: Any) -> str:
    role = str(raw or "").strip().lower()
    return role if role in _ROLES else _DEFAULT_ROLE


def _normalise_priority(raw: Any) -> str:
    text = str(raw if raw is not None else "").strip()
    return text if text.isdigit() else _DEFAULT_PRIORITY


def _node_id(body: dict[str, Any]) -> str:
    """Return a content-addressed node id (no clock, no random source)."""
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()[:16]


def project_task_graph(*, platform: str, intent: dict[str, Any]) -> TaskGraphProjection:
    """Project a normalised trigger intent onto a canonical task graph.

    Pure: the result depends only on ``platform`` and the *values* in
    ``intent``, never on key insertion order, the clock, or a random source. A
    ``steps`` list projects one node per step, chained in list order so the
    dependency edges are themselves deterministic; otherwise the intent
    projects a single node.

    Args:
        platform: The automation platform label the trigger arrived from.
        intent: The normalised trigger intent (see the platform adapters).

    Returns:
        The canonical :class:`TaskGraphProjection`.
    """
    steps = intent.get("steps")
    raw_nodes: list[dict[str, Any]]
    if isinstance(steps, list) and steps:
        raw_nodes = [step if isinstance(step, dict) else {"title": str(step)} for step in steps]
    else:
        raw_nodes = [intent]

    nodes: list[TaskGraphNode] = []
    previous_id = ""
    for index, raw in enumerate(raw_nodes):
        body = {
            "v": AUTOMATION_BRIDGE_SCHEMA_VERSION,
            "platform": platform,
            "index": index,
            "title": str(raw.get("title", "") or ""),
            "description": str(raw.get("description", "") or ""),
            "role": _normalise_role(raw.get("role")),
            "priority": _normalise_priority(raw.get("priority")),
        }
        depends_on = (previous_id,) if previous_id else ()
        node = TaskGraphNode(
            node_id=_node_id(body | {"depends_on": list(depends_on)}),
            title=body["title"],
            description=body["description"],
            role=body["role"],
            priority=body["priority"],
            depends_on=depends_on,
        )
        nodes.append(node)
        previous_id = node.node_id

    digest = _sha256_bytes(
        _canonical_bytes(
            {
                "v": AUTOMATION_BRIDGE_SCHEMA_VERSION,
                "platform": platform,
                "nodes": [node.to_dict() for node in nodes],
            }
        )
    )
    return TaskGraphProjection(nodes=tuple(nodes), graph_digest=digest)


# ---------------------------------------------------------------------------
# Trigger receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerReceipt:
    """Signed, chain-anchored receipt for one inbound automation trigger.

    Attributes:
        trigger_id: The caller-supplied trigger id; the replay nonce.
        platform: The automation platform label the trigger arrived from.
        request_path: The request path the trigger was fired at.
        payload_digest: Content hash of the raw trigger body.
        graph_digest: Digest of the canonical task graph the payload projects;
            empty on a refusal, which projects nothing.
        scope: The scope granted to the admitted trigger.
        outcome: :data:`TRIGGER_OUTCOME_ADMITTED` or
            :data:`TRIGGER_OUTCOME_REFUSED`.
        refusal_reason: Why the trigger was refused; empty when admitted.
        admission_chain_head: The audit-chain head read at admission time.
        replay_protected: Whether the trigger id was a caller-supplied nonce
            checked against the replay ledger. ``False`` means the id was
            derived from the request, so a genuinely repeated request cannot be
            told apart from a replayed one. Part of the signed binding, so the
            platform's stored copy states plainly how strong its own guarantee
            is rather than implying one it does not have.
        timestamp: Integer timestamp; caller-supplied so identical fixtures
            anchor byte-identically.
        task_ids: Task ids the admission created, when known.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        chain_entry_hash: The audit-chain entry HMAC anchoring the receipt.
    """

    trigger_id: str
    platform: str
    request_path: str
    payload_digest: str
    graph_digest: str = ""
    scope: str = ""
    outcome: str = TRIGGER_OUTCOME_ADMITTED
    refusal_reason: str = ""
    admission_chain_head: str = ""
    replay_protected: bool = True
    timestamp: int = 0
    task_ids: tuple[str, ...] = ()
    signer_public_key_pem: str = ""
    signature: str = ""
    chain_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the signed binding (everything but the signature and anchor)."""
        return {
            "v": AUTOMATION_BRIDGE_SCHEMA_VERSION,
            "kind": "trigger_receipt",
            "trigger_id": self.trigger_id,
            "platform": self.platform,
            "request_path": self.request_path,
            "payload_digest": self.payload_digest,
            "graph_digest": self.graph_digest,
            "scope": self.scope,
            "outcome": self.outcome,
            "refusal_reason": self.refusal_reason,
            "admission_chain_head": self.admission_chain_head,
            "replay_protected": self.replay_protected,
            "timestamp": self.timestamp,
            "task_ids": list(self.task_ids),
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (signed + chain-hashed)."""
        return _canonical_bytes(self._binding())

    def binding_digest(self) -> str:
        """Return the content hash of the signed binding."""
        return _sha256_bytes(self.to_canonical_bytes())

    def to_dict(self) -> dict[str, Any]:
        """Return the receipt as the platform stores it."""
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "chain_entry_hash": self.chain_entry_hash,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> TriggerReceipt:
        """Rebuild a receipt from its stored form.

        Raises:
            AutomationBridgeError: When a required field is missing or the
                document is not a trigger receipt.
        """
        try:
            return cls(
                trigger_id=str(row["trigger_id"]),
                platform=str(row["platform"]),
                request_path=str(row["request_path"]),
                payload_digest=str(row["payload_digest"]),
                graph_digest=str(row.get("graph_digest", "")),
                scope=str(row.get("scope", "")),
                outcome=str(row.get("outcome", TRIGGER_OUTCOME_ADMITTED)),
                refusal_reason=str(row.get("refusal_reason", "")),
                admission_chain_head=str(row.get("admission_chain_head", "")),
                replay_protected=bool(row.get("replay_protected", True)),
                timestamp=int(row.get("timestamp", 0)),
                task_ids=tuple(str(t) for t in row.get("task_ids", ())),
                signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
                signature=str(row.get("signature", "")),
                chain_entry_hash=str(row.get("chain_entry_hash", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AutomationBridgeError(f"malformed trigger receipt: {exc}") from exc


@dataclass(frozen=True)
class TriggerAdmission:
    """Outcome of :func:`admit_trigger`.

    Attributes:
        receipt: The signed receipt, whether the trigger was admitted or not.
            ``None`` only when an unauthenticated refusal exceeded the refusal
            budget, in which case the trigger is still refused and the
            suppression is counted onto the next anchored refusal.
        admitted: ``True`` when the trigger passed authentication and replay
            checks; ``False`` when it was refused.
        graph: The projected task graph, or ``None`` on a refusal.
    """

    receipt: TriggerReceipt | None
    admitted: bool
    graph: TaskGraphProjection | None = None

    @property
    def refusal_reason(self) -> str:
        """Return why the trigger was refused; empty when admitted."""
        return self.receipt.refusal_reason if self.receipt is not None else REFUSAL_UNAUTHENTICATED


class TriggerNonceLedger:
    """Replay ledger of trigger ids the bridge has already admitted.

    Backed by one file per trigger id so two workers admitting distinct
    triggers never contend, and an admitted id survives a restart. The id is
    content-hashed into the filename, so a hostile id cannot escape the ledger
    directory.
    """

    def __init__(self, root: Path) -> None:
        self._dir = root / _TRIGGERS_SUBDIR

    def path_for(self, trigger_id: str) -> Path:
        """Return the ledger path recording ``trigger_id``."""
        return self._dir / f"{_safe_name(trigger_id)}.json"

    def decision_lock(self, trigger_id: str) -> contextlib.AbstractContextManager[None]:
        """Return the section that makes one id's admission decision exclusive.

        :meth:`seen` and :meth:`remember` are a check and an act. Held apart,
        two deliveries of one id both read "not admitted" and both take the
        admitted branch, so the nonce stops being a nonce while the receipt
        still claims ``replay_protected=True``. Callers hold this across both
        halves; the lock's domain is the bridge root, which is a different
        domain from the audit chain's.
        """
        return _decision_lock(self._dir / f"{_safe_name(trigger_id)}.lock")

    def seen(self, trigger_id: str) -> bool:
        """Return whether ``trigger_id`` was already admitted.

        Only meaningful as a decision inside :meth:`decision_lock`; outside it
        the answer can be stale before the caller acts on it.
        """
        return self.path_for(trigger_id).is_file()

    def remember(self, trigger_id: str, receipt: TriggerReceipt) -> None:
        """Record ``trigger_id`` as admitted, storing its receipt."""
        path = self.path_for(trigger_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(receipt.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    def read(self, trigger_id: str) -> TriggerReceipt | None:
        """Return the receipt recorded for ``trigger_id``, or ``None``."""
        path = self.path_for(trigger_id)
        if not path.is_file():
            return None
        try:
            return TriggerReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (AutomationBridgeError, json.JSONDecodeError, OSError):
            logger.warning("automation bridge: malformed trigger receipt at %s", sanitize_log(str(path)))
            return None


@dataclass(frozen=True)
class RefusalBudget:
    """Bounds how many unauthenticated refusals get their own chain entry.

    Recording every refusal is the point: a turned-away trigger must leave a
    record. But the refusal path is reachable without the shared secret, so an
    unbounded signed-append-per-bad-request would let anonymous traffic grow the
    audit chain at will. The budget caps how many refusals are anchored
    individually within a window and counts the rest, so the next anchored
    refusal carries a ``suppressed`` count. The chain therefore never hides that
    refusals happened, and never grows without bound because they did.

    Replayed triggers are not budgeted: producing one requires a valid
    signature, so that path is already gated by possession of the secret.

    Attributes:
        root: Bridge state root; the counter lives under it.
        limit: Refusals anchored individually per window.
        window_s: Window length in seconds.
    """

    root: Path
    limit: int = 60
    window_s: int = 60

    @property
    def _path(self) -> Path:
        return self.root / "refusal-budget.json"

    def take(self, now: int) -> tuple[bool, int]:
        """Consume one refusal from the budget.

        Args:
            now: Current time in Unix seconds.

        Returns:
            ``(anchor, suppressed)`` -- whether this refusal should be anchored
            individually, and how many refusals were suppressed since the last
            anchored one (non-zero only on the first anchored refusal of a new
            window).
        """
        state = self._read()
        window_start = int(state.get("window_start", 0))
        count = int(state.get("count", 0))
        suppressed = int(state.get("suppressed", 0))

        if now - window_start >= self.window_s:
            self._write({"window_start": now, "count": 1, "suppressed": 0})
            return True, suppressed
        if count < self.limit:
            self._write({"window_start": window_start, "count": count + 1, "suppressed": suppressed})
            return True, 0
        self._write({"window_start": window_start, "count": count, "suppressed": suppressed + 1})
        return False, 0

    def _read(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _chain_store(audit_dir: Path, hmac_key: bytes) -> AuditChainStore:
    from bernstein.core.security.audit_chain import AuditChainStore

    return AuditChainStore(audit_dir, key=hmac_key)


def admit_trigger(
    *,
    root: Path,
    audit_dir: Path,
    hmac_key: bytes,
    platform: str,
    request_path: str,
    trigger_id: str,
    body: bytes,
    scope: str,
    timestamp: int,
    authenticated: bool = True,
    refusal_reason: str = "",
    enforce_replay: bool = True,
    budget: RefusalBudget | None = None,
    intent: dict[str, Any] | None = None,
    task_ids: tuple[str, ...] = (),
) -> TriggerAdmission:
    """Admit or refuse one inbound trigger, minting a signed receipt either way.

    An unauthenticated trigger, or one whose ``trigger_id`` was already
    admitted, is refused -- and the refusal is itself a signed receipt anchored
    in the audit chain, so the negative path leaves a record rather than a
    silent drop. An admitted trigger projects the canonical task graph and binds
    its digest into the receipt, which is what makes "we fired the identical
    graph" checkable between two operators.

    Args:
        root: Bridge state root (receipts, nonce ledger, install identity).
        audit_dir: Audit-chain directory.
        hmac_key: The audit-chain HMAC key.
        platform: The automation platform label.
        request_path: The request path the trigger was fired at.
        trigger_id: The caller-supplied trigger id; the replay nonce.
        body: Raw trigger body bytes.
        scope: The scope granted to the admitted trigger.
        timestamp: Integer timestamp recorded on the receipt.
        authenticated: ``False`` when upstream authentication already failed.
        refusal_reason: Reason to record when ``authenticated`` is ``False``.
        enforce_replay: Whether ``trigger_id`` is a caller-supplied nonce that
            should be checked against the replay ledger. Pass ``False`` when the
            id was derived from the request itself: a derived id cannot
            distinguish a replay from a genuinely repeated request, and refusing
            on it would turn a legitimate re-fire into an error. The receipt
            records which regime applied, so the platform's stored copy never
            implies a guarantee it does not carry.
        budget: Optional cap on individually anchored unauthenticated refusals.
            The refusal path is reachable without the shared secret, so an
            unbounded append-per-bad-request would let anonymous traffic grow
            the chain at will. Over budget the trigger is still refused, but no
            receipt is minted and the suppression is counted onto the next
            anchored refusal.
        intent: Normalised trigger intent to project; defaults to the decoded
            body when it is a JSON object.
        task_ids: Task ids the admission created, when already known.

    Returns:
        A :class:`TriggerAdmission`.

    Raises:
        AutomationBridgeError: When ``trigger_id`` is empty.
    """
    if not trigger_id:
        raise AutomationBridgeError("trigger_id is required to mint a receipt")

    ledger = TriggerNonceLedger(root)
    payload_digest = compute_payload_digest(body)

    # The replay decision and the ledger entry recording it are one section, so
    # a doubly-delivered trigger id cannot be read as unseen twice. Only the
    # regime that actually consults the ledger takes the section: an
    # unauthenticated trigger is refused before the ledger is reached, and an
    # unenforced replay regime never reads or writes it, so neither waits behind
    # a decision it does not make.
    adjudicating_nonce = enforce_replay and authenticated
    nonce_section = ledger.decision_lock(trigger_id) if adjudicating_nonce else contextlib.nullcontext()

    with nonce_section:
        reason = ""
        if not authenticated:
            reason = refusal_reason or REFUSAL_UNAUTHENTICATED
        elif enforce_replay and ledger.seen(trigger_id):
            reason = REFUSAL_REPLAYED_TRIGGER

        # Only the unauthenticated path is budgeted. Producing a replay refusal
        # requires a valid signature, so that path is already gated by the secret.
        suppressed = 0
        if reason and not authenticated and budget is not None:
            anchor, suppressed = budget.take(timestamp)
            if not anchor:
                logger.warning(
                    "automation bridge: refusal budget exhausted; refusing %s without anchoring a receipt",
                    sanitize_log(request_path),
                )
                return TriggerAdmission(receipt=None, admitted=False, graph=None)

        graph: TaskGraphProjection | None = None
        graph_digest = ""
        if not reason:
            graph = project_task_graph(platform=platform, intent=intent if intent is not None else _decode_intent(body))
            graph_digest = graph.graph_digest

        outcome = TRIGGER_OUTCOME_REFUSED if reason else TRIGGER_OUTCOME_ADMITTED
        chain = _chain_store(audit_dir, hmac_key)

        # Loading the bridge identity may create and persist a keypair; it does
        # not depend on the chain head, so it stays outside the chain section
        # below rather than holding the chain against every other writer while
        # it runs. It stays inside the nonce section, which is scoped to this
        # trigger id alone and so blocks nobody else's admission.
        private_pem, public_pem = load_or_create_bridge_identity(root)
        from bernstein.core.security.audit_chain import record_trigger_receipt
        from bernstein.core.skills.catalog.signature import sign_payload

        # Reading the head, signing it, and appending the record that sits on it
        # are one atomic section. Split apart, a writer appending during the
        # signature leaves the receipt naming a chain position its own record
        # does not occupy, and the head is opaque payload to the signature so
        # nothing downstream can tell. The head is read from disk here, not from
        # the store's cache, so another process's appends are seen too.
        with chain.chain_transaction():
            unsigned = TriggerReceipt(
                trigger_id=trigger_id,
                platform=platform,
                request_path=request_path,
                payload_digest=payload_digest,
                graph_digest=graph_digest,
                scope="" if reason else scope,
                outcome=outcome,
                refusal_reason=reason,
                # The head this trigger was adjudicated at, and part of the
                # signed binding: the record appended below chains onto it.
                admission_chain_head=chain.resync_head(),
                replay_protected=enforce_replay,
                timestamp=timestamp,
                task_ids=() if reason else task_ids,
            )

            signature = sign_payload(unsigned.to_canonical_bytes(), private_pem)

            event = record_trigger_receipt(
                chain=chain,
                trigger_id=trigger_id,
                platform=platform,
                request_path=request_path,
                payload_digest=payload_digest,
                graph_digest=graph_digest,
                scope=unsigned.scope,
                outcome=outcome,
                receipt_digest=unsigned.binding_digest(),
                refusal_reason=reason,
                suppressed_refusals=suppressed,
            )

        receipt = TriggerReceipt(
            trigger_id=unsigned.trigger_id,
            platform=unsigned.platform,
            request_path=unsigned.request_path,
            payload_digest=unsigned.payload_digest,
            graph_digest=unsigned.graph_digest,
            scope=unsigned.scope,
            outcome=unsigned.outcome,
            refusal_reason=unsigned.refusal_reason,
            admission_chain_head=unsigned.admission_chain_head,
            replay_protected=unsigned.replay_protected,
            timestamp=unsigned.timestamp,
            task_ids=unsigned.task_ids,
            signer_public_key_pem=public_pem,
            signature=signature,
            chain_entry_hash=event.hmac,
        )
        # Inside the nonce section: the id becomes seen before the next
        # delivery of it can read the ledger, which is what the check above
        # then decides on.
        if not reason and enforce_replay:
            ledger.remember(trigger_id, receipt)
        return TriggerAdmission(receipt=receipt, admitted=not reason, graph=graph)


def _decode_intent(body: bytes) -> dict[str, Any]:
    """Return the decoded trigger body as a mapping, or an empty intent."""
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


# ---------------------------------------------------------------------------
# Status proofs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusProof:
    """Signed, chain-anchored proof travelling with an outbound status callback.

    Attributes:
        event_id: The notification event id the callback carries.
        run_id: The run the status belongs to.
        status: The reported status.
        producing_event_digest: Content hash of the canonical notification
            payload that produced the status.
        chain_head: The audit-chain head read at emission.
        timestamp: Integer timestamp recorded on the proof.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        chain_entry_hash: The audit-chain entry HMAC anchoring the proof.
    """

    event_id: str
    run_id: str
    status: str
    producing_event_digest: str
    chain_head: str = ""
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""
    chain_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        return {
            "v": AUTOMATION_BRIDGE_SCHEMA_VERSION,
            "kind": "status_proof",
            "event_id": self.event_id,
            "run_id": self.run_id,
            "status": self.status,
            "producing_event_digest": self.producing_event_digest,
            "chain_head": self.chain_head,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (signed + chain-hashed)."""
        return _canonical_bytes(self._binding())

    def binding_digest(self) -> str:
        """Return the content hash of the signed binding."""
        return _sha256_bytes(self.to_canonical_bytes())

    def to_dict(self) -> dict[str, Any]:
        """Return the proof as it travels inside the callback envelope."""
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "chain_entry_hash": self.chain_entry_hash,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> StatusProof:
        """Rebuild a proof from its delivered form.

        Raises:
            AutomationBridgeError: When a required field is missing.
        """
        try:
            return cls(
                event_id=str(row["event_id"]),
                run_id=str(row.get("run_id", "")),
                status=str(row["status"]),
                producing_event_digest=str(row["producing_event_digest"]),
                chain_head=str(row.get("chain_head", "")),
                timestamp=int(row.get("timestamp", 0)),
                signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
                signature=str(row.get("signature", "")),
                chain_entry_hash=str(row.get("chain_entry_hash", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AutomationBridgeError(f"malformed status proof: {exc}") from exc


def status_proof_path(root: Path, event_id: str) -> Path:
    """Return the on-disk path caching the proof minted for ``event_id``."""
    return root / _STATUS_SUBDIR / f"{_safe_name(event_id)}.json"


def _status_proof_lock_path(root: Path, event_id: str) -> Path:
    """Return the lock file guarding the mint of ``event_id``'s proof."""
    return root / _STATUS_SUBDIR / f"{_safe_name(event_id)}.lock"


def _cached_status_proof(path: Path, *, report_malformed: bool) -> StatusProof | None:
    """Return the proof cached at ``path``, or ``None`` when there is none to use.

    A cache file that cannot be parsed is treated as absent so the callback is
    still answered, and is reported only once per emission: the probe outside
    the mint section is best-effort, and re-reporting it inside would log the
    same file twice for one call.
    """
    if not path.is_file():
        return None
    try:
        return StatusProof.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (AutomationBridgeError, json.JSONDecodeError, OSError):
        if report_malformed:
            logger.warning("automation bridge: malformed status proof at %s", sanitize_log(str(path)))
        return None


def derive_status(payload: dict[str, Any]) -> str:
    """Return the status a notification payload reports.

    Prefers an explicit ``details.status``, then the event severity, so the
    reported status is a projection of the payload rather than a free choice.
    """
    details = payload.get("details")
    if isinstance(details, dict):
        explicit = details.get("status")
        if explicit:
            return str(explicit)
    severity = str(payload.get("severity", "") or "")
    return severity or "unknown"


def emit_status_proof(
    *,
    root: Path,
    audit_dir: Path,
    hmac_key: bytes,
    payload: dict[str, Any],
    status: str = "",
    timestamp: int = 0,
) -> StatusProof:
    """Mint (or re-return) the chain-anchored proof for one status callback.

    The proof is cached per ``event_id``: a callback re-sent after a transient
    delivery failure returns the recorded proof, so the retried envelope is
    byte-identical rather than a second, differently-anchored claim.

    That holds under concurrency too. The cache probe, the mint, and the cache
    write are one section per ``event_id``, so two callbacks in flight for one
    event anchor one ``status.proof.emitted`` row between them and are handed
    the same proof: the second waits for the first and then reads what the
    first recorded, instead of minting a second anchor and overwriting the
    proof its peer already holds. A cache hit is answered before the section is
    entered, so the common re-send costs no lock and touches nothing.

    Args:
        root: Bridge state root.
        audit_dir: Audit-chain directory.
        hmac_key: The audit-chain HMAC key.
        payload: The notification payload the callback carries.
        status: The status to report; derived from the payload when omitted.
        timestamp: Integer timestamp recorded on the proof.

    Returns:
        The signed, anchored :class:`StatusProof`.

    Raises:
        AutomationBridgeError: When the payload carries no ``event_id``.
    """
    event_id = str(payload.get("event_id", "") or "")
    if not event_id:
        raise AutomationBridgeError("status payload has no event_id to anchor")

    cached_path = status_proof_path(root, event_id)
    # Answered before the section: a re-send of an already-proved event takes
    # no lock, creates nothing, and so still works where the bridge root is
    # mounted read-only.
    already_proved = _cached_status_proof(cached_path, report_malformed=False)
    if already_proved is not None:
        return already_proved

    effective_status = status or derive_status(payload)
    run_id = str(payload.get("run_id", "") or "")
    chain = _chain_store(audit_dir, hmac_key)

    # The probe, the mint, and the cache write are one section per event id, so
    # one event id yields one anchor. Held apart, two callbacks both miss the
    # cache, both anchor a ``status.proof.emitted`` row, and the later write
    # replaces the proof the earlier caller was already handed -- leaving a peer
    # holding a proof that is not the proof on disk, with both rows honestly
    # chained so nothing reads as tampering.
    with _decision_lock(_status_proof_lock_path(root, event_id)):
        # The waiter re-probes here: by the time it holds the section the first
        # caller has recorded its proof, and returning that is what makes the
        # two envelopes byte-identical.
        recorded = _cached_status_proof(cached_path, report_malformed=True)
        if recorded is not None:
            return recorded

        # Outside the chain section below: no dependency on the chain head, and
        # creating the keypair should not hold the chain against other writers.
        private_pem, public_pem = load_or_create_bridge_identity(root)
        from bernstein.core.security.audit_chain import record_status_proof
        from bernstein.core.skills.catalog.signature import sign_payload

        # One atomic section for the same reason as the trigger path: the head
        # is signed into the proof, so it has to be the head the proof's own
        # record ends up chained onto, and it is read from disk rather than from
        # the store's per-instance cache.
        with chain.chain_transaction():
            unsigned = StatusProof(
                event_id=event_id,
                run_id=run_id,
                status=effective_status,
                producing_event_digest=compute_document_digest(payload),
                chain_head=chain.resync_head(),
                timestamp=timestamp,
            )

            signature = sign_payload(unsigned.to_canonical_bytes(), private_pem)

            event = record_status_proof(
                chain=chain,
                event_id=event_id,
                run_id=run_id,
                status=effective_status,
                producing_event_digest=unsigned.producing_event_digest,
                proof_digest=unsigned.binding_digest(),
            )

        proof = StatusProof(
            event_id=unsigned.event_id,
            run_id=unsigned.run_id,
            status=unsigned.status,
            producing_event_digest=unsigned.producing_event_digest,
            chain_head=unsigned.chain_head,
            timestamp=unsigned.timestamp,
            signer_public_key_pem=public_pem,
            signature=signature,
            chain_entry_hash=event.hmac,
        )
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_text(
            json.dumps(proof.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        return proof


def wrap_status_payload(payload: dict[str, Any], proof: StatusProof) -> dict[str, Any]:
    """Return the callback payload with the proof attached additively.

    Every original key survives verbatim under its original name, so a consumer
    written against the plain payload keeps parsing it unchanged; the proof
    occupies one new key.
    """
    return dict(payload) | {PROOF_ENVELOPE_KEY: proof.to_dict()}


def unwrap_status_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return the carried notification payload, stripped of the proof key."""
    return {k: v for k, v in envelope.items() if k != PROOF_ENVELOPE_KEY}


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptVerification:
    """Outcome of verifying a stored receipt or a delivered status proof.

    Attributes:
        ok: Whether the document verifies against the local chain.
        reason: Why verification failed; empty when ``ok``.
        kind: ``trigger``, ``status``, or ``unknown``.
        outcome: The trigger receipt's outcome, when applicable.
        chain_status: The status the chain actually recorded for the run, when
            the document is a status proof. Reported even on failure so an
            operator handed a doctored callback learns the recorded value.
        details: Extra fields worth surfacing to an operator.
    """

    ok: bool
    reason: str = ""
    kind: str = "unknown"
    outcome: str = ""
    chain_status: str = ""
    details: dict[str, Any] = field(default_factory=dict[str, Any])


def _verify_signature(canonical: bytes, signature: str, public_key_pem: str) -> tuple[bool, str]:
    from bernstein.core.skills.catalog.signature import verify_payload

    if not signature or not public_key_pem:
        return False, "document is unsigned"
    outcome = verify_payload(canonical, signature, public_key_pem, allow_unverified=True)
    if not outcome.verified:
        return False, f"signature does not verify ({outcome.reason})"
    return True, ""


def _chain_rows(audit_dir: Path, hmac_key: bytes, event_type: str) -> list[dict[str, Any]]:
    """Return ``(details, hmac)`` pairs for every chain event of ``event_type``.

    ``include_archived=True`` so re-verification reads the compressed
    ``archive/*.jsonl.gz`` segments as well as the live ones. Retention routinely
    moves an anchoring row across that boundary; a live-only read would report an
    honest, archived receipt as unanchored, which is a false tamper verdict on
    exactly the long-after-the-fact check the receipt exists to answer.
    """
    chain = _chain_store(audit_dir, hmac_key)
    return [
        {"details": event.details, "hmac": event.hmac}
        for event in chain.query(event_type=event_type, include_archived=True)
    ]


def verify_trigger_receipt(
    receipt: TriggerReceipt,
    *,
    audit_dir: Path,
    hmac_key: bytes,
    body: bytes | None = None,
) -> ReceiptVerification:
    """Verify a stored trigger receipt offline against the local chain.

    Checks, in order: the Ed25519 signature over the binding; when the original
    payload is supplied, that it still digests to the receipt's value; and that
    the chain holds an entry whose recorded receipt digest and HMAC match. Any
    single-byte edit to the receipt or to the payload fails one of these.

    Args:
        receipt: The receipt exactly as the platform stored it.
        audit_dir: Audit-chain directory.
        hmac_key: The audit-chain HMAC key.
        body: The original trigger body, when available.

    Returns:
        A :class:`ReceiptVerification`.
    """
    signed, reason = _verify_signature(receipt.to_canonical_bytes(), receipt.signature, receipt.signer_public_key_pem)
    if not signed:
        return ReceiptVerification(ok=False, reason=reason, kind="trigger", outcome=receipt.outcome)

    if body is not None and compute_payload_digest(body) != receipt.payload_digest:
        return ReceiptVerification(
            ok=False,
            reason="payload digest does not match the receipt",
            kind="trigger",
            outcome=receipt.outcome,
        )

    from bernstein.core.security.audit_chain import (
        EVENT_TRIGGER_RECEIPT_ISSUED,
        EVENT_TRIGGER_RECEIPT_REFUSED,
    )

    event_type = (
        EVENT_TRIGGER_RECEIPT_ISSUED if receipt.outcome == TRIGGER_OUTCOME_ADMITTED else EVENT_TRIGGER_RECEIPT_REFUSED
    )
    wanted = receipt.binding_digest()
    for row in _chain_rows(audit_dir, hmac_key, event_type):
        if row["details"].get("receipt_digest") != wanted:
            continue
        if row["hmac"] != receipt.chain_entry_hash:
            return ReceiptVerification(
                ok=False,
                reason="receipt anchor does not match the chain entry over these bytes",
                kind="trigger",
                outcome=receipt.outcome,
            )
        return ReceiptVerification(
            ok=True,
            kind="trigger",
            outcome=receipt.outcome,
            details={
                "trigger_id": receipt.trigger_id,
                "platform": receipt.platform,
                "graph_digest": receipt.graph_digest,
                "refusal_reason": receipt.refusal_reason,
            },
        )
    return ReceiptVerification(
        ok=False,
        reason="receipt is not anchored in the audit chain",
        kind="trigger",
        outcome=receipt.outcome,
    )


def verify_status_proof(
    envelope: dict[str, Any],
    *,
    audit_dir: Path,
    hmac_key: bytes,
) -> ReceiptVerification:
    """Verify a delivered status callback offline against the local chain.

    Checks the Ed25519 signature over the proof binding, that the carried
    payload still digests to the recorded ``producing_event_digest``, that the
    chain holds a matching anchored entry, and that the reported status equals
    the chain's record. A status flipped on the wire fails, and the chain's
    actual recorded status is reported alongside the failure.

    Args:
        envelope: The callback body exactly as the platform received it.
        audit_dir: Audit-chain directory.
        hmac_key: The audit-chain HMAC key.

    Returns:
        A :class:`ReceiptVerification` carrying ``chain_status``.
    """
    raw_proof = envelope.get(PROOF_ENVELOPE_KEY)
    if not isinstance(raw_proof, dict):
        return ReceiptVerification(ok=False, reason="callback carries no status proof", kind="status")
    try:
        proof = StatusProof.from_dict(raw_proof)
    except AutomationBridgeError as exc:
        return ReceiptVerification(ok=False, reason=str(exc), kind="status")

    from bernstein.core.security.audit_chain import EVENT_STATUS_PROOF_EMITTED

    # Resolve the chain's record first so the recorded status is reportable
    # even when the delivered document fails every other check.
    rows = _chain_rows(audit_dir, hmac_key, EVENT_STATUS_PROOF_EMITTED)
    anchored = next((row for row in rows if row["hmac"] == proof.chain_entry_hash), None)
    chain_status = str(anchored["details"].get("status", "")) if anchored else ""

    signed, reason = _verify_signature(proof.to_canonical_bytes(), proof.signature, proof.signer_public_key_pem)
    if not signed:
        return ReceiptVerification(ok=False, reason=reason, kind="status", chain_status=chain_status)

    carried = unwrap_status_payload(envelope)
    if compute_document_digest(carried) != proof.producing_event_digest:
        return ReceiptVerification(
            ok=False,
            reason="producing event digest does not match the delivered payload",
            kind="status",
            chain_status=chain_status,
        )

    if anchored is None:
        return ReceiptVerification(
            ok=False,
            reason="status proof is not anchored in the audit chain",
            kind="status",
            chain_status=chain_status,
        )
    if anchored["details"].get("proof_digest") != proof.binding_digest():
        return ReceiptVerification(
            ok=False,
            reason="proof anchor does not match the chain entry over these bytes",
            kind="status",
            chain_status=chain_status,
        )
    if chain_status != proof.status:
        return ReceiptVerification(
            ok=False,
            reason=f"reported status {proof.status!r} is not the status the chain recorded",
            kind="status",
            chain_status=chain_status,
        )
    return ReceiptVerification(
        ok=True,
        kind="status",
        chain_status=chain_status,
        details={"event_id": proof.event_id, "run_id": proof.run_id},
    )


def verify_receipt_document(
    document: dict[str, Any],
    *,
    audit_dir: Path,
    hmac_key: bytes,
    body: bytes | None = None,
) -> ReceiptVerification:
    """Verify a stored trigger receipt or a delivered status callback.

    Dispatches on the document shape so an operator runs one command over
    whatever the automation platform handed them.

    Args:
        document: The stored receipt or the received callback envelope.
        audit_dir: Audit-chain directory.
        hmac_key: The audit-chain HMAC key.
        body: The original trigger body, when verifying a trigger receipt.

    Returns:
        A :class:`ReceiptVerification`.
    """
    if PROOF_ENVELOPE_KEY in document:
        return verify_status_proof(document, audit_dir=audit_dir, hmac_key=hmac_key)
    if document.get("kind") == "status_proof":
        return verify_status_proof({PROOF_ENVELOPE_KEY: document}, audit_dir=audit_dir, hmac_key=hmac_key)
    if document.get("kind") == "trigger_receipt" or "payload_digest" in document:
        try:
            receipt = TriggerReceipt.from_dict(document)
        except AutomationBridgeError as exc:
            return ReceiptVerification(ok=False, reason=str(exc), kind="trigger")
        return verify_trigger_receipt(receipt, audit_dir=audit_dir, hmac_key=hmac_key, body=body)
    return ReceiptVerification(
        ok=False,
        reason="document is neither a trigger receipt nor a status proof",
        kind="unknown",
    )


__all__ = [
    "AUTOMATION_BRIDGE_SCHEMA_VERSION",
    "BRIDGE_ROOT_ENV",
    "PROOF_ENVELOPE_KEY",
    "REFUSAL_MALFORMED_TRIGGER",
    "REFUSAL_REPLAYED_TRIGGER",
    "REFUSAL_STALE_TIMESTAMP",
    "REFUSAL_UNAUTHENTICATED",
    "TRIGGER_OUTCOME_ADMITTED",
    "TRIGGER_OUTCOME_REFUSED",
    "AutomationBridgeError",
    "ReceiptVerification",
    "RefusalBudget",
    "StatusProof",
    "TaskGraphNode",
    "TaskGraphProjection",
    "TriggerAdmission",
    "TriggerNonceLedger",
    "TriggerReceipt",
    "admit_trigger",
    "bridge_root",
    "compute_document_digest",
    "compute_payload_digest",
    "derive_status",
    "emit_status_proof",
    "load_or_create_bridge_identity",
    "project_task_graph",
    "status_proof_path",
    "unwrap_status_payload",
    "verify_receipt_document",
    "verify_status_proof",
    "verify_trigger_receipt",
    "wrap_status_payload",
]
