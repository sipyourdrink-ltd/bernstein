"""Query receipts: the signed record of the exact result set an agent saw.

A :class:`QueryReceipt` binds ``{connection id, query text hash, parameters
hash, result content_hash, row count, truncation flag, chain anchor}`` into a
lineage entry appended through :class:`~bernstein.core.lineage.store.LineageStore`
with its ``.jws`` sidecar, mirrored as an additive audit event. The result
handed to the agent carries the receipt id; the receipt *is* the "these are the
bytes the model saw" record.

Why the lineage entry is the anchor
-----------------------------------

The receipt JSON file on its own is unsigned data. Its integrity comes entirely
from the lineage entry it projects:

* ``content_hash`` -- the entry's own ``content_hash`` is the result digest, so
  a re-hash of the stored result copy must match the *signed* entry.
* every other receipt-core field (connection id, query/params hash, row count,
  truncation flag, row cap, driver, engine) is folded into a *binding digest*
  carried in the entry's signed ``span_id`` field. Tampering any receipt-core
  field makes the recomputed binding diverge from the signed entry.
* removing the lineage entry (or editing it, which changes its hash) makes the
  receipt reference dangle -- the receipt becomes unverifiable, which is exactly
  the intended failure.

So the receipt cannot be forged without forging an Ed25519 signature and an
operator HMAC over the entry, and it cannot be silently detached from its
result because the result digest lives inside the signed entry.
"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.datasources.errors import DataSourceError, ReceiptNotFound
from bernstein.core.datasources.result import NormalizedResult, canonical_bytes, content_hash
from bernstein.core.lineage.entry import canonicalise, compute_operator_hmac, entry_hash
from bernstein.core.lineage.identity import AgentCard, verify_detached
from bernstein.core.lineage.recorder import seal_write
from bernstein.core.lineage.store import LineageStore

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bernstein.core.datasources.connection import DataSourceConnection

RECEIPT_VERSION = 1

#: Additive audit event name mirrored alongside the lineage entry.
AUDIT_EVENT = "datasource.query_receipt"

#: Default cap on a stored result copy (bytes). A larger canonical result is not
#: copied to disk; the receipt still verifies its signature + chain anchor, only
#: the offline re-hash of a stored copy is unavailable.
DEFAULT_RESULT_COPY_CAP = 1_000_000


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _stem(hash_str: str) -> str:
    return hash_str.split(":", 1)[1] if ":" in hash_str else hash_str


def hash_query_text(sql: str) -> str:
    """Return ``sha256:<hex>`` over the NFC-normalised query text."""
    return _sha256_hex(unicodedata.normalize("NFC", sql).encode("utf-8"))


def hash_params(params: Sequence[object] | Mapping[str, object] | None) -> str:
    """Return ``sha256:<hex>`` over a canonical JSON encoding of ``params``."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return _sha256_hex(canonical.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class QueryReceipt:
    """A content-addressed record of one grounded query result."""

    v: int
    receipt_id: str
    connection_id: str
    driver: str
    engine: str
    query_text: str
    query_text_hash: str
    params: object
    params_hash: str
    content_hash: str
    row_count: int
    truncated: bool
    row_cap: int
    binding: str
    artefact_path: str
    lineage_entry_hash: str
    executed_at_ns: int
    result_copy_relpath: str | None = None

    def binding_core(self) -> dict[str, object]:
        """The receipt-core dict whose digest is bound into the signed entry."""
        return {
            "v": self.v,
            "connection_id": self.connection_id,
            "driver": self.driver,
            "engine": self.engine,
            "query_text_hash": self.query_text_hash,
            "params_hash": self.params_hash,
            "content_hash": self.content_hash,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "row_cap": self.row_cap,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "v": self.v,
            "receipt_id": self.receipt_id,
            "connection_id": self.connection_id,
            "driver": self.driver,
            "engine": self.engine,
            "query_text": self.query_text,
            "query_text_hash": self.query_text_hash,
            "params": self.params,
            "params_hash": self.params_hash,
            "content_hash": self.content_hash,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "row_cap": self.row_cap,
            "binding": self.binding,
            "artefact_path": self.artefact_path,
            "lineage_entry_hash": self.lineage_entry_hash,
            "executed_at_ns": self.executed_at_ns,
            "result_copy_relpath": self.result_copy_relpath,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> QueryReceipt:
        return cls(
            v=_coerce_int(data["v"]),
            receipt_id=str(data["receipt_id"]),
            connection_id=str(data["connection_id"]),
            driver=str(data["driver"]),
            engine=str(data["engine"]),
            query_text=str(data["query_text"]),
            query_text_hash=str(data["query_text_hash"]),
            params=data.get("params"),
            params_hash=str(data["params_hash"]),
            content_hash=str(data["content_hash"]),
            row_count=_coerce_int(data["row_count"]),
            truncated=bool(data["truncated"]),
            row_cap=_coerce_int(data["row_cap"]),
            binding=str(data["binding"]),
            artefact_path=str(data["artefact_path"]),
            lineage_entry_hash=str(data["lineage_entry_hash"]),
            executed_at_ns=_coerce_int(data["executed_at_ns"]),
            result_copy_relpath=(None if data.get("result_copy_relpath") is None else str(data["result_copy_relpath"])),
        )


def compute_binding(core: Mapping[str, object]) -> str:
    """Return the hex binding digest over a receipt-core mapping (JCS-style)."""
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """Result of an offline receipt verification."""

    ok: bool
    receipt_id: str
    checks: dict[str, bool]
    failures: list[str]


@dataclass(frozen=True, slots=True)
class DriftOutcome:
    """Result of a ``--re-execute`` drift check."""

    match: bool
    receipt_id: str
    recorded_hash: str
    live_hash: str
    recorded_row_count: int
    live_row_count: int

    @property
    def status(self) -> str:
        return "MATCH" if self.match else "DRIFT"


class QueryReceiptStore:
    """Records and verifies query receipts rooted at a datasource directory.

    Layout under ``root`` (typically ``<sdd>/datasources``)::

        lineage/                 - LineageStore (log.jsonl + .jws sidecars)
        receipts/<stem>.json     - one receipt per execution
        results/<content>.bin    - optional canonical result copies
        receipts-audit.jsonl     - additive audit mirror
    """

    def __init__(
        self,
        root: Path,
        *,
        agent_card: AgentCard,
        private_key_pem: str,
        operator_hmac_key: bytes,
        agent_id: str | None = None,
    ) -> None:
        self.root = Path(root)
        self._card = agent_card
        self._private_key_pem = private_key_pem
        self._operator_hmac_key = operator_hmac_key
        self._agent_id = agent_id or agent_card.agent_id
        self._store = LineageStore(self.root / "lineage")

    # -- paths --------------------------------------------------------------

    @property
    def receipts_dir(self) -> Path:
        return self.root / "receipts"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    @property
    def audit_path(self) -> Path:
        return self.root / "receipts-audit.jsonl"

    def receipt_path(self, receipt_id: str) -> Path:
        return self.receipts_dir / f"{_stem(receipt_id)}.json"

    # -- recording ----------------------------------------------------------

    def record(
        self,
        *,
        connection: DataSourceConnection,
        query_text: str,
        params: Sequence[object] | Mapping[str, object] | None,
        result: NormalizedResult,
        engine_name: str = "sqlite",
        tool_call_id: str = "",
        store_result_copy: bool = False,
        result_copy_cap: int = DEFAULT_RESULT_COPY_CAP,
        now_ns: int | None = None,
    ) -> QueryReceipt:
        """Canonicalise ``result``, append the signed lineage entry, write the receipt.

        The connection's DSN is never persisted here -- only ``connection.id``.
        Set ``store_result_copy`` to also persist the canonical bytes so an
        offline verifier can re-hash them (redaction-policy gated by the caller).
        """
        canonical = canonical_bytes(result)
        result_hash = _sha256_hex(canonical)
        qhash = hash_query_text(query_text)
        phash = hash_params(params)
        executed_at_ns = now_ns if now_ns is not None else time.time_ns()

        # Fold every receipt-core field into a binding digest carried in the
        # signed entry, so tampering any of them is detectable offline.
        core = {
            "v": RECEIPT_VERSION,
            "connection_id": connection.id,
            "driver": connection.driver,
            "engine": engine_name,
            "query_text_hash": qhash,
            "params_hash": phash,
            "content_hash": result_hash,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "row_cap": result.row_cap,
        }
        binding = compute_binding(core)

        artefact_path = _artefact_path(connection.id, qhash, phash)
        # ``span_id`` is deliberately repurposed here: instead of an OTel span
        # context, it carries the receipt-core ``binding`` digest so the binding
        # is signed and HMAC'd along with the rest of the entry. The seal path
        # does not consume ``span_id`` for telemetry (it neither opens a span
        # with it nor emits it), so the repurposing has no telemetry side effect.
        lineage_entry_hash = seal_write(
            self._store,
            self._operator_hmac_key,
            artefact_path=artefact_path,
            new_content=canonical,
            agent_id=self._agent_id,
            agent_card=self._card,
            private_key_pem=self._private_key_pem,
            tool_call_id=tool_call_id or f"query:{connection.id}",
            span_id=binding,
            artefact_kind="query-result",
        )

        result_copy_relpath: str | None = None
        if store_result_copy and len(canonical) <= result_copy_cap:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            copy_name = f"{_stem(result_hash)}.bin"
            (self.results_dir / copy_name).write_bytes(canonical)
            result_copy_relpath = f"results/{copy_name}"

        receipt = QueryReceipt(
            v=RECEIPT_VERSION,
            receipt_id=lineage_entry_hash,
            connection_id=connection.id,
            driver=connection.driver,
            engine=engine_name,
            query_text=query_text,
            query_text_hash=qhash,
            params=_json_safe_params(params),
            params_hash=phash,
            content_hash=result_hash,
            row_count=result.row_count,
            truncated=result.truncated,
            row_cap=result.row_cap,
            binding=binding,
            artefact_path=artefact_path,
            lineage_entry_hash=lineage_entry_hash,
            executed_at_ns=executed_at_ns,
            result_copy_relpath=result_copy_relpath,
        )

        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.receipt_path(lineage_entry_hash).write_text(
            json.dumps(receipt.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._append_audit(receipt)
        # Persist the public agent card so an offline verifier has the key.
        self._persist_card()
        return receipt

    def _append_audit(self, receipt: QueryReceipt) -> None:
        """Mirror an additive, secret-free audit event for the receipt."""
        record = {
            "event": AUDIT_EVENT,
            "timestamp": receipt.executed_at_ns / 1e9,
            "receipt_id": receipt.receipt_id,
            "connection_id": receipt.connection_id,
            "content_hash": receipt.content_hash,
            "query_text_hash": receipt.query_text_hash,
            "params_hash": receipt.params_hash,
            "row_count": receipt.row_count,
            "truncated": receipt.truncated,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def _persist_card(self) -> None:
        identity_dir = self.root / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)
        card_dir = identity_dir / self._card.agent_id
        card_dir.mkdir(parents=True, exist_ok=True)
        (card_dir / "card.json").write_text(
            json.dumps(
                {
                    "agent_id": self._card.agent_id,
                    "kid": self._card.kid,
                    "public_key_pem": self._card.public_key_pem,
                    "protocol_version": self._card.protocol_version,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    # -- loading ------------------------------------------------------------

    def load(self, receipt_id: str) -> QueryReceipt:
        path = self.receipt_path(receipt_id)
        if not path.exists():
            raise ReceiptNotFound(f"no receipt found for id {receipt_id!r}")
        return QueryReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_receipts(self) -> list[QueryReceipt]:
        if not self.receipts_dir.exists():
            return []
        out: list[QueryReceipt] = []
        for path in sorted(self.receipts_dir.glob("*.json")):
            out.append(QueryReceipt.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return out

    # -- verification -------------------------------------------------------

    def verify(self, receipt_id: str, *, check_operator_hmac: bool = True) -> VerifyOutcome:
        """Offline-verify a receipt against its signed lineage entry.

        Checks, in order, and names the first field that fails:

        * ``lineage_entry`` -- the anchor entry exists in the log,
        * ``signature`` -- its detached JWS verifies against the agent card,
        * ``operator_hmac`` -- its HMAC envelope recomputes (when enabled),
        * ``content_hash`` -- the signed entry's digest matches the receipt,
        * ``receipt_body`` -- the receipt-core binding matches the signed
          ``span_id``,
        * ``result_copy`` -- any stored result copy re-hashes to ``content_hash``.
        """
        receipt = self.load(receipt_id)
        checks: dict[str, bool] = {}
        failures: list[str] = []

        def fail(field: str, msg: str) -> None:
            checks[field] = False
            failures.append(f"{field}: {msg}")

        # 1. Locate the signed lineage entry by hash.
        entry = None
        jws = ""
        for candidate, candidate_jws in self._store.read_log():
            if entry_hash(candidate) == receipt.lineage_entry_hash:
                entry, jws = candidate, candidate_jws
                break
        if entry is None:
            fail("lineage_entry", "anchor entry missing from the log; receipt is unverifiable")
            return VerifyOutcome(ok=False, receipt_id=receipt.receipt_id, checks=checks, failures=failures)
        checks["lineage_entry"] = True

        # 2. Signature.
        card = self._load_card(entry.agent_id, entry.agent_card_kid)
        if card is None:
            fail("signature", f"no agent card for {entry.agent_id!r}/{entry.agent_card_kid!r}")
        elif not verify_detached(canonicalise(entry), jws, card):
            fail("signature", "detached JWS did not verify against the agent card")
        else:
            checks["signature"] = True

        # 3. Operator HMAC.
        if check_operator_hmac:
            expected = compute_operator_hmac(replace(entry, operator_hmac=""), self._operator_hmac_key)
            if expected != entry.operator_hmac:
                fail("operator_hmac", "HMAC envelope did not recompute")
            else:
                checks["operator_hmac"] = True

        # 4. Content hash (chain anchor binds the result digest).
        if entry.content_hash != receipt.content_hash:
            fail("content_hash", f"signed entry digest {entry.content_hash} != receipt {receipt.content_hash}")
        else:
            checks["content_hash"] = True

        # 5. Receipt body binding.
        recomputed = compute_binding(receipt.binding_core())
        if recomputed != entry.span_id:
            fail("receipt_body", "receipt-core binding does not match the signed entry")
        elif recomputed != receipt.binding:
            fail("receipt_body", "receipt.binding field was tampered")
        else:
            checks["receipt_body"] = True

        # 6. Stored result copy (optional).
        if receipt.result_copy_relpath is not None:
            copy_path = self.root / receipt.result_copy_relpath
            if not copy_path.exists():
                fail("result_copy", "stored result copy is missing")
            elif _sha256_hex(copy_path.read_bytes()) != receipt.content_hash:
                fail("result_copy", "stored result copy does not re-hash to content_hash")
            else:
                checks["result_copy"] = True

        return VerifyOutcome(ok=not failures, receipt_id=receipt.receipt_id, checks=checks, failures=failures)

    def _load_card(self, agent_id: str, kid: str) -> AgentCard | None:
        card_path = self.root / "identity" / agent_id / "card.json"
        if not card_path.exists():
            return None
        try:
            data = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("agent_id") != agent_id or data.get("kid") != kid:
            return None
        return AgentCard(
            agent_id=str(data["agent_id"]),
            kid=str(data["kid"]),
            public_key_pem=str(data["public_key_pem"]),
            protocol_version=str(data.get("protocol_version", "a2a/1.0")),
        )

    # -- drift --------------------------------------------------------------

    def reexecute(self, receipt_id: str, connection: DataSourceConnection) -> DriftOutcome:
        """Re-run the receipt's query live and report MATCH or DRIFT.

        Re-execution uses the receipt's recorded query text, parameters and row
        cap against ``connection`` (looked up by the operator, not by the
        receipt, so the live DSN is never taken from untrusted data).
        """
        receipt = self.load(receipt_id)
        engine = connection.open_engine()
        params = receipt.params if isinstance(receipt.params, (list, dict)) else None
        result = engine.execute(receipt.query_text, params, row_cap=receipt.row_cap)
        live_hash = content_hash(result)
        return DriftOutcome(
            match=(live_hash == receipt.content_hash),
            receipt_id=receipt.receipt_id,
            recorded_hash=receipt.content_hash,
            live_hash=live_hash,
            recorded_row_count=receipt.row_count,
            live_row_count=result.row_count,
        )


def _artefact_path(connection_id: str, query_text_hash: str, params_hash: str) -> str:
    """Repo-relative POSIX lineage path for a connection+query+params tuple.

    Identical query+params on a connection reuse the same path, so re-executions
    chain in the lineage log and a divergent result is a visible chain event.
    """
    return f".sdd/datasources/queries/{connection_id}/{_stem(query_text_hash)[:16]}_{_stem(params_hash)[:16]}.jsonl"


def _coerce_int(value: object) -> int:
    """Coerce a JSON-loaded scalar into ``int`` (receipts store ints as ints)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        return int(value)
    raise DataSourceError(f"receipt field is not an integer: {value!r}")


def _json_safe_params(params: Sequence[object] | Mapping[str, object] | None) -> object:
    """Coerce params into a JSON-serialisable projection for the receipt file."""
    if params is None:
        return None
    if isinstance(params, dict):
        return {str(k): _json_safe_scalar(v) for k, v in params.items()}
    return [_json_safe_scalar(v) for v in params]


def _json_safe_scalar(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


__all__ = [
    "AUDIT_EVENT",
    "DEFAULT_RESULT_COPY_CAP",
    "RECEIPT_VERSION",
    "DriftOutcome",
    "QueryReceipt",
    "QueryReceiptStore",
    "VerifyOutcome",
    "compute_binding",
    "hash_params",
    "hash_query_text",
]
