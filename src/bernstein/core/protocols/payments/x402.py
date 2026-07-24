"""x402 settlement hook for metered MCP gateway calls (issue #2528).

When an upstream MCP server answers a proxied tool call with an HTTP 402
challenge (the x402 pay-and-retry pattern), the gateway has, until now, no
settlement path: the 402 surfaces as an ordinary tool error and no record ties
a payment to the exact invocation it paid for. This module is the concrete
x402 adapter over the AP2 mandate / consent-receipt surface
(:mod:`bernstein.core.protocols.payments.mandates`). Bernstein never executes a
payment itself; the settlement hook is the single boundary where the operator's
own payment tooling plugs in.

Design (the artefact IS the proof)
----------------------------------
A settlement is not "a payment plus an audit line". The primary artefact is a
:class:`SpendReceipt` whose identity is a lineage-spine entry hash and whose
bindings recompute offline against two independent records:

* the **WAL invocation record** of the exact proxied (retried) tool call --
  proving *which executed call* the charge paid for; and
* the **consent receipt** the settlement was authorized under -- proving the
  spend was inside a signed :class:`IntentMandate` bound
  (:func:`verify_consent_receipt`).

A payment claim that does not chain to *both* fails :func:`verify_spend_receipt`.
Mutating the recorded amount, the 402 challenge digest, or the WAL invocation
digest breaks the recompute -- so a provider statement is checkable against
gateway execution history rather than taken on trust. Strip the spine, the WAL,
or the mandate and the receipt loses its meaning, not just its log.

Default off
-----------
:class:`X402Config` gates the whole path and defaults to disabled. With no
active config a 402 surfaces unchanged (no hook lookup, no retry). The gateway
never double-settles under replay: replay serves the recorded settled response
without reaching this module (the settlement seam is on the live-proxy branch
only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.protocols.payments.mandates import (
    CartMandate,
    ConsentReceipt,
    IntentMandate,
    SettlementRef,
    authorized_action_set,
    emit_consent_receipt,
    read_consent_receipt,
    verify_consent_receipt,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from bernstein.core.cost.mcp_server_cost import MCPServerCostMeter
    from bernstein.core.persistence.wal import WALEntry
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

#: Version stamped into every spend / refusal receipt preimage. Bump only on a
#: wire-format change.
X402_SCHEMA_VERSION = 1

#: Dedicated lineage run under which x402 spend and refusal receipts are
#: anchored, kept separate from the mandate spine so settlement lineage never
#: interleaves with consent-receipt lineage.
X402_SETTLEMENT_RUN_ID = "x402-settlements"

#: Audit-chain event types mirrored for a settled / refused settlement. Only
#: hashes, the server name, and the (public, non-secret) amount are recorded --
#: never a payment credential.
EVENT_X402_SETTLEMENT = "x402.settlement"
EVENT_X402_SETTLEMENT_REFUSED = "x402.settlement_refused"

#: Reserved params key under which the payment reference is injected into the
#: retried JSON-RPC request.
X402_PAYMENT_KEY = "_x402_payment"

_SETTLEMENT_SUBPATH = (".sdd", "x402", "settlements")
_REFUSAL_SUBPATH = (".sdd", "x402", "refusals")

_MANDATE_ACTOR = "x402_gateway"
_MANDATE_MODEL = "none"


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def settlement_ref_hash(ref: SettlementRef) -> str:
    """Return the content digest of a :class:`SettlementRef` (audit-chain input)."""
    return _sha256(ref.to_dict())


def _safe_hash_name(receipt_hash: str) -> str:
    """Return a filesystem-safe basename for a ``sha256:<hex>`` receipt hash."""
    if not receipt_hash:
        raise ValueError("empty receipt_hash")
    if "/" in receipt_hash or "\\" in receipt_hash or "\x00" in receipt_hash:
        raise ValueError(f"receipt_hash contains an unsafe character: {receipt_hash!r}")
    return receipt_hash.replace(":", "_")


# ---------------------------------------------------------------------------
# X402Challenge -- parsed 402 challenge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class X402Challenge:
    """A parsed HTTP 402 (x402) challenge extracted from a proxied response.

    ``raw`` is the canonical challenge payload the digest is computed over --
    either the ``x402Version``/``accepts`` object or, when the upstream only
    signals a bare ``402`` code, the JSON-RPC error object.

    Attributes:
        raw: The dict the challenge digest is bound to.
        x402_version: The advertised x402 protocol version (``0`` when absent).
        accepts: The advertised payment options (may be empty).
    """

    raw: dict[str, Any]
    x402_version: int = 0
    accepts: tuple[dict[str, Any], ...] = ()

    def challenge_hash(self) -> str:
        """Return the ``sha256:`` digest of the canonical challenge body."""
        return _sha256(self.raw)

    def _first_accept(self) -> dict[str, Any]:
        return self.accepts[0] if self.accepts else {}

    def max_amount_required(self) -> str:
        """Return the advertised ``maxAmountRequired`` atomic string (or ``""``)."""
        return str(self._first_accept().get("maxAmountRequired", ""))

    def resource(self) -> str:
        """Return the advertised protected resource identifier (or ``""``)."""
        return str(self._first_accept().get("resource", ""))

    def scheme(self) -> str:
        """Return the advertised payment scheme (or ``""``)."""
        return str(self._first_accept().get("scheme", ""))

    def resolved_amount_usd(self) -> float | None:
        """Return an explicit USD price when the challenge states one.

        x402 challenges carry ``maxAmountRequired`` in atomic units of an
        arbitrary asset; converting that to USD needs the asset's decimals and
        a rate Bernstein does not invent. When the upstream includes an
        explicit ``amountUsd`` (some servers do) it is used; otherwise the
        operator must supply a price resolver via :class:`X402Config`. Returns
        ``None`` when no USD figure can be determined -- settlement then refuses
        honestly rather than guessing.
        """
        raw_amount = self._first_accept().get("amountUsd")
        if raw_amount is None:
            return None
        try:
            return float(raw_amount)
        except (TypeError, ValueError):
            return None


def _extract_challenge_payload(response: dict[str, Any]) -> dict[str, Any] | None:
    """Return the x402 challenge payload carried by *response*, or ``None``.

    Recognises the challenge in an ``error`` object (``code == 402`` or a
    ``data``/body carrying ``x402Version``/``accepts``) and in a ``result``
    that wraps an x402 body. An ordinary success or a non-402 error is not a
    challenge.
    """
    error = response.get("error")
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and _looks_like_x402(data):
            return data
        if _http_status(error) == 402 or error.get("code") == 402:
            return data if isinstance(data, dict) and data else error
    result = response.get("result")
    if isinstance(result, dict) and _looks_like_x402(result):
        return result
    return None


def _looks_like_x402(payload: dict[str, Any]) -> bool:
    return "x402Version" in payload or "accepts" in payload


def _http_status(error: dict[str, Any]) -> int | None:
    data = error.get("data")
    if isinstance(data, dict):
        status = data.get("http_status") or data.get("httpStatus") or data.get("status")
        if isinstance(status, int):
            return status
    return None


def parse_challenge(response: dict[str, Any]) -> X402Challenge | None:
    """Parse a JSON-RPC *response* into an :class:`X402Challenge`, or ``None``.

    ``None`` means "not a 402 challenge" -- the caller returns the response
    unchanged. Detection is deliberately structural (a 402 code or an x402
    body); it never performs a network read.
    """
    if not isinstance(response, dict):
        return None
    payload = _extract_challenge_payload(response)
    if payload is None:
        return None
    accepts_raw = payload.get("accepts")
    accepts: tuple[dict[str, Any], ...] = (
        tuple(a for a in accepts_raw if isinstance(a, dict)) if isinstance(accepts_raw, list) else ()
    )
    try:
        version = int(payload.get("x402Version", 0))
    except (TypeError, ValueError):
        version = 0
    return X402Challenge(raw=payload, x402_version=version, accepts=accepts)


def build_retry_request(message: dict[str, Any], payment_ref: str) -> dict[str, Any]:
    """Return a copy of *message* with *payment_ref* injected into its params.

    The payment reference lands under :data:`X402_PAYMENT_KEY` inside the tool
    arguments so the upstream can settle the retried call. The original message
    is not mutated -- the retried-request digest binds this exact copy.
    """
    retried = json.loads(json.dumps(message))  # deep copy via canonical round-trip
    params = retried.setdefault("params", {})
    if not isinstance(params, dict):
        params = {}
        retried["params"] = params
    arguments = params.setdefault("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
        params["arguments"] = arguments
    arguments[X402_PAYMENT_KEY] = {"payment_ref": payment_ref}
    return retried


def retried_request_hash(retried: dict[str, Any]) -> str:
    """Return the content digest of a retried request payload."""
    return _sha256(retried)


# ---------------------------------------------------------------------------
# Settlement hook -- the operator's payment boundary
# ---------------------------------------------------------------------------


@runtime_checkable
class SettlementHook(Protocol):
    """The operator-registered boundary that settles a 402 challenge.

    Bernstein calls :meth:`settle` after it has confirmed the spend is inside a
    signed mandate and under its cap. The hook executes the operator's own
    payment and returns an opaque, non-secret payment reference -- or ``None``
    to decline, which surfaces the original 402 as an ordinary tool error.

    ``amount_usd`` is the amount Bernstein authorized (derived from the
    challenge / mandate), never a value the hook is free to change.
    """

    def settle(self, challenge: X402Challenge, *, server_name: str, tool_name: str, amount_usd: float) -> str | None:
        """Return a payment reference for *challenge*, or ``None`` to decline."""
        ...


@dataclass(frozen=True)
class CallableSettlementHook:
    """Adapt a plain callable into a :class:`SettlementHook`."""

    fn: Callable[[X402Challenge, str, str, float], str | None]

    def settle(self, challenge: X402Challenge, *, server_name: str, tool_name: str, amount_usd: float) -> str | None:
        return self.fn(challenge, server_name, tool_name, amount_usd)


@dataclass(frozen=True)
class CommandSettlementHook:
    """Settle by invoking an operator command.

    The command receives the challenge, server, tool, and authorized amount as
    a JSON object on stdin and must print ``{"payment_ref": "..."}`` on stdout
    to settle, or exit non-zero / print ``{"payment_ref": null}`` to decline.
    Bernstein never parses a credential out of the command; only the opaque
    reference is read.
    """

    argv: tuple[str, ...]
    timeout_s: float = 30.0

    def settle(self, challenge: X402Challenge, *, server_name: str, tool_name: str, amount_usd: float) -> str | None:
        request = json.dumps(
            {
                "challenge": challenge.raw,
                "server_name": server_name,
                "tool_name": tool_name,
                "amount_usd": amount_usd,
            }
        )
        try:
            proc = subprocess.run(
                list(self.argv),
                input=request,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("x402 settlement command failed to run: %s", exc)
            return None
        if proc.returncode != 0:
            logger.warning("x402 settlement command declined (exit %s)", proc.returncode)
            return None
        try:
            parsed = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            logger.warning("x402 settlement command produced non-JSON output")
            return None
        ref = parsed.get("payment_ref")
        return str(ref) if ref else None


# ---------------------------------------------------------------------------
# Config gate -- default off
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class X402Config:
    """The x402 settlement gate. Disabled by default.

    Attributes:
        enabled: Master switch. When ``False`` the settlement path is inert.
        hook: The operator settlement boundary. Required for an active config.
        max_settlement_usd: Optional extra ceiling applied per settlement on
            top of the mandate cap (``0`` disables the extra guard).
        price_resolver: Optional operator callback that maps a challenge to a
            USD amount when the challenge carries no explicit ``amountUsd``.
    """

    enabled: bool = False
    hook: SettlementHook | None = None
    max_settlement_usd: float = 0.0
    price_resolver: Callable[[X402Challenge], float | None] | None = None

    def is_active(self) -> bool:
        """Return whether the settlement path should run."""
        return bool(self.enabled and self.hook is not None)

    def resolve_amount(self, challenge: X402Challenge) -> float | None:
        """Return the USD amount to authorize for *challenge*, or ``None``."""
        if self.price_resolver is not None:
            return self.price_resolver(challenge)
        return challenge.resolved_amount_usd()


# ---------------------------------------------------------------------------
# SpendReceipt -- the primary settlement artefact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendReceipt:
    """A settled x402 charge, bound to its WAL invocation and its mandate.

    Attributes:
        server_name: Upstream MCP server the charge is attributed to.
        tool_name: Tool call that was settled (the authorized action).
        wal_run_id: WAL run the settled invocation was recorded in.
        wal_invocation_seq: Sequence number of the settled invocation record.
        wal_invocation_digest: ``entry_hash`` of that WAL record -- the exact
            executed call the charge paid for.
        mandate_hash: Cart-mandate hash of the authorising consent receipt.
        intent_hash: Intent-mandate hash the cart was issued under.
        settlement_ref: The x402 pay-and-retry reference (challenge digest,
            payment reference, retried-request digest, settled amount).
        consent_journal_entry_hash: Spine anchor of the consent receipt this
            settlement was authorized under (cross-link into the mandate spine).
        task_id: Task the settlement was attributed to.
        timestamp: Integer timestamp; caller-chosen but stable.
        journal_entry_hash: This receipt's own spine anchor -- its identity.
    """

    server_name: str
    tool_name: str
    wal_run_id: str
    wal_invocation_seq: int
    wal_invocation_digest: str
    mandate_hash: str
    intent_hash: str
    settlement_ref: SettlementRef
    consent_journal_entry_hash: str
    task_id: str
    timestamp: int
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the anchored binding (everything except the anchor itself)."""
        return {
            "v": X402_SCHEMA_VERSION,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "wal_run_id": self.wal_run_id,
            "wal_invocation_seq": self.wal_invocation_seq,
            "wal_invocation_digest": self.wal_invocation_digest,
            "mandate_hash": self.mandate_hash,
            "intent_hash": self.intent_hash,
            "settlement_ref": self.settlement_ref.to_dict(),
            "consent_journal_entry_hash": self.consent_journal_entry_hash,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (spine-hashed)."""
        return _canonical_bytes(self._binding())

    def receipt_hash(self) -> str:
        """Return the content hash of the binding (the receipt's file id)."""
        return _sha256(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}

    @classmethod
    def from_bytes(cls, raw: bytes) -> SpendReceipt:
        row = json.loads(raw)
        return cls(
            server_name=str(row["server_name"]),
            tool_name=str(row["tool_name"]),
            wal_run_id=str(row["wal_run_id"]),
            wal_invocation_seq=int(row["wal_invocation_seq"]),
            wal_invocation_digest=str(row["wal_invocation_digest"]),
            mandate_hash=str(row["mandate_hash"]),
            intent_hash=str(row["intent_hash"]),
            settlement_ref=SettlementRef.from_dict(row["settlement_ref"]),
            consent_journal_entry_hash=str(row["consent_journal_entry_hash"]),
            task_id=str(row["task_id"]),
            timestamp=int(row["timestamp"]),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


@dataclass(frozen=True)
class RefusalReceipt:
    """A refused settlement, anchored so a denial is as provable as a payment.

    Attributes:
        server_name: Upstream MCP server the refused charge targeted.
        tool_name: Tool call that was refused.
        challenge_hash: Digest of the 402 challenge that was refused.
        amount_usd: The amount that would have been settled (``0`` when
            undeterminable).
        reason: Human-readable, closed-vocabulary refusal reason.
        mandate_hash: Cart-mandate hash when one applied (``""`` when no
            mandate authorized the spend at all).
        task_id: Task the refusal was attributed to.
        timestamp: Integer timestamp.
        journal_entry_hash: This receipt's own spine anchor.
    """

    server_name: str
    tool_name: str
    challenge_hash: str
    amount_usd: float
    reason: str
    mandate_hash: str
    task_id: str
    timestamp: int
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        return {
            "v": X402_SCHEMA_VERSION,
            "kind": "refusal",
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "challenge_hash": self.challenge_hash,
            "amount_usd": self.amount_usd,
            "reason": self.reason,
            "mandate_hash": self.mandate_hash,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        return _canonical_bytes(self._binding())

    def receipt_hash(self) -> str:
        return _sha256(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}

    @classmethod
    def from_bytes(cls, raw: bytes) -> RefusalReceipt:
        row = json.loads(raw)
        return cls(
            server_name=str(row["server_name"]),
            tool_name=str(row["tool_name"]),
            challenge_hash=str(row["challenge_hash"]),
            amount_usd=float(row.get("amount_usd", 0.0)),
            reason=str(row["reason"]),
            mandate_hash=str(row.get("mandate_hash", "")),
            task_id=str(row["task_id"]),
            timestamp=int(row["timestamp"]),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


# ---------------------------------------------------------------------------
# On-disk paths
# ---------------------------------------------------------------------------


def spend_receipt_path(workdir: Path, receipt_hash: str) -> Path:
    """Return the on-disk spend-receipt path for *receipt_hash*."""
    return workdir.joinpath(*_SETTLEMENT_SUBPATH, f"{_safe_hash_name(receipt_hash)}.json")


def refusal_receipt_path(workdir: Path, receipt_hash: str) -> Path:
    """Return the on-disk refusal-receipt path for *receipt_hash*."""
    return workdir.joinpath(*_REFUSAL_SUBPATH, f"{_safe_hash_name(receipt_hash)}.json")


def read_spend_receipt(workdir: Path, receipt_hash: str) -> SpendReceipt | None:
    """Return the spend receipt for *receipt_hash* or ``None`` if absent/malformed."""
    path = spend_receipt_path(workdir, receipt_hash)
    if not path.is_file():
        return None
    try:
        return SpendReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("x402: malformed spend receipt at %s", path)
        return None


def iter_spend_receipts(workdir: Path) -> Iterator[SpendReceipt]:
    """Yield every recorded spend receipt (unordered)."""
    root = workdir.joinpath(*_SETTLEMENT_SUBPATH)
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        try:
            yield SpendReceipt.from_bytes(path.read_bytes())
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.debug("x402: skipping malformed spend receipt at %s", path)
            continue


# ---------------------------------------------------------------------------
# Settlement context + coordinator
# ---------------------------------------------------------------------------


class SettlementStatus(Enum):
    """Outcome of :meth:`X402SettlementCoordinator.pre_authorize`."""

    #: The config is inert -- no hook lookup, no retry (default off, AC1).
    SKIPPED = "skipped"
    #: A mandate/cap/hook gate refused; a chain-anchored refusal receipt was
    #: emitted and the original 402 should surface as an ordinary error (AC2).
    REFUSED = "refused"
    #: The spend is authorized; the caller retries and calls
    #: :meth:`X402SettlementCoordinator.record_settlement`.
    AUTHORIZED = "authorized"


@dataclass(frozen=True)
class PreAuthResult:
    """Result of the pre-payment gate."""

    status: SettlementStatus
    payment_ref: str | None = None
    amount_usd: float = 0.0
    reason: str = ""
    refusal_receipt: RefusalReceipt | None = None


@dataclass
class SettlementContext:
    """Everything the coordinator needs to gate, settle, and anchor.

    Attributes:
        config: The x402 gate (may be inert).
        hmac_key: Audit-chain HMAC key that signs mandates and tags spine
            entries.
        workdir: Project root; receipts land under ``.sdd/x402/``.
        lineage_root: Spine root (``.sdd/lineage``).
        wal_sdd_dir: The ``.sdd`` dir the WAL lives under (for verification).
        wal_run_id: WAL run id the proxied calls are recorded in.
        intent: The active signed intent mandate (``None`` -> fail closed).
        meter: Optional per-server cost meter to flush settled amounts into.
        audit_chain: Optional audit chain to mirror settlement events into.
        now: Clock returning an integer timestamp.
    """

    config: X402Config
    hmac_key: bytes
    workdir: Path
    lineage_root: Path
    wal_sdd_dir: Path
    wal_run_id: str
    intent: IntentMandate | None = None
    meter: MCPServerCostMeter | None = None
    audit_chain: AuditChainStore | None = None
    now: Callable[[], int] = lambda: 0

    @property
    def task_id(self) -> str:
        return self.intent.task_id if self.intent is not None else "unknown"


class X402SettlementCoordinator:
    """Gates, settles, and anchors x402 settlements over the mandate surface.

    The coordinator holds no transport: the caller (the gateway) performs the
    actual retry between :meth:`pre_authorize` and :meth:`record_settlement`.
    This keeps every side effect that matters -- the mandate gate, the receipt,
    the ledger flush -- pure and hermetically testable.
    """

    def __init__(self, context: SettlementContext) -> None:
        self._ctx = context

    # -- pre-payment gate ---------------------------------------------------

    def pre_authorize(self, challenge: X402Challenge, *, server_name: str, tool_name: str) -> PreAuthResult:
        """Gate a 402 challenge against the mandate and invoke the hook.

        Returns :data:`SettlementStatus.SKIPPED` (do nothing) when the config
        is inert, :data:`SettlementStatus.REFUSED` (fail closed, refusal
        receipt anchored) when no mandate authorizes the spend / the cap is
        breached / the hook declines / the amount is undeterminable, and
        :data:`SettlementStatus.AUTHORIZED` with a payment reference otherwise.

        The hook is never invoked -- and thus no payment is ever executed --
        until the mandate gate and the spend cap have both passed.
        """
        ctx = self._ctx
        if not ctx.config.is_active():
            return PreAuthResult(status=SettlementStatus.SKIPPED, reason="x402 disabled")

        amount = ctx.config.resolve_amount(challenge)
        if amount is None:
            return self._refuse(challenge, server_name, tool_name, 0.0, "amount undeterminable", "")
        amount = max(0.0, float(amount))

        intent = ctx.intent
        if intent is None:
            return self._refuse(challenge, server_name, tool_name, amount, "no active mandate", "")

        cart = self._build_cart(intent, tool_name, amount)
        authorized = authorized_action_set(
            intent=intent,
            cart=cart,
            hmac_key=ctx.hmac_key,
            now=ctx.now(),
            workdir=ctx.workdir,
        )
        if tool_name not in authorized:
            return self._refuse(
                challenge, server_name, tool_name, amount, "no matching mandate authorizes this tool call", ""
            )

        breach = self._cap_breach(intent, amount)
        if breach is not None:
            return self._refuse(challenge, server_name, tool_name, amount, breach, cart.mandate_hash())

        if ctx.config.max_settlement_usd > 0 and amount > ctx.config.max_settlement_usd:
            return self._refuse(
                challenge, server_name, tool_name, amount, "amount exceeds max_settlement_usd", cart.mandate_hash()
            )

        assert ctx.config.hook is not None  # is_active() guaranteed a hook
        payment_ref = ctx.config.hook.settle(challenge, server_name=server_name, tool_name=tool_name, amount_usd=amount)
        if not payment_ref:
            return self._refuse(
                challenge, server_name, tool_name, amount, "settlement hook declined", cart.mandate_hash()
            )

        return PreAuthResult(status=SettlementStatus.AUTHORIZED, payment_ref=payment_ref, amount_usd=amount)

    # -- post-retry recording ----------------------------------------------

    def record_settlement(
        self,
        challenge: X402Challenge,
        *,
        server_name: str,
        tool_name: str,
        payment_ref: str,
        amount_usd: float,
        retried_request: dict[str, Any],
        wal_entry: WALEntry,
    ) -> SpendReceipt:
        """Bind the settled invocation into a chain-anchored spend receipt.

        Reuses the consent-receipt machinery to prove the spend was authorized,
        then anchors a :class:`SpendReceipt` binding the WAL invocation record
        digest, the 402 challenge digest, the payment reference, the retried
        request digest, and the mandate hash. Flushes the settled amount into
        the per-server cost meter and mirrors a ``x402.settlement`` event into
        the audit chain.
        """
        ctx = self._ctx
        assert ctx.intent is not None  # AUTHORIZED implies an intent
        intent = ctx.intent
        cart = self._build_cart(intent, tool_name, amount_usd)
        ref = SettlementRef(
            challenge_hash=challenge.challenge_hash(),
            payment_ref=payment_ref,
            retried_request_hash=retried_request_hash(retried_request),
            amount_usd=amount_usd,
        )
        now = ctx.now()
        # The consent receipt re-gates and enforces the cap; a breach here
        # raises MandateRefused rather than silently over-spending.
        consent: ConsentReceipt = emit_consent_receipt(
            workdir=ctx.workdir,
            lineage_root=ctx.lineage_root,
            hmac_key=ctx.hmac_key,
            intent=intent,
            cart=cart,
            settlement_ref=ref,
            now=now,
            ledger=ctx.meter.ledger if ctx.meter is not None else None,
        )

        receipt = SpendReceipt(
            server_name=server_name,
            tool_name=tool_name,
            wal_run_id=ctx.wal_run_id,
            wal_invocation_seq=wal_entry.seq,
            wal_invocation_digest=wal_entry.entry_hash,
            mandate_hash=consent.mandate_hash,
            intent_hash=consent.intent_hash,
            settlement_ref=ref,
            consent_journal_entry_hash=consent.journal_entry_hash,
            task_id=intent.task_id,
            timestamp=now,
        )
        anchored = self._anchor_spend_receipt(receipt, now)
        self._flush_ledger(anchored)
        self._mirror_settlement(anchored)
        return anchored

    # -- internals ----------------------------------------------------------

    def _build_cart(self, intent: IntentMandate, tool_name: str, amount_usd: float) -> CartMandate:
        return CartMandate(
            intent_hash=intent.mandate_hash(),
            tool_calls=(tool_name,),
            amount_usd=amount_usd,
        ).sign(self._ctx.hmac_key)

    def _cap_breach(self, intent: IntentMandate, amount: float) -> str | None:
        """Return a refusal reason when *amount* would breach the intent cap."""
        cap = intent.spend_cap_usd if intent.spend_cap_usd > 0 else 0.0
        prior = 0.0
        meter = self._ctx.meter
        if meter is not None and meter.ledger is not None:
            prior = meter.ledger.totals_by("task").get(intent.task_id or "unknown", 0.0)
        if prior + max(0.0, amount) > cap:
            return f"spend cap breach: ${prior:.4f} + ${amount:.4f} exceeds cap ${cap:.4f}"
        return None

    def _anchor_spend_receipt(self, receipt: SpendReceipt, now: int) -> SpendReceipt:
        spine = LineageSpine(self._ctx.lineage_root, run_id=X402_SETTLEMENT_RUN_ID, hmac_key=self._ctx.hmac_key)
        artifact_path = "/".join((*_SETTLEMENT_SUBPATH, f"{_safe_hash_name(receipt.receipt_hash())}.json"))
        anchor = spine.record(
            artifact_path=artifact_path,
            content=receipt.to_canonical_bytes(),
            actor=_MANDATE_ACTOR,
            step_id=receipt.receipt_hash(),
            model=_MANDATE_MODEL,
            timestamp=now,
        )
        anchored = SpendReceipt(
            server_name=receipt.server_name,
            tool_name=receipt.tool_name,
            wal_run_id=receipt.wal_run_id,
            wal_invocation_seq=receipt.wal_invocation_seq,
            wal_invocation_digest=receipt.wal_invocation_digest,
            mandate_hash=receipt.mandate_hash,
            intent_hash=receipt.intent_hash,
            settlement_ref=receipt.settlement_ref,
            consent_journal_entry_hash=receipt.consent_journal_entry_hash,
            task_id=receipt.task_id,
            timestamp=receipt.timestamp,
            journal_entry_hash=anchor,
        )
        path = spend_receipt_path(self._ctx.workdir, anchored.receipt_hash())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        return anchored

    def _flush_ledger(self, receipt: SpendReceipt) -> None:
        meter = self._ctx.meter
        if meter is None:
            return
        meter.record(
            task_id=receipt.task_id,
            server_name=receipt.server_name,
            tool_name=receipt.tool_name,
            cost_usd=receipt.settlement_ref.amount_usd,
        )

    def _mirror_settlement(self, receipt: SpendReceipt) -> None:
        chain = self._ctx.audit_chain
        if chain is None:
            return
        try:
            chain.log_with_prev_digest(
                event_type=EVENT_X402_SETTLEMENT,
                actor=_MANDATE_ACTOR,
                resource_type="x402_spend_receipt",
                resource_id=receipt.receipt_hash(),
                details={
                    "server_name": receipt.server_name,
                    "tool_name": receipt.tool_name,
                    "mandate_hash": receipt.mandate_hash,
                    "challenge_hash": receipt.settlement_ref.challenge_hash,
                    "settlement_ref_hash": settlement_ref_hash(receipt.settlement_ref),
                    "wal_invocation_digest": receipt.wal_invocation_digest,
                    "amount_usd": receipt.settlement_ref.amount_usd,
                    "journal_entry_hash": receipt.journal_entry_hash,
                    "consent_journal_entry_hash": receipt.consent_journal_entry_hash,
                },
            )
        except Exception:  # pragma: no cover - audit mirror is best-effort
            logger.exception("x402: failed to mirror settlement into the audit chain")

    def _refuse(
        self,
        challenge: X402Challenge,
        server_name: str,
        tool_name: str,
        amount_usd: float,
        reason: str,
        mandate_hash: str,
    ) -> PreAuthResult:
        receipt = self._anchor_refusal(
            RefusalReceipt(
                server_name=server_name,
                tool_name=tool_name,
                challenge_hash=challenge.challenge_hash(),
                amount_usd=amount_usd,
                reason=reason,
                mandate_hash=mandate_hash,
                task_id=self._ctx.task_id,
                timestamp=self._ctx.now(),
            )
        )
        return PreAuthResult(
            status=SettlementStatus.REFUSED,
            reason=reason,
            refusal_receipt=receipt,
        )

    def _anchor_refusal(self, receipt: RefusalReceipt) -> RefusalReceipt:
        spine = LineageSpine(self._ctx.lineage_root, run_id=X402_SETTLEMENT_RUN_ID, hmac_key=self._ctx.hmac_key)
        artifact_path = "/".join((*_REFUSAL_SUBPATH, f"{_safe_hash_name(receipt.receipt_hash())}.json"))
        anchor = spine.record(
            artifact_path=artifact_path,
            content=receipt.to_canonical_bytes(),
            actor=_MANDATE_ACTOR,
            step_id=receipt.receipt_hash(),
            model=_MANDATE_MODEL,
            timestamp=receipt.timestamp,
        )
        anchored = RefusalReceipt(
            server_name=receipt.server_name,
            tool_name=receipt.tool_name,
            challenge_hash=receipt.challenge_hash,
            amount_usd=receipt.amount_usd,
            reason=receipt.reason,
            mandate_hash=receipt.mandate_hash,
            task_id=receipt.task_id,
            timestamp=receipt.timestamp,
            journal_entry_hash=anchor,
        )
        path = refusal_receipt_path(self._ctx.workdir, anchored.receipt_hash())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        self._mirror_refusal(anchored)
        return anchored

    def _mirror_refusal(self, receipt: RefusalReceipt) -> None:
        chain = self._ctx.audit_chain
        if chain is None:
            return
        try:
            chain.log_with_prev_digest(
                event_type=EVENT_X402_SETTLEMENT_REFUSED,
                actor=_MANDATE_ACTOR,
                resource_type="x402_refusal_receipt",
                resource_id=receipt.receipt_hash(),
                details={
                    "server_name": receipt.server_name,
                    "tool_name": receipt.tool_name,
                    "challenge_hash": receipt.challenge_hash,
                    "amount_usd": receipt.amount_usd,
                    "reason": receipt.reason,
                    "mandate_hash": receipt.mandate_hash,
                    "journal_entry_hash": receipt.journal_entry_hash,
                },
            )
        except Exception:  # pragma: no cover - audit mirror is best-effort
            logger.exception("x402: failed to mirror refusal into the audit chain")


# ---------------------------------------------------------------------------
# Offline verification (AC3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendVerifyResult:
    """Outcome of :func:`verify_spend_receipt`."""

    ok: bool
    reason: str
    receipt: SpendReceipt | None = None


def verify_spend_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    wal_sdd_dir: Path,
    spend_receipt_hash: str,
    intent: IntentMandate,
) -> SpendVerifyResult:
    """Prove a spend receipt offline against the WAL and the mandate (AC3).

    Recomputes, from the recorded receipt and the presented intent alone:

    * the receipt's own spine anchor over its canonical bytes (a single-byte
      edit to the amount, challenge digest, or invocation digest fails here);
    * the WAL invocation record: the settled run's WAL chain verifies and the
      record at the recorded sequence still hashes to the recorded digest (a
      tamper of the paid-for call fails here);
    * the mandate binding: the reconstructed cart chains to the presented
      intent and its consent receipt verifies (:func:`verify_consent_receipt`),
      so the payment is provably inside a signed authority.

    ``ok`` is True only when every recomputation matches.
    """
    receipt = read_spend_receipt(workdir, spend_receipt_hash)
    if receipt is None:
        return SpendVerifyResult(ok=False, reason="no spend receipt found")

    anchor_reason = _verify_receipt_anchor(receipt, lineage_root, hmac_key)
    if anchor_reason is not None:
        return SpendVerifyResult(ok=False, reason=anchor_reason, receipt=receipt)

    wal_reason = _verify_wal_invocation(receipt, wal_sdd_dir)
    if wal_reason is not None:
        return SpendVerifyResult(ok=False, reason=wal_reason, receipt=receipt)

    mandate_reason = _verify_mandate_binding(receipt, workdir, lineage_root, hmac_key, intent)
    if mandate_reason is not None:
        return SpendVerifyResult(ok=False, reason=mandate_reason, receipt=receipt)

    return SpendVerifyResult(ok=True, reason="", receipt=receipt)


def _verify_receipt_anchor(receipt: SpendReceipt, lineage_root: Path, hmac_key: bytes) -> str | None:
    spine = LineageSpine(lineage_root, run_id=X402_SETTLEMENT_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return f"settlement spine failed verification ({spine_result.status.value})"
    want = content_hash_of(receipt.to_canonical_bytes())
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            if entry.entry_hash != receipt.journal_entry_hash:
                return "recorded journal_entry_hash does not match the spine anchor over the receipt bytes"
            return None
    return "receipt is not anchored in the settlement spine"


def _verify_wal_invocation(receipt: SpendReceipt, wal_sdd_dir: Path) -> str | None:
    from bernstein.core.persistence.wal import WALReader

    reader = WALReader(run_id=receipt.wal_run_id, sdd_dir=wal_sdd_dir)
    try:
        ok, _errors = reader.verify_chain()
    except FileNotFoundError:
        return "settled WAL run not found"
    if not ok:
        return "settled WAL chain integrity failed"
    for entry in reader.iter_entries():
        if entry.seq != receipt.wal_invocation_seq:
            continue
        if entry.entry_hash != receipt.wal_invocation_digest:
            return "WAL invocation digest does not match the recorded invocation record"
        if entry.decision_type != "mcp_tool_call":
            return "recorded WAL invocation is not an mcp_tool_call"
        if str(entry.inputs.get("server_name", "")) != receipt.server_name:
            return "WAL invocation server_name does not match the receipt"
        return None
    return "no WAL invocation record at the recorded sequence"


def _verify_mandate_binding(
    receipt: SpendReceipt,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    intent: IntentMandate,
) -> str | None:
    if intent.mandate_hash() != receipt.intent_hash:
        return "presented intent does not match the receipt intent_hash"
    cart = CartMandate(
        intent_hash=intent.mandate_hash(),
        tool_calls=(receipt.tool_name,),
        amount_usd=receipt.settlement_ref.amount_usd,
    ).sign(hmac_key)
    if cart.mandate_hash() != receipt.mandate_hash:
        return "reconstructed cart does not match the receipt mandate_hash"

    consent = read_consent_receipt(workdir, receipt.mandate_hash)
    if consent is None:
        return "no consent receipt found for the settlement"
    if consent.settlement_ref.to_dict() != receipt.settlement_ref.to_dict():
        return "consent receipt settlement reference does not match the spend receipt"

    result = verify_consent_receipt(
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        mandate_hash=receipt.mandate_hash,
        intent=intent,
        cart=cart,
    )
    if not result.ok:
        return f"consent receipt verification failed: {result.reason}"
    return None


__all__ = [
    "EVENT_X402_SETTLEMENT",
    "EVENT_X402_SETTLEMENT_REFUSED",
    "X402_PAYMENT_KEY",
    "X402_SCHEMA_VERSION",
    "X402_SETTLEMENT_RUN_ID",
    "CallableSettlementHook",
    "CommandSettlementHook",
    "PreAuthResult",
    "RefusalReceipt",
    "SettlementContext",
    "SettlementHook",
    "SettlementStatus",
    "SpendReceipt",
    "SpendVerifyResult",
    "X402Challenge",
    "X402Config",
    "X402SettlementCoordinator",
    "build_retry_request",
    "iter_spend_receipts",
    "parse_challenge",
    "read_spend_receipt",
    "refusal_receipt_path",
    "retried_request_hash",
    "settlement_ref_hash",
    "spend_receipt_path",
    "verify_spend_receipt",
]
