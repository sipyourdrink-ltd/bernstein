"""Signed recall receipts for chain-native memory reads.

A receipt binds one exact-query recall to the MemoryChain head and canonical
fold it read. The canonical receipt body is content-addressed, sealed through
the shared signed-lineage substrate, and mirrored as a hashes-only
``memory.recall`` audit event. Replaying the recorded head and query must select
the same ordered memory record hashes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from bernstein.core.lineage.entry import canonicalise, compute_operator_hmac, entry_hash
from bernstein.core.lineage.identity import AgentCard, verify_detached
from bernstein.core.lineage.signed_write import seal_write
from bernstein.core.lineage.store import LineageStore
from bernstein.core.memory.chain import (
    RECALL_SELECTOR_CLAIM_EXACT_V1,
    MemoryChain,
    MemoryChainStatus,
    MemoryReplayError,
    MemoryScope,
)
from bernstein.core.persistence.atomic_write import write_atomic_bytes
from bernstein.core.security.agent_card_signer import canonicalize_jcs
from bernstein.core.security.audit_chain import (
    EVENT_MEMORY_RECALL,
    AuditChainStore,
    record_memory_recall,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit import AuditEvent

RECALL_RECEIPT_VERSION = 1
_RECEIPT_ARTEFACT_KIND = "sdd-runtime"
_RECEIPT_SPAN_ID = "0" * 16


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _hash_stem(value: str) -> str:
    return value.split(":", 1)[1] if ":" in value else value


def hash_recall_query(query: str) -> str:
    """Return the UTF-8 content hash of the exact query used for selection."""
    return _sha256(query.encode("utf-8"))


def hash_record_hashes(record_hashes: tuple[str, ...]) -> str:
    """Return a canonical digest of an ordered selected-record tuple."""
    return _sha256(canonicalize_jcs(list(record_hashes)))


@dataclass(frozen=True, slots=True)
class MemoryRecallReceipt:
    """Content-addressed record of one non-empty chain-native recall."""

    v: int
    scope: str
    namespace: str
    query: str
    query_hash: str
    selector: str
    fold_head: str
    fold_hash: str
    record_hashes: tuple[str, ...]
    run_id: str
    step_id: str
    receipt_id: str
    lineage_entry_hash: str | None = None

    def body(self) -> dict[str, object]:
        """Return the canonical content-addressed receipt body."""
        return {
            "v": self.v,
            "scope": self.scope,
            "namespace": self.namespace,
            "query": self.query,
            "query_hash": self.query_hash,
            "selector": self.selector,
            "fold_head": self.fold_head,
            "fold_hash": self.fold_hash,
            "record_hashes": list(self.record_hashes),
            "run_id": self.run_id,
            "step_id": self.step_id,
        }

    def body_bytes(self) -> bytes:
        """Return RFC 8785 canonical bytes of :meth:`body`."""
        return canonicalize_jcs(self.body())

    def computed_receipt_id(self) -> str:
        """Return the SHA-256 content address of the canonical body."""
        return _sha256(self.body_bytes())

    def to_dict(self) -> dict[str, object]:
        out = self.body()
        out["receipt_id"] = self.receipt_id
        if self.lineage_entry_hash is not None:
            out["lineage_entry_hash"] = self.lineage_entry_hash
        return out

    @classmethod
    def from_dict(cls, row: dict[str, object]) -> MemoryRecallReceipt:
        version = row.get("v")
        hashes_raw = row.get("record_hashes")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("v must be an integer")
        if not isinstance(hashes_raw, list):
            raise ValueError("record_hashes must be a list of strings")
        hashes = cast("list[object]", hashes_raw)
        if not all(isinstance(value, str) for value in hashes):
            raise ValueError("record_hashes must be a list of strings")

        text_fields: dict[str, str] = {}
        for name in (
            "scope",
            "namespace",
            "query",
            "query_hash",
            "selector",
            "fold_head",
            "fold_hash",
            "run_id",
            "step_id",
            "receipt_id",
        ):
            value = row.get(name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            text_fields[name] = value

        anchor = row.get("lineage_entry_hash")
        if anchor is not None and not isinstance(anchor, str):
            raise ValueError("lineage_entry_hash must be a string when present")
        return cls(
            v=version,
            scope=text_fields["scope"],
            namespace=text_fields["namespace"],
            query=text_fields["query"],
            query_hash=text_fields["query_hash"],
            selector=text_fields["selector"],
            fold_head=text_fields["fold_head"],
            fold_hash=text_fields["fold_hash"],
            record_hashes=tuple(cast("list[str]", hashes)),
            run_id=text_fields["run_id"],
            step_id=text_fields["step_id"],
            receipt_id=text_fields["receipt_id"],
            lineage_entry_hash=anchor,
        )

    def with_anchor(self, lineage_entry_hash: str) -> MemoryRecallReceipt:
        """Return a copy with the signed-lineage anchor attached."""
        return MemoryRecallReceipt(
            v=self.v,
            scope=self.scope,
            namespace=self.namespace,
            query=self.query,
            query_hash=self.query_hash,
            selector=self.selector,
            fold_head=self.fold_head,
            fold_hash=self.fold_hash,
            record_hashes=self.record_hashes,
            run_id=self.run_id,
            step_id=self.step_id,
            receipt_id=self.receipt_id,
            lineage_entry_hash=lineage_entry_hash,
        )


@dataclass(frozen=True, slots=True)
class MemoryRecallVerification:
    """Offline verification result for a stored recall receipt."""

    ok: bool
    receipt_id: str
    checks: dict[str, bool]
    failures: list[str]


def receipts_dir(workdir: Path) -> Path:
    """Return the directory containing persisted memory recall receipts."""
    return workdir / ".sdd" / "memory" / "recall" / "receipts"


def receipt_artefact_path(receipt_id: str) -> str:
    """Return the repo-relative lineage artefact key for a recall receipt."""
    return f".sdd/memory/recall/receipts/{_hash_stem(receipt_id)}.json"


def _identity_card_path(workdir: Path, agent_id: str, kid: str) -> Path:
    """Return a Windows-safe deterministic path for one persisted agent card."""
    identity_key = hashlib.sha256(canonicalize_jcs({"agent_id": agent_id, "kid": kid})).hexdigest()
    return workdir / ".sdd" / "memory" / "recall" / "identity" / identity_key / "card.json"


class MemoryRecallReceiptStore:
    """Record and offline-verify chain-native recall receipts for one workdir."""

    def __init__(
        self,
        workdir: Path,
        *,
        agent_card: AgentCard,
        private_key_pem: str,
        operator_hmac_key: bytes,
    ) -> None:
        self.workdir = Path(workdir)
        self._card = agent_card
        self._private_key_pem = private_key_pem
        self._operator_hmac_key = operator_hmac_key
        self._memory_chain = MemoryChain(
            self.workdir / ".sdd" / "memory" / "chain",
            hmac_key=operator_hmac_key,
        )

    def receipt_path(self, receipt_id: str) -> Path:
        return receipts_dir(self.workdir) / f"{_hash_stem(receipt_id)}.json"

    def record_exact(
        self,
        *,
        scope: MemoryScope,
        namespace: str,
        query: str,
        run_id: str,
        step_id: str,
        fold_head: str | None = None,
        ts_ns: int | None = None,
    ) -> MemoryRecallReceipt | None:
        """Seal a receipt for an exact recall; return ``None`` for no match.

        Empty namespaces and exact queries selecting no live record perform no
        lineage, audit, identity, or receipt writes.

        The namespace is fully verified, including its lineage-spine anchors,
        before anything is sealed. That verification scans the namespace and
        its referenced spines, so recording cost grows with chain length.
        """
        selection = self._memory_chain.recall_exact(
            query,
            scope=scope,
            namespace=namespace,
            fold_head=fold_head,
        )
        if selection.fold_head == "" and not selection.entries:
            return None

        chain_result = self._memory_chain.verify(
            scope,
            namespace,
            spine_root=self.workdir / ".sdd" / "lineage",
        )
        if chain_result.status is not MemoryChainStatus.OK:
            detail = "; ".join(chain_result.errors) or chain_result.status.value
            raise MemoryReplayError(f"memory chain does not verify: {detail}")
        if not selection.entries:
            return None

        query_hash = hash_recall_query(query)
        draft = MemoryRecallReceipt(
            v=RECALL_RECEIPT_VERSION,
            scope=scope.value,
            namespace=namespace,
            query=query,
            query_hash=query_hash,
            selector=selection.selector,
            fold_head=selection.fold_head,
            fold_hash=selection.fold_hash,
            record_hashes=selection.record_hashes,
            run_id=run_id,
            step_id=step_id,
            receipt_id="",
        )
        receipt_id = draft.computed_receipt_id()
        receipt = MemoryRecallReceipt(
            v=draft.v,
            scope=draft.scope,
            namespace=draft.namespace,
            query=draft.query,
            query_hash=draft.query_hash,
            selector=draft.selector,
            fold_head=draft.fold_head,
            fold_hash=draft.fold_hash,
            record_hashes=draft.record_hashes,
            run_id=draft.run_id,
            step_id=draft.step_id,
            receipt_id=receipt_id,
        )

        self._persist_exact_bytes(
            self.receipt_path(receipt_id),
            receipt.body_bytes(),
            label="recall receipt",
        )
        self._persist_card()

        lineage_store = LineageStore(self.workdir / ".sdd" / "lineage")
        lineage_entry_hash = self._existing_lineage_anchor(lineage_store, receipt)
        if lineage_entry_hash is None:
            lineage_entry_hash = seal_write(
                lineage_store,
                self._operator_hmac_key,
                artefact_path=receipt_artefact_path(receipt_id),
                new_content=receipt.body_bytes(),
                agent_id=self._card.agent_id,
                agent_card=self._card,
                private_key_pem=self._private_key_pem,
                tool_call_id=receipt_id,
                span_id=_RECEIPT_SPAN_ID,
                artefact_kind=_RECEIPT_ARTEFACT_KIND,
                ts_ns=ts_ns,
            )
        anchored = receipt.with_anchor(lineage_entry_hash)

        self._ensure_audit_event(anchored)
        return anchored

    @staticmethod
    def _persist_exact_bytes(path: Path, payload: bytes, *, label: str) -> None:
        if path.exists():
            existing = path.read_bytes()
            if existing != payload:
                raise MemoryReplayError(f"existing {label} bytes do not match the expected content address")
            return
        write_atomic_bytes(path, payload)

    def _existing_lineage_anchor(self, store: LineageStore, receipt: MemoryRecallReceipt) -> str | None:
        artefact_path = receipt_artefact_path(receipt.receipt_id)
        try:
            candidates = [(entry, jws) for entry, jws in store.read_log() if entry.artefact_path == artefact_path]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise MemoryReplayError("existing recall lineage could not be read safely") from exc
        if not candidates:
            return None
        if len(candidates) != 1:
            raise MemoryReplayError("existing recall lineage is ambiguous; expected exactly one entry")

        entry, jws = candidates[0]
        valid = (
            entry.content_hash == receipt.receipt_id
            and entry.artefact_kind == _RECEIPT_ARTEFACT_KIND
            and entry.agent_id == self._card.agent_id
            and entry.agent_card_kid == self._card.kid
            and entry.tool_call_id == receipt.receipt_id
            and entry.span_id == _RECEIPT_SPAN_ID
            and compute_operator_hmac(entry, self._operator_hmac_key) == entry.operator_hmac
            and bool(jws)
            and verify_detached(canonicalise(entry), jws, self._card)
        )
        if not valid:
            raise MemoryReplayError("existing recall lineage does not match the receipt and current signer")
        return entry_hash(entry)

    def _ensure_audit_event(self, receipt: MemoryRecallReceipt) -> None:
        if receipt.lineage_entry_hash is None:
            raise MemoryReplayError("cannot record recall audit event without a lineage anchor")

        audit_chain = AuditChainStore(self.workdir / ".sdd" / "audit", key=self._operator_hmac_key)
        audit_ok, audit_errors, events = audit_chain.verify_and_query(
            event_type=EVENT_MEMORY_RECALL,
            resource_id=receipt.receipt_id,
            include_archived=True,
        )
        if not audit_ok:
            raise MemoryReplayError("audit chain does not verify: " + "; ".join(audit_errors))

        records_hash = hash_record_hashes(receipt.record_hashes)

        def event_matches(event: AuditEvent) -> bool:
            details = event.details
            return (
                event.actor == self._card.agent_id
                and details.get("receipt_id") == receipt.receipt_id
                and details.get("lineage_entry_hash") == receipt.lineage_entry_hash
                and details.get("scope") == receipt.scope
                and details.get("namespace") == receipt.namespace
                and details.get("run_id") == receipt.run_id
                and details.get("step_id") == receipt.step_id
                and details.get("query_hash") == receipt.query_hash
                and details.get("fold_head") == receipt.fold_head
                and details.get("fold_hash") == receipt.fold_hash
                and details.get("records_hash") == records_hash
                and details.get("record_count") == len(receipt.record_hashes)
            )

        if events:
            if not all(event_matches(event) for event in events):
                raise MemoryReplayError("existing memory.recall audit event conflicts with the receipt")
            return

        record_memory_recall(
            chain=audit_chain,
            receipt_id=receipt.receipt_id,
            lineage_entry_hash=receipt.lineage_entry_hash,
            scope=receipt.scope,
            namespace=receipt.namespace,
            actor=self._card.agent_id,
            run_id=receipt.run_id,
            step_id=receipt.step_id,
            query_hash=receipt.query_hash,
            fold_head=receipt.fold_head,
            fold_hash=receipt.fold_hash,
            records_hash=records_hash,
            record_count=len(receipt.record_hashes),
        )

    def load(self, receipt_id: str) -> MemoryRecallReceipt:
        """Load the persisted canonical receipt body by content address."""
        path = self.receipt_path(receipt_id)
        if not path.exists():
            raise FileNotFoundError(f"no memory recall receipt stored at {path}")
        row_raw = json.loads(path.read_bytes())
        if not isinstance(row_raw, dict):
            raise ValueError("memory recall receipt is not an object")
        row = cast("dict[str, object]", row_raw)
        row["receipt_id"] = receipt_id
        return MemoryRecallReceipt.from_dict(row)

    def _persist_card(self) -> None:
        path = _identity_card_path(self.workdir, self._card.agent_id, self._card.kid)
        payload = canonicalize_jcs(
            {
                "agent_id": self._card.agent_id,
                "kid": self._card.kid,
                "public_key_pem": self._card.public_key_pem,
                "protocol_version": self._card.protocol_version,
            }
        )
        self._persist_exact_bytes(path, payload, label="recall identity card")

    def _load_card(self, agent_id: str, kid: str) -> AgentCard | None:
        path = _identity_card_path(self.workdir, agent_id, kid)
        if not path.exists():
            return None
        try:
            row_raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(row_raw, dict):
            return None
        row = cast("dict[str, object]", row_raw)
        public_key_pem = row.get("public_key_pem")
        protocol_version = row.get("protocol_version", "a2a/1.0")
        if (
            row.get("agent_id") != agent_id
            or row.get("kid") != kid
            or not isinstance(public_key_pem, str)
            or not isinstance(protocol_version, str)
        ):
            return None
        return AgentCard(
            agent_id=agent_id,
            kid=kid,
            public_key_pem=public_key_pem,
            protocol_version=protocol_version,
        )

    def verify(self, receipt_id: str) -> MemoryRecallVerification:
        """Verify the receipt, signed lineage, audit mirror, and replayed recall."""
        try:
            stored_bytes = self.receipt_path(receipt_id).read_bytes()
            receipt = self.load(receipt_id)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return MemoryRecallVerification(
                ok=False,
                receipt_id=receipt_id,
                checks={"receipt_file": False, "receipt_body": False},
                failures=[f"receipt_file: stored receipt cannot be verified ({type(exc).__name__})"],
            )
        checks: dict[str, bool] = {}
        failures: list[str] = []

        def check(name: str, passed: bool, message: str) -> None:
            checks[name] = passed
            if not passed:
                failures.append(f"{name}: {message}")

        recomputed_id = receipt.computed_receipt_id()
        check(
            "receipt_body",
            receipt.v == RECALL_RECEIPT_VERSION
            and receipt.receipt_id == receipt_id
            and recomputed_id == receipt.receipt_id
            and stored_bytes == receipt.body_bytes()
            and _sha256(stored_bytes) == receipt.receipt_id,
            "canonical receipt body does not match its content address",
        )
        check(
            "query_hash",
            receipt.query_hash == hash_recall_query(receipt.query),
            "query hash does not match the recorded query",
        )
        check(
            "selector",
            receipt.selector == RECALL_SELECTOR_CLAIM_EXACT_V1,
            "unsupported recall selector",
        )

        lineage_store = LineageStore(self.workdir / ".sdd" / "lineage")
        found = None
        try:
            candidates = [
                (candidate, jws)
                for candidate, jws in lineage_store.read_log()
                if candidate.artefact_path == receipt_artefact_path(receipt.receipt_id)
            ]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            candidates = []
        if len(candidates) == 1:
            found = candidates[0]
        elif len(candidates) > 1:
            failures.append("lineage_entry: multiple lineage entries exist for one recall receipt")
        check("lineage_entry", found is not None, "signed lineage entry is missing")

        lineage_entry_hash = ""
        actor = ""
        if found is not None:
            entry, jws = found
            actor = entry.agent_id
            card = self._load_card(entry.agent_id, entry.agent_card_kid)
            signed = card is not None and bool(jws) and verify_detached(canonicalise(entry), jws, card)
            check("signature", signed, "detached JWS is missing or invalid")
            check(
                "operator_hmac",
                compute_operator_hmac(entry, self._operator_hmac_key) == entry.operator_hmac,
                "lineage operator HMAC does not verify",
            )
            check(
                "lineage_content",
                entry.content_hash == recomputed_id
                and entry.artefact_path == receipt_artefact_path(receipt.receipt_id)
                and entry.artefact_kind == _RECEIPT_ARTEFACT_KIND,
                "lineage entry does not bind the receipt body and artefact path",
            )
            lineage_entry_hash = entry_hash(entry)
        else:
            check("signature", False, "cannot verify a missing lineage entry")
            check("operator_hmac", False, "cannot verify a missing lineage entry")
            check("lineage_content", False, "cannot verify a missing lineage entry")

        audit_chain = AuditChainStore(self.workdir / ".sdd" / "audit", key=self._operator_hmac_key)
        audit_ok, audit_errors, events = audit_chain.verify_and_query(
            event_type=EVENT_MEMORY_RECALL,
            resource_id=receipt.receipt_id,
            include_archived=True,
        )
        check("audit_chain", audit_ok, "; ".join(audit_errors) or "audit chain does not verify")
        records_hash = hash_record_hashes(receipt.record_hashes)
        event_ok = any(
            event.actor == actor
            and event.details.get("receipt_id") == receipt.receipt_id
            and event.details.get("lineage_entry_hash") == lineage_entry_hash
            and event.details.get("scope") == receipt.scope
            and event.details.get("namespace") == receipt.namespace
            and event.details.get("run_id") == receipt.run_id
            and event.details.get("step_id") == receipt.step_id
            and event.details.get("query_hash") == receipt.query_hash
            and event.details.get("fold_head") == receipt.fold_head
            and event.details.get("fold_hash") == receipt.fold_hash
            and event.details.get("records_hash") == records_hash
            and event.details.get("record_count") == len(receipt.record_hashes)
            for event in events
        )
        check("audit_event", event_ok, "matching memory.recall audit event is missing or divergent")

        try:
            scope = MemoryScope(receipt.scope)
        except ValueError:
            scope = None
        chain_ok = False
        replay_ok = False
        if scope is not None:
            try:
                chain_result = self._memory_chain.verify(
                    scope,
                    receipt.namespace,
                    spine_root=self.workdir / ".sdd" / "lineage",
                )
                chain_ok = chain_result.status is MemoryChainStatus.OK
                if chain_ok:
                    replay = self._memory_chain.recall_exact(
                        receipt.query,
                        scope=scope,
                        namespace=receipt.namespace,
                        fold_head=receipt.fold_head,
                    )
                    replay_ok = (
                        replay.selector == receipt.selector
                        and replay.fold_hash == receipt.fold_hash
                        and replay.record_hashes == receipt.record_hashes
                    )
            except (MemoryReplayError, OSError, ValueError):
                chain_ok = False
                replay_ok = False
        check("memory_chain", chain_ok, "memory chain does not verify")
        check("recall_replay", replay_ok, "historical fold/query replay selected different evidence")

        return MemoryRecallVerification(
            ok=not failures,
            receipt_id=receipt.receipt_id,
            checks=checks,
            failures=failures,
        )


__all__ = [
    "RECALL_RECEIPT_VERSION",
    "MemoryRecallReceipt",
    "MemoryRecallReceiptStore",
    "MemoryRecallVerification",
    "hash_recall_query",
    "hash_record_hashes",
    "receipt_artefact_path",
    "receipts_dir",
]
