"""Placement as a receipt: the pool is a declared selector input (#2547).

The selector (:mod:`bernstein.core.sandbox.selector`) stays a pure function.
This module composes it with a pool: :func:`pool_to_sandbox_policy` maps a
:class:`~bernstein.core.sandbox.pool.PoolMergeResult` into a
:class:`~bernstein.core.sandbox.selector.SandboxPolicy`, and
:func:`select_pool_backend` restricts the candidate backends to the pool
allowlist before delegating to :func:`~bernstein.core.sandbox.selector.select_sandbox`.
``select_sandbox`` itself is untouched, so with no pool configured selection is
byte-identical to today (AC: regression).

Every dispatch seals a :class:`PlacementReceipt` over
``(pool_hash, template_hash, overrides_hash, effective_manifest_hash,
selector_inputs, chosen_backend)``, following the sealed canonical-JSON receipt
pattern in :mod:`bernstein.core.cost.scheduling.receipt`. The receipt's identity
is its own ``placement_hash``; :func:`verify_placement_receipt` recomputes it
from the stored body so a forged backend or a widened effective manifest
recomputes to a different hash and fails, exactly like a tampered chain entry.
When the receipt is also mirrored into the HMAC audit chain, flipping one byte
of the recorded effective manifest breaks chain verification (AC: verifiability).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.sandbox.selector import (
    SandboxEnvironment,
    SandboxPolicy,
    select_sandbox,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from bernstein.core.sandbox.backend import SandboxBackend
    from bernstein.core.sandbox.pool import PoolManifest, PoolMergeResult
    from bernstein.core.security.audit_chain import AuditChainStore

#: Wire-format version stamped into every placement receipt.
PLACEMENT_RECEIPT_SCHEMA_VERSION = 1

_PLACEMENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEMENT_SUBPATH = ("sandbox", "placements")


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pool_to_sandbox_policy(
    merge: PoolMergeResult,
    *,
    pool: PoolManifest,
    allow_paid: bool = False,
) -> SandboxPolicy:
    """Map a merged pool placement into a pure :class:`SandboxPolicy`.

    The pool becomes a declared input to selection: the required capabilities
    are the merge's effective capabilities, the precedence is the pool backend
    allowlist (so operator preference order wins), and an exposed ``backend``
    override becomes the policy override.

    Args:
        merge: The deterministic merge result for this dispatch.
        pool: The pool manifest the merge targeted (for the allowlist order).
        allow_paid: Whether paid cloud backends may be considered.

    Returns:
        A :class:`SandboxPolicy` the pure selector consumes.
    """
    precedence = tuple(pool.backend_allowlist) if pool.backend_allowlist else None
    return SandboxPolicy(
        override=merge.backend_override,
        allow_paid=allow_paid,
        required_capabilities=frozenset(merge.capabilities),
        precedence=precedence,
    )


def selector_inputs(
    merge: PoolMergeResult,
    *,
    pool: PoolManifest,
    environment: SandboxEnvironment,
    allow_paid: bool,
    candidate_names: Iterable[str],
) -> dict[str, Any]:
    """Return the canonical, JSON-safe record of the inputs the pick used.

    Every value that could change the chosen backend is captured so a verifier
    can prove the selection was a pure function of these inputs.
    """
    return {
        "pool_hash": merge.pool_hash,
        "backend_allowlist": list(pool.backend_allowlist),
        "backend_override": merge.backend_override,
        "required_capabilities": sorted(c.value for c in merge.capabilities),
        "network_egress_class": merge.network_egress_class,
        "allow_paid": bool(allow_paid),
        "available_credentials": sorted(environment.available_credentials),
        "budget_remaining_usd": environment.budget_remaining_usd,
        "candidates": sorted(candidate_names),
    }


def select_pool_backend(
    backends: Iterable[SandboxBackend],
    merge: PoolMergeResult,
    *,
    pool: PoolManifest,
    environment: SandboxEnvironment | None = None,
    allow_paid: bool = False,
) -> tuple[SandboxBackend, dict[str, Any]]:
    """Select a backend for a pool dispatch and return the inputs it used.

    Candidates are first filtered to the pool backend allowlist (empty
    allowlist means "any registered backend"), then handed to the pure
    :func:`~bernstein.core.sandbox.selector.select_sandbox`. Two hosts with the
    same pool, recipe, and environment choose the same backend.

    Returns:
        ``(chosen_backend, selector_inputs)``.
    """
    env = environment or SandboxEnvironment()
    allowed = set(pool.backend_allowlist)
    candidates = [b for b in backends if not allowed or b.name in allowed]
    policy = pool_to_sandbox_policy(merge, pool=pool, allow_paid=allow_paid)
    chosen = select_sandbox(candidates, policy=policy, environment=env)
    inputs = selector_inputs(
        merge,
        pool=pool,
        environment=env,
        allow_paid=allow_paid,
        candidate_names=[b.name for b in candidates],
    )
    return chosen, inputs


@dataclass(frozen=True)
class PlacementReceipt:
    """A sealed placement decision (#2547).

    The receipt's identity is its ``placement_hash``: a SHA-256 over every other
    field (including the ``selector_inputs`` payload and the chosen backend).
    """

    pool_hash: str
    template_hash: str
    overrides_hash: str
    effective_manifest_hash: str
    chosen_backend: str
    selector_inputs: dict[str, Any]
    schema_version: int = PLACEMENT_RECEIPT_SCHEMA_VERSION
    timestamp: int = 0
    placement_hash: str = ""

    def _body(self) -> dict[str, Any]:
        """Canonical payload the ``placement_hash`` is computed over."""
        return {
            "pool_hash": self.pool_hash,
            "template_hash": self.template_hash,
            "overrides_hash": self.overrides_hash,
            "effective_manifest_hash": self.effective_manifest_hash,
            "chosen_backend": self.chosen_backend,
            "selector_inputs": self.selector_inputs,
            "schema_version": int(self.schema_version),
            "timestamp": int(self.timestamp),
        }

    def compute_hash(self) -> str:
        """SHA-256 over the canonical receipt body."""
        return _sha256_hex(_canonical_json(self._body()))

    def selector_inputs_hash(self) -> str:
        """SHA-256 over just the selector-inputs payload."""
        return _sha256_hex(_canonical_json(self.selector_inputs))

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        body["placement_hash"] = self.placement_hash
        return body

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlacementReceipt:
        return cls(
            pool_hash=str(raw.get("pool_hash", "")),
            template_hash=str(raw.get("template_hash", "")),
            overrides_hash=str(raw.get("overrides_hash", "")),
            effective_manifest_hash=str(raw.get("effective_manifest_hash", "")),
            chosen_backend=str(raw.get("chosen_backend", "")),
            selector_inputs=dict(raw.get("selector_inputs", {}) or {}),
            schema_version=int(raw.get("schema_version", PLACEMENT_RECEIPT_SCHEMA_VERSION)),
            timestamp=int(raw.get("timestamp", 0)),
            placement_hash=str(raw.get("placement_hash", "")),
        )

    def verify_self_hash(self) -> bool:
        """Return whether ``placement_hash`` recomputes from the body."""
        return bool(self.placement_hash) and self.placement_hash == self.compute_hash()


def seal_placement(
    *,
    merge: PoolMergeResult,
    chosen_backend: str,
    selector_inputs: dict[str, Any],
    timestamp: int = 0,
) -> PlacementReceipt:
    """Build a sealed :class:`PlacementReceipt` from a merge and a pick.

    Pure and deterministic: same merge + backend + inputs + timestamp seal
    byte-identically, so two hosts agree on ``placement_hash`` (AC: determinism).
    """
    receipt = PlacementReceipt(
        pool_hash=merge.pool_hash,
        template_hash=merge.template_hash,
        overrides_hash=merge.overrides_hash,
        effective_manifest_hash=merge.effective_manifest_hash,
        chosen_backend=chosen_backend,
        selector_inputs=selector_inputs,
        timestamp=timestamp,
    )
    object.__setattr__(receipt, "placement_hash", receipt.compute_hash())
    return receipt


def placement_receipt_path(workdir: Path, placement_hash: str) -> Path:
    """Return the on-disk receipt path for *placement_hash* under *workdir*.

    The hash is validated against ``<64 hex>`` and the resolved path is asserted
    to stay under the placements directory (path-injection defense in depth).
    """
    if not _PLACEMENT_HASH_RE.match(placement_hash):
        raise ValueError(f"placement_hash is not a canonical sha256 digest: {placement_hash!r}")
    base = workdir.joinpath(".sdd", *_PLACEMENT_SUBPATH)
    candidate = base / f"{placement_hash}.json"
    base_real = os.path.realpath(base)
    cand_real = os.path.realpath(candidate)
    if os.path.commonpath([base_real, cand_real]) != base_real:
        raise ValueError(f"receipt path escapes placements directory: {placement_hash!r}")
    return candidate


def write_placement_receipt(workdir: Path, receipt: PlacementReceipt) -> Path:
    """Write *receipt* to its content-addressed path and return it."""
    path = placement_receipt_path(workdir, receipt.placement_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(receipt.to_canonical_json(), encoding="utf-8")
    return path


def read_placement_receipt(workdir: Path, placement_hash: str) -> PlacementReceipt | None:
    """Return the sealed receipt for *placement_hash*, or ``None`` if absent/bad."""
    try:
        path = placement_receipt_path(workdir, placement_hash)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return PlacementReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PlacementVerifyResult:
    """Outcome of an offline placement-receipt verification."""

    ok: bool
    reason: str
    receipt: PlacementReceipt | None


def verify_placement_receipt(workdir: Path, placement_hash: str) -> PlacementVerifyResult:
    """Re-verify the placement receipt for *placement_hash* offline.

    A forged backend or a widened ``effective_manifest_hash`` recomputes to a
    different ``placement_hash`` and fails, exactly like a tampered chain entry.
    """
    receipt = read_placement_receipt(workdir, placement_hash)
    if receipt is None:
        return PlacementVerifyResult(ok=False, reason=f"no placement receipt for {placement_hash!r}", receipt=None)
    if receipt.placement_hash != placement_hash:
        return PlacementVerifyResult(ok=False, reason="receipt placement_hash does not match request", receipt=receipt)
    if not receipt.verify_self_hash():
        return PlacementVerifyResult(
            ok=False, reason="placement_hash does not recompute from the receipt body (tampered)", receipt=receipt
        )
    return PlacementVerifyResult(ok=True, reason="", receipt=receipt)


def record_placement(
    *,
    chain: AuditChainStore,
    receipt: PlacementReceipt,
) -> None:
    """Mirror a sealed placement receipt into the HMAC audit chain.

    After this, flipping one byte of the recorded effective manifest in the
    chain breaks ``bernstein audit verify`` (AC: verifiability).
    """
    from bernstein.core.security.audit_chain import record_pool_placement_receipt

    record_pool_placement_receipt(
        chain=chain,
        placement_hash=receipt.placement_hash,
        pool_hash=receipt.pool_hash,
        template_hash=receipt.template_hash,
        overrides_hash=receipt.overrides_hash,
        effective_manifest_hash=receipt.effective_manifest_hash,
        chosen_backend=receipt.chosen_backend,
        selector_inputs_hash=receipt.selector_inputs_hash(),
    )


__all__ = [
    "PLACEMENT_RECEIPT_SCHEMA_VERSION",
    "PlacementReceipt",
    "PlacementVerifyResult",
    "placement_receipt_path",
    "pool_to_sandbox_policy",
    "read_placement_receipt",
    "record_placement",
    "seal_placement",
    "select_pool_backend",
    "selector_inputs",
    "verify_placement_receipt",
    "write_placement_receipt",
]
