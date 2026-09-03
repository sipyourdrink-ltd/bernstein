"""Re-decide a grant's preconditions at dispatch, against current chain state.

A grant is decided once, at issue, and then carried. Every fact it rested on
can change before it is spent: the grant is revoked, its capability ceiling is
narrowed, its expiry elapses. The signature over the issued record stays valid
through all of that, so a check that only verifies the signature keeps passing
-- correct cryptography, wrong authorisation. The window is not theoretical: a
long-running fan-out issues grants at the start and spends them over the
following hours.

This module re-decides at the point of use and, when it refuses, says *which
fact changed and where*. A refusal carries a chain position, not a boolean:
"grant ``g`` issued at chain position 0 was superseded by ``grant_revoked`` at
chain position 3". That statement is checkable offline, later, by someone who
was not there -- the same JSONL slice and the install audit key reconstruct it.
The reverse query becomes answerable too: given a grant, everything it
authorised, and the exact record after which it should not have.

Cost
----
The re-decision runs on every dispatch, so it must not walk the chain per call.
:class:`GrantPreconditionIndex` keeps a byte offset into the run's append-only
JSONL and a running ``prev_hmac``, exactly like
:class:`bernstein.core.security.audit.ChainScanCursor` does for the audit
chain. Per dispatch the index reads the bytes appended since the last call --
normally none -- and then answers from a dict lookup and a frozenset membership
test. Authenticating a record (one HMAC, one Ed25519 verification) happens once
per record, never once per call.

Semantics
---------
* **Revocation is monotone.** Once a ``grant_revoked`` record is seen, a later
  ``grant_issued`` record for the same ``grant_id`` cannot resurrect it.
* **Re-issue narrows, it never widens.** The effective ceiling is the
  *intersection* of the ceilings across every ``grant_issued`` record for the
  ``grant_id``, in chain order; the position where it last shrank is the
  superseding position a refusal names. A re-issue carrying a wider ceiling
  therefore restores nothing, which matches the attenuation rule the delegation
  capability tokens already enforce.
* **An empty ceiling authorises nothing.** A ceiling is a maximum, so an empty
  one caps every capability out. The gate is opt-in, so this fails closed
  without changing how any existing grant is issued.
* **A chain break refuses everything.** A record that does not link, does not
  re-HMAC, or does not verify against its embedded issuer key poisons the
  index; every later decision refuses and names the offending record.
"""

from __future__ import annotations

import hmac as _hmac
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.core.identity.grants import (
    GENESIS_HMAC,
    GRANT_ISSUED,
    GRANT_REVOKED,
    GrantLedger,
    record_hmac,
    verify_grant_signature,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "DispatchGrantGate",
    "GrantDecision",
    "GrantPreconditionIndex",
    "GrantPreconditions",
    "GrantRefusedError",
    "RedecisionOutcome",
    "ToolCallDescription",
    "tool_call_capability",
]


class RedecisionOutcome(StrEnum):
    """What the chain says about a grant's preconditions at the point of use."""

    #: Nothing on the chain changed the grant since it was issued.
    VALID = "valid"
    #: The grant still authorises, but over a narrower set than at issue.
    NARROWED = "narrowed"
    #: The grant authorises nothing any more -- revoked, expired, or unknown.
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class GrantDecision:
    """One re-decision, with the chain position that explains it.

    ``reason`` is the whole point of the record: it names the position the
    grant was issued at and, when something superseded it, the kind and
    position of the superseding record. ``permitted`` is the dispatch verdict;
    ``outcome`` is what happened to the grant, which is not the same question
    (a still-valid grant refuses a call outside its ceiling).
    """

    outcome: RedecisionOutcome
    permitted: bool
    grant_id: str
    capability: str
    reason: str
    issued_index: int = -1
    superseded_index: int = -1
    superseding_kind: str = ""


@dataclass(frozen=True, slots=True)
class GrantPreconditions:
    """The current state of one grant's preconditions, folded from the chain."""

    grant_id: str
    issued_index: int
    task_id: str
    expiry: int
    capabilities: frozenset[str]
    narrowed_index: int = -1
    revoked_index: int = -1
    revoked_kind: str = ""


class GrantRefusedError(RuntimeError):
    """Raised when a re-decision refuses an about-to-run call.

    Carries the :class:`GrantDecision` so a caller that catches it can report
    the superseding chain position rather than only the fact of refusal.
    """

    def __init__(self, decision: GrantDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class ToolCallDescription(Protocol):
    """The part of an about-to-dispatch tool call a capability is derived from."""

    @property
    def server_name(self) -> str: ...

    @property
    def tool_name(self) -> str: ...


def tool_call_capability(intent: ToolCallDescription) -> str:
    """Return the capability name a tool call needs -- its tool name.

    A grant's ``capability_ceiling`` holds symbolic capability names, and the
    tool name is the symbol an operator writes when scoping a grant to a tool.
    A deployment whose ceiling is expressed differently supplies its own
    resolver through :attr:`DispatchGrantGate.capability_of`.
    """
    return intent.tool_name


def _empty_precondition_map() -> dict[str, GrantPreconditions]:
    """Return the per-grant map an index starts from."""
    return {}


@dataclass(slots=True)
class GrantPreconditionIndex:
    """Incrementally authenticated projection of one run's grant chain.

    The index owns a byte offset and a running ``prev_hmac``, so a refresh
    authenticates only the records appended since the previous one. Records
    that fail linkage, HMAC, or signature poison the index permanently: the
    offset is not advanced past them and every later decision refuses.
    """

    path: Path
    key: bytes
    _offset: int = 0
    _prev_hmac: str = GENESIS_HMAC
    _grants: dict[str, GrantPreconditions] = field(default_factory=_empty_precondition_map)
    _break: str = ""

    @classmethod
    def for_run(cls, ledger: GrantLedger, run_id: str) -> GrantPreconditionIndex:
        """Return an index over ``run_id``'s records in ``ledger``'s tree."""
        return cls(path=ledger.receipt_path(run_id), key=ledger.hmac_key)

    def refresh(self) -> None:
        """Authenticate and fold the records appended since the last refresh."""
        if self._break:
            return
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                blob = fh.read()
        except FileNotFoundError:
            return
        # A record is durable only once its terminating newline is on disk, so
        # a trailing partial line is left for the next refresh rather than
        # parsed and rejected as malformed.
        cut = blob.rfind(b"\n")
        if cut < 0:
            return
        for raw in blob[: cut + 1].splitlines(keepends=True):
            text = raw.strip()
            if text and not self._consume(text):
                return
            self._offset += len(raw)

    def _consume(self, raw: bytes) -> bool:
        """Authenticate one record and fold it in; False marks the chain broken."""
        try:
            entry: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._break = f"grant chain broke at byte offset {self._offset}: malformed JSON: {exc}"
            return False
        index = int(entry.get("record_index", -1))
        chain_body = {k: v for k, v in entry.items() if k != "hmac"}
        if chain_body.get("prev_hmac") != self._prev_hmac:
            self._break = f"grant chain broke at record {index}: prev_hmac does not match the preceding record"
            return False
        if not _hmac.compare_digest(record_hmac(self.key, self._prev_hmac, chain_body), str(entry.get("hmac", ""))):
            self._break = f"grant chain broke at record {index}: HMAC mismatch (record tampered or wrong key)"
            return False
        signed = {k: v for k, v in chain_body.items() if k not in ("prev_hmac", "signature")}
        if not verify_grant_signature(str(entry.get("issuer_pubkey", "")), signed, str(entry.get("signature", ""))):
            self._break = f"grant chain broke at record {index}: Ed25519 signature invalid"
            return False
        self._fold(entry, index)
        self._prev_hmac = str(entry.get("hmac", ""))
        return True

    def _fold(self, entry: dict[str, Any], index: int) -> None:
        """Apply one authenticated record to the per-grant precondition state."""
        grant_id = str(entry.get("grant_id", ""))
        if not grant_id:
            return
        kind = str(entry.get("kind", ""))
        current = self._grants.get(grant_id)
        if kind == GRANT_ISSUED:
            self._grants[grant_id] = self._fold_issued(entry, index, current)
        elif kind == GRANT_REVOKED and current is not None and current.revoked_index < 0:
            self._grants[grant_id] = GrantPreconditions(
                grant_id=current.grant_id,
                issued_index=current.issued_index,
                task_id=current.task_id,
                expiry=current.expiry,
                capabilities=current.capabilities,
                narrowed_index=current.narrowed_index,
                revoked_index=index,
                revoked_kind=GRANT_REVOKED,
            )

    @staticmethod
    def _fold_issued(
        entry: dict[str, Any],
        index: int,
        current: GrantPreconditions | None,
    ) -> GrantPreconditions:
        """Fold a ``grant_issued`` record, narrowing but never widening."""
        ceiling = frozenset(str(c) for c in entry.get("capability_ceiling", ()))
        expiry = int(entry.get("expiry", 0))
        if current is None:
            return GrantPreconditions(
                grant_id=str(entry.get("grant_id", "")),
                issued_index=index,
                task_id=str(entry.get("task_id", "")),
                expiry=expiry,
                capabilities=ceiling,
            )
        narrowed = current.capabilities & ceiling
        # ``expiry == 0`` means "no explicit expiry", which is the widest
        # value, so a re-issue can only bring the effective expiry forward.
        candidates = [e for e in (current.expiry, expiry) if e]
        return GrantPreconditions(
            grant_id=current.grant_id,
            issued_index=current.issued_index,
            task_id=current.task_id,
            expiry=min(candidates) if candidates else 0,
            capabilities=narrowed,
            narrowed_index=index if narrowed != current.capabilities else current.narrowed_index,
            revoked_index=current.revoked_index,
            revoked_kind=current.revoked_kind,
        )

    def decide(self, grant_id: str, capability: str, *, now: float | None = None) -> GrantDecision:
        """Re-decide ``grant_id`` for ``capability`` against current chain state."""
        self.refresh()
        if self._break:
            return GrantDecision(
                outcome=RedecisionOutcome.REVOKED,
                permitted=False,
                grant_id=grant_id,
                capability=capability,
                reason=self._break,
            )
        pre = self._grants.get(grant_id)
        if pre is None:
            return GrantDecision(
                outcome=RedecisionOutcome.REVOKED,
                permitted=False,
                grant_id=grant_id,
                capability=capability,
                reason=f"no grant_issued record for grant {grant_id} on this chain",
            )
        if pre.revoked_index >= 0:
            return GrantDecision(
                outcome=RedecisionOutcome.REVOKED,
                permitted=False,
                grant_id=grant_id,
                capability=capability,
                reason=(
                    f"grant {grant_id} issued at chain position {pre.issued_index} "
                    f"was superseded by {pre.revoked_kind} at chain position {pre.revoked_index}"
                ),
                issued_index=pre.issued_index,
                superseded_index=pre.revoked_index,
                superseding_kind=pre.revoked_kind,
            )
        current = time.time() if now is None else now
        if pre.expiry and current >= pre.expiry:
            return GrantDecision(
                outcome=RedecisionOutcome.REVOKED,
                permitted=False,
                grant_id=grant_id,
                capability=capability,
                reason=(f"grant {grant_id} issued at chain position {pre.issued_index} expired at {pre.expiry}"),
                issued_index=pre.issued_index,
            )
        return self._decide_capability(pre, capability)

    @staticmethod
    def _decide_capability(pre: GrantPreconditions, capability: str) -> GrantDecision:
        """Decide a live grant against the capability the call needs."""
        narrowed = pre.narrowed_index >= 0
        outcome = RedecisionOutcome.NARROWED if narrowed else RedecisionOutcome.VALID
        superseded = pre.narrowed_index if narrowed else -1
        superseding = GRANT_ISSUED if narrowed else ""
        if capability in pre.capabilities:
            reason = (
                f"grant {pre.grant_id} issued at chain position {pre.issued_index} still authorises "
                f"capability {capability!r}"
            )
            if narrowed:
                reason += f" after being narrowed at chain position {pre.narrowed_index}"
            return GrantDecision(
                outcome=outcome,
                permitted=True,
                grant_id=pre.grant_id,
                capability=capability,
                reason=reason,
                issued_index=pre.issued_index,
                superseded_index=superseded,
                superseding_kind=superseding,
            )
        if narrowed:
            reason = (
                f"capability {capability!r} authorised by grant {pre.grant_id} at chain position "
                f"{pre.issued_index} was superseded by {GRANT_ISSUED} at chain position {pre.narrowed_index}"
            )
        else:
            reason = (
                f"grant {pre.grant_id} issued at chain position {pre.issued_index} never authorised "
                f"capability {capability!r}"
            )
        return GrantDecision(
            outcome=outcome,
            permitted=False,
            grant_id=pre.grant_id,
            capability=capability,
            reason=reason,
            issued_index=pre.issued_index,
            superseded_index=superseded,
            superseding_kind=superseding,
        )


@dataclass(slots=True)
class DispatchGrantGate:
    """Re-decide the grant behind one run's tool calls, and chain the refusals.

    A refusal is appended to the same grant chain as a ``grant_refused``
    record, so the sequence ``grant_issued -> grant_revoked -> grant_refused``
    is ordered evidence rather than a log line: a verifier reading the chain
    later sees when the authority lapsed and which call was refused after it.
    A permitted call writes nothing, so re-deciding on every dispatch does not
    flood the chain.
    """

    index: GrantPreconditionIndex
    grant_id: str
    run_id: str
    task_id: str = ""
    ledger: GrantLedger | None = None
    capability_of: Callable[[ToolCallDescription], str] = tool_call_capability

    def re_decide(self, intent: ToolCallDescription) -> GrantDecision:
        """Return the decision for ``intent``, raising when it is refused."""
        decision = self.index.decide(self.grant_id, self.capability_of(intent))
        if decision.permitted:
            return decision
        if self.ledger is not None:
            self.ledger.record_refusal(
                run_id=self.run_id,
                task_id=self.task_id,
                secret_name="",
                reason=decision.reason,
                grant_id=self.grant_id,
            )
        raise GrantRefusedError(decision)
