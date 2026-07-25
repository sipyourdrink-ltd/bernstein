"""Per-hop HMAC-chained delegation receipts (RFC 8693-style token exchange).

A single run authorizes a chain of principals:
``principal -> orchestrator -> sub-agent``. Each hop is an act of delegation
(the principal authorizes the orchestrator; the orchestrator spawns a
sub-agent to act on the principal's behalf). This module records one
*delegation receipt* per hop, HMAC-chained so an auditor can reconstruct
offline exactly which principal authorized which sub-agent action for a run.

Receipt shape
-------------
Each receipt is one JSONL line under ``<root>/delegation/<run_id>.jsonl``::

    {
      "run_id": "...",
      "hop_index": 0,
      "issuer": "principal:alex",     # who is delegating (RFC 8693 subject)
      "subject": "orchestrator",      # the acting party being authorized
      "audience": "sub-agent:backend",# the target the token is minted for
      "act": "task.spawn",            # the delegated action
      "created": 1730000000,
      "prev_hmac": "<hex>",
      "hmac": "<hex>",

      # optional authority binding (absent on receipts written before it
      # existed); see bernstein.core.identity.delegation_scope
      "parent_ref": "<hmac of the hop this descends from>",
      "scope_ref": "sha256:<hex>",    # content address of the granted scope
      "scope": {...},                 # the effective scope, carried inline
      "binding": {"charter_hash": "...", "certificate_version": "..."}
    }

The HMAC covers the previous receipt's HMAC concatenated with the canonical
JSON of this receipt's body (all fields except ``hmac``), using the same
construction as :mod:`bernstein.core.security.audit`. A flipped field, a
deleted hop, or the wrong key surfaces as a linkage/HMAC error in
:func:`verify_run_chain` rather than passing silently.

The key is the install-scoped audit key (``load_or_create_audit_key``) so the
delegation chain shares the install's tamper-evidence anchor: strip the
identity and the chain no longer verifies.

Sequence is not narrowing
-------------------------
The HMAC chain proves the *order and integrity* of a delegation sequence. It
cannot prove that authority narrowed, because until the ``scope`` fields above
existed the authority granted at each hop was never written down - so the only
answer to "did this sub-agent hold more than the worker that spawned it?" was
"the route checks would have refused it", which is an argument rather than a
receipt. A hop that records its effective scope and names its parent lets
:func:`verify_run_chain` recompute the child-subset-of-parent relation from the
receipts alone. The recomputation, the separation-of-duties check, and the
decision-time charter binding live in
:mod:`bernstein.core.identity.delegation_scope`.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac as _hmac
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from bernstein.core.identity.delegation_scope import (
    AuthorityReport,
    DecisionBinding,
    DelegationScope,
    ScopeViolation,
    verify_authority,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

if sys.platform == "win32":
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

__all__ = [
    "DEFAULT_ROOT",
    "GENESIS_HMAC",
    "AuthorityReport",
    "ChainResult",
    "DecisionBinding",
    "DelegationLedger",
    "DelegationReceipt",
    "DelegationScope",
    "ScopeViolation",
    "default_ledger",
    "record_delegation_hop",
    "verify_run",
    "verify_run_chain",
]

#: Genesis linkage value for the first hop of a run (matches the audit-chain
#: convention of a fixed 64-hex-zero anchor).
GENESIS_HMAC: Final[str] = "0" * 64

_SUBDIR: Final[str] = "delegation"

#: Default root for delegation receipts - the same tree as the HMAC-chained
#: audit log so ``.sdd/audit/delegation/`` sits beside ``.sdd/audit/*.jsonl``.
DEFAULT_ROOT: Final[Path] = Path(".sdd/audit")

#: Per-run in-process locks serialising the tail-read-through-append critical
#: section. Keyed by ``run_id`` and shared across every :class:`DelegationLedger`
#: in the process (ledgers are often reconstructed per call via
#: :func:`default_ledger`), so two threads never fork one run's chain. The
#: registry itself is guarded by :data:`_RUN_LOCKS_GUARD`.
_RUN_LOCKS: Final[dict[str, threading.Lock]] = {}
_RUN_LOCKS_GUARD: Final[threading.Lock] = threading.Lock()

#: Receipt fields that are omitted from the signed body when unset, so a hop
#: recorded without authority binding hashes exactly as it did before those
#: fields existed.
_OPTIONAL_BODY_FIELDS: Final[tuple[str, ...]] = ("parent_ref", "scope_ref", "scope", "binding")


def _run_lock(run_id: str) -> threading.Lock:
    """Return the process-wide append lock for ``run_id`` (created on demand)."""
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _RUN_LOCKS[run_id] = lock
        return lock


@dataclass(frozen=True)
class DelegationReceipt:
    """A single HMAC-chained delegation hop.

    The authority fields (``parent_ref``, ``scope_ref``, ``scope``,
    ``binding``) are optional and absent from receipts recorded before they
    existed. They are part of the signed body when present, so the grant a hop
    carried and the charter version it was decided under are covered by the
    receipt HMAC.
    """

    run_id: str
    hop_index: int
    issuer: str
    subject: str
    audience: str
    act: str
    created: int
    prev_hmac: str = GENESIS_HMAC
    hmac: str = ""
    #: HMAC of the hop this one descends from (:data:`GENESIS_HMAC` for a root).
    #: Lets a run record a delegation *tree* in one file: the parent is named,
    #: not assumed to be the preceding line.
    parent_ref: str | None = None
    #: Content address (``sha256:<hex>``) of the effective scope granted here.
    scope_ref: str | None = None
    #: The effective scope body, when carried inline rather than by reference.
    scope: dict[str, Any] | None = None
    #: Charter hash / tenant-certificate version in force at decision time.
    binding: dict[str, Any] | None = None

    def body(self) -> dict[str, Any]:
        """Return the signed body (all fields except ``hmac``).

        Optional authority fields that are unset are omitted entirely, so a
        receipt recorded without them produces byte-identical signing input to
        one written before the fields existed.
        """
        data = asdict(self)
        data.pop("hmac", None)
        for key in _OPTIONAL_BODY_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        return data


def _compute_hmac(key: bytes, prev_hmac: str, body: dict[str, Any]) -> str:
    """HMAC-SHA256 over ``prev_hmac`` concatenated with canonical JSON body.

    Identical construction to :func:`bernstein.core.security.audit._compute_hmac`
    so the delegation chain and the audit chain share tamper-evidence
    semantics.
    """
    payload = prev_hmac + json.dumps(body, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


@dataclass
class ChainResult:
    """Outcome of reconstructing a run's delegation chain offline.

    ``chain_ok`` is the tamper-evidence verdict (linkage plus HMAC), unchanged
    from before authority checks existed. ``valid`` is the overall verdict: the
    chain is intact *and* the recomputed authority checks found nothing. A
    widening hop therefore fails ``bernstein delegation verify`` even though
    every HMAC in the file is correct - which is the point, since the party
    that wrote a widened hop holds the key that seals it.
    """

    valid: bool
    hops: int
    receipts: list[DelegationReceipt] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    chain_ok: bool = True
    authority: AuthorityReport = field(default_factory=lambda: AuthorityReport())

    @property
    def violations(self) -> list[ScopeViolation]:
        """Recomputed authority failures (narrowing, duties, binding)."""
        return self.authority.violations


class DelegationLedger:
    """Append-only, HMAC-chained delegation-receipt store rooted at a dir.

    One JSONL file per run under ``<root>/delegation/``. The ledger tracks
    the running ``prev_hmac`` per run by reading the tail of the file, so
    hops recorded across process restarts still chain continuously.
    """

    def __init__(self, root: Path, key: bytes) -> None:
        """Bind the ledger to a root directory and a chaining key.

        Args:
            root: Base directory; receipts land under ``root/delegation/``.
            key: HMAC key - use the install audit key so the chain is
                anchored to the install identity.
        """
        self.root = Path(root)
        self._key = key
        self._dir = self.root / _SUBDIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def receipt_path(self, run_id: str) -> Path:
        """Return the JSONL path backing ``run_id``'s receipts."""
        safe = run_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.jsonl"

    @contextlib.contextmanager
    def _append_lock(self, run_id: str) -> Iterator[None]:
        """Serialise the tail-read-through-append for ``run_id``.

        Holds two locks for the whole read-modify-write:

        * an in-process :class:`threading.Lock` keyed by ``run_id`` so threads
          in this process never recover the same tail, and
        * an OS ``flock(LOCK_EX)`` on a per-run lock file so separate processes
          (task server, spawner, CLI) extending the same chain are serialised
          too.

        Without both, two writers read the same ``prev_hmac`` and append records
        that fork the HMAC chain, breaking :func:`verify_run_chain` for the run
        (issue #2640). Falls back to the in-process lock alone on platforms
        without ``fcntl`` (Windows), matching how the audit chain degrades.
        """
        with _run_lock(run_id):
            if fcntl is None:  # pragma: no cover - Windows path
                yield
                return
            safe = run_id.replace("/", "_").replace("\\", "_")
            lock_path = self._dir / f"{safe}.jsonl.lock"
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _tail(self, run_id: str) -> tuple[str, int]:
        """Return ``(prev_hmac, next_hop_index)`` for the run.

        Reads the last JSONL line's ``hmac`` as the linkage anchor; a fresh
        run starts from :data:`GENESIS_HMAC` at index 0.
        """
        path = self.receipt_path(run_id)
        if not path.is_file():
            return GENESIS_HMAC, 0
        prev = GENESIS_HMAC
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prev = obj.get("hmac", prev)
            count += 1
        return prev, count

    def record_hop(
        self,
        *,
        run_id: str,
        issuer: str,
        subject: str,
        audience: str,
        act: str,
        created: int | None = None,
        scope: DelegationScope | None = None,
        parent_ref: str | None = None,
        binding: DecisionBinding | None = None,
        inline_scope: bool = True,
    ) -> DelegationReceipt:
        """Append one delegation receipt and return it.

        Args:
            run_id: Run the delegation belongs to.
            issuer: The delegating party (RFC 8693 subject principal).
            subject: The acting party being authorized.
            audience: The target the delegated token is minted for.
            act: Symbolic name of the delegated action.
            created: Optional unix timestamp; defaults to now (exposed for
                deterministic tests).
            scope: The effective authority granted at this hop. When supplied,
                the receipt records its content address and (by default) the
                scope body, so a verifier can recompute child-subset-of-parent
                offline instead of trusting that route checks caught every
                invalid action.
            parent_ref: HMAC of the hop this one descends from. Defaults to the
                preceding hop when a ``scope`` is recorded, which is the linear
                ``principal -> orchestrator -> sub-agent`` case; pass it
                explicitly when a run records a delegation tree.
            binding: Charter hash / tenant-certificate version in force at
                decision time.
            inline_scope: When False the receipt carries only ``scope_ref`` and
                the verifier needs a resolver to fetch the scope body.

        Returns:
            The freshly-appended, HMAC-computed receipt.
        """
        # The tail read, HMAC computation, and append must be one atomic
        # critical section: a concurrent writer that recovers the same tail
        # would embed a stale ``prev_hmac`` and fork the chain (issue #2640).
        with self._append_lock(run_id):
            prev_hmac, hop_index = self._tail(run_id)
            ts = int(created if created is not None else time.time())
            body: dict[str, Any] = {
                "run_id": run_id,
                "hop_index": hop_index,
                "issuer": issuer,
                "subject": subject,
                "audience": audience,
                "act": act,
                "created": ts,
                "prev_hmac": prev_hmac,
            }
            if scope is not None:
                body["parent_ref"] = parent_ref if parent_ref is not None else prev_hmac
                body["scope_ref"] = scope.scope_ref()
                if inline_scope:
                    body["scope"] = scope.to_body()
            elif parent_ref is not None:
                body["parent_ref"] = parent_ref
            if binding is not None:
                body["binding"] = binding.to_body()
            computed = _compute_hmac(self._key, prev_hmac, body)
            entry = body.copy()
            entry["hmac"] = computed
            path = self.receipt_path(run_id)
            with path.open("a", encoding="utf-8", newline="") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return DelegationReceipt(**entry)


def verify_run_chain(
    *,
    root: Path,
    run_id: str,
    key: bytes,
    scope_resolver: Callable[[str], DelegationScope | None] | None = None,
) -> ChainResult:
    """Reconstruct and verify a run's delegation chain offline.

    Walks ``<root>/delegation/<run_id>.jsonl`` from genesis, recomputing each
    hop's HMAC and checking linkage, then recomputes the authority relations
    the receipts record: child-scope-is-a-subset-of-parent-scope per hop,
    separation of duties across principals, and decision-time charter binding.
    Reconstructs the ``principal -> orchestrator -> sub-agent`` ordering (AC4).

    Args:
        root: Base directory the ledger was rooted at.
        run_id: Run to verify.
        key: The HMAC key (install audit key) the receipts were written with.
        scope_resolver: Optional lookup for receipts that carry only a
            content-addressed ``scope_ref`` rather than an inline scope body.

    Returns:
        A :class:`ChainResult`. ``valid`` is True only when at least one hop
        exists, the whole chain verifies from genesis to tail, and every
        authority check passes.
    """
    ledger_dir = Path(root) / _SUBDIR
    safe = run_id.replace("/", "_").replace("\\", "_")
    path = ledger_dir / f"{safe}.jsonl"
    if not path.is_file():
        return ChainResult(valid=False, hops=0, errors=["no delegation receipts for run"], chain_ok=False)

    receipts: list[DelegationReceipt] = []
    errors: list[str] = []
    prev_hmac = GENESIS_HMAC
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"hop {lineno}: malformed JSON: {exc}")
            break
        stored_hmac = obj.get("hmac", "")
        body = {k: v for k, v in obj.items() if k != "hmac"}
        if body.get("prev_hmac") != prev_hmac:
            errors.append(
                f"hop {body.get('hop_index', lineno)}: broken linkage (prev_hmac does not match preceding hop)"
            )
            break
        expected = _compute_hmac(key, prev_hmac, body)
        if not _hmac.compare_digest(expected, stored_hmac):
            errors.append(f"hop {body.get('hop_index', lineno)}: HMAC mismatch (receipt tampered or wrong key)")
            break
        try:
            receipts.append(DelegationReceipt(**obj))
        except TypeError as exc:
            errors.append(f"hop {body.get('hop_index', lineno)}: unknown receipt field: {exc}")
            break
        prev_hmac = stored_hmac

    chain_ok = not errors and len(receipts) > 0
    authority = verify_authority(receipts, scope_resolver=scope_resolver, genesis=GENESIS_HMAC)
    return ChainResult(
        valid=chain_ok and authority.ok,
        hops=len(receipts),
        receipts=receipts,
        errors=errors,
        chain_ok=chain_ok,
        authority=authority,
    )


# ---------------------------------------------------------------------------
# Default-resolving convenience helpers (install-anchored key + audit root)
# ---------------------------------------------------------------------------


def _audit_key() -> bytes:
    """Return the install-scoped audit HMAC key (the delegation chain anchor)."""
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def default_ledger(root: Path | None = None) -> DelegationLedger:
    """Return a ledger rooted at the audit tree, keyed by the install audit key.

    Args:
        root: Optional root override; defaults to :data:`DEFAULT_ROOT`.

    Returns:
        A :class:`DelegationLedger` chained to the install identity's audit key.
    """
    return DelegationLedger(root=root or DEFAULT_ROOT, key=_audit_key())


def record_delegation_hop(
    *,
    run_id: str,
    issuer: str,
    subject: str,
    audience: str,
    act: str,
    root: Path | None = None,
    scope: DelegationScope | None = None,
    parent_ref: str | None = None,
    binding: DecisionBinding | None = None,
) -> DelegationReceipt:
    """Record one delegation hop for ``run_id`` using install-anchored defaults.

    Convenience wrapper the orchestrator calls at each
    ``principal -> orchestrator -> sub-agent`` handoff. Pass ``scope`` to make
    the hop's grant recomputable rather than merely sequenced.
    """
    return default_ledger(root).record_hop(
        run_id=run_id,
        issuer=issuer,
        subject=subject,
        audience=audience,
        act=act,
        scope=scope,
        parent_ref=parent_ref,
        binding=binding,
    )


def verify_run(
    run_id: str,
    *,
    root: Path | None = None,
    scope_resolver: Callable[[str], DelegationScope | None] | None = None,
) -> ChainResult:
    """Verify a run's delegation chain using install-anchored defaults.

    Args:
        run_id: Run to verify.
        root: Optional root override; defaults to :data:`DEFAULT_ROOT`.
        scope_resolver: Optional lookup for content-addressed scope references.

    Returns:
        The :class:`ChainResult` from :func:`verify_run_chain`.
    """
    return verify_run_chain(
        root=root or DEFAULT_ROOT,
        run_id=run_id,
        key=_audit_key(),
        scope_resolver=scope_resolver,
    )
