"""Typed task-completion record: schema, validation, and canonical serialization (#4185).

Provides a structured completion record containing exports, assumptions,
receipt references, and reopen triggers. Enforces strict boundary validation,
canonical serialization (excluding wall-clock envelope), and hash referencing integrity.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

_HASH_RE = re.compile(r"^(?:sha256:)?[a-f0-9]{64}$", re.IGNORECASE)

KNOWN_TOP_LEVEL_FIELDS = {
    "task_id",
    "exports",
    "assumptions",
    "receipt_refs",
    "reopen_triggers",
    "timestamp",
}

KNOWN_EXPORT_FIELDS = {"path", "content_hash"}
KNOWN_ASSUMPTION_FIELDS = {"kind", "key_or_path", "expected_value_or_hash", "note"}
KNOWN_TRIGGER_FIELDS = {"kind", "referenced_hash"}


class ReopenTriggerKind(StrEnum):
    """Evidence conditions that can reopen a completed task."""

    ASSUMPTION_HASH_MISMATCH = "assumption-hash-mismatch"
    RECEIPT_INVALIDATED = "receipt-invalidated"


class CompletionRecordValidationError(ValueError):
    """Raised when a task completion record violates boundary validation rules."""


def _is_valid_hash(h: str) -> bool:
    if not isinstance(h, str):
        return False
    return bool(_HASH_RE.match(h.strip()))


@dataclass(frozen=True)
class ExportItem:
    """An exported artifact path and its recorded content hash."""

    path: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "content_hash": self.content_hash}


@dataclass(frozen=True)
class AssumptionItem:
    """An expected file hash or configuration value used by a completion record."""

    kind: Literal["file", "config"]
    key_or_path: str
    expected_value_or_hash: str
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "key_or_path": self.key_or_path,
            "expected_value_or_hash": self.expected_value_or_hash,
        }
        if self.note is not None:
            d["note"] = self.note
        return d


@dataclass(frozen=True)
class ReopenTriggerItem:
    """A completion reopening condition tied to a specific evidence hash."""

    kind: ReopenTriggerKind
    referenced_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value if isinstance(self.kind, ReopenTriggerKind) else str(self.kind),
            "referenced_hash": self.referenced_hash,
        }


@dataclass(frozen=True)
class TaskCompletionRecord:
    """Typed task completion record backing verification and automated reopen logic."""

    task_id: str
    exports: tuple[ExportItem, ...] = ()
    assumptions: tuple[AssumptionItem, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    reopen_triggers: tuple[ReopenTriggerItem, ...] = ()
    timestamp: float | None = None

    def canonical_dict(self) -> dict[str, Any]:
        """Return canonical dictionary excluding timestamp envelope."""
        return {
            "task_id": self.task_id,
            "exports": [e.to_dict() for e in self.exports],
            "assumptions": [a.to_dict() for a in self.assumptions],
            "receipt_refs": list(self.receipt_refs),
            "reopen_triggers": [t.to_dict() for t in self.reopen_triggers],
        }

    def canonical_bytes(self) -> bytes:
        """Return deterministic JSON bytes excluding timestamp envelope."""
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        """Serialise full record dictionary including optional timestamp."""
        d = self.canonical_dict()
        if self.timestamp is not None:
            d["timestamp"] = self.timestamp
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskCompletionRecord:
        """Parse dict with strict validation."""
        validate_completion_record_dict(data)

        exports = tuple(ExportItem(path=e["path"], content_hash=e["content_hash"]) for e in data.get("exports", []))
        assumptions = tuple(
            AssumptionItem(
                kind=a["kind"],
                key_or_path=a["key_or_path"],
                expected_value_or_hash=a["expected_value_or_hash"],
                note=a.get("note"),
            )
            for a in data.get("assumptions", [])
        )
        receipt_refs = tuple(data.get("receipt_refs", []))
        reopen_triggers = tuple(
            ReopenTriggerItem(
                kind=ReopenTriggerKind(t["kind"]),
                referenced_hash=t["referenced_hash"],
            )
            for t in data.get("reopen_triggers", [])
        )

        record = cls(
            task_id=data["task_id"],
            exports=exports,
            assumptions=assumptions,
            receipt_refs=receipt_refs,
            reopen_triggers=reopen_triggers,
            timestamp=data.get("timestamp"),
        )
        validate_completion_record(record)
        return record


def validate_completion_record_dict(data: dict[str, Any]) -> None:
    """Enforce strict schema validation on raw dictionary before parsing."""
    if not isinstance(data, dict):
        raise CompletionRecordValidationError("Completion record payload must be a JSON dictionary")

    unknown = set(data.keys()) - KNOWN_TOP_LEVEL_FIELDS
    if unknown:
        raise CompletionRecordValidationError(f"Unknown top-level field(s) rejected: {sorted(unknown)}")

    if "task_id" not in data or not isinstance(data["task_id"], str) or not data["task_id"].strip():
        raise CompletionRecordValidationError("Field 'task_id' must be a non-empty string")

    if "exports" in data:
        if not isinstance(data["exports"], list):
            raise CompletionRecordValidationError("Field 'exports' must be a list")
        for i, item in enumerate(data["exports"]):
            if not isinstance(item, dict):
                raise CompletionRecordValidationError(f"exports[{i}] must be a dict")
            unk_exp = set(item.keys()) - KNOWN_EXPORT_FIELDS
            if unk_exp:
                raise CompletionRecordValidationError(f"exports[{i}] contains unknown fields: {sorted(unk_exp)}")
            if "path" not in item or not isinstance(item["path"], str) or not item["path"].strip():
                raise CompletionRecordValidationError(f"exports[{i}] must contain a non-empty 'path'")
            if "content_hash" not in item or not _is_valid_hash(item["content_hash"]):
                raise CompletionRecordValidationError(f"exports[{i}] contains malformed 'content_hash'")

    if "assumptions" in data:
        if not isinstance(data["assumptions"], list):
            raise CompletionRecordValidationError("Field 'assumptions' must be a list")
        for i, item in enumerate(data["assumptions"]):
            if not isinstance(item, dict):
                raise CompletionRecordValidationError(f"assumptions[{i}] must be a dict")
            unk_ass = set(item.keys()) - KNOWN_ASSUMPTION_FIELDS
            if unk_ass:
                raise CompletionRecordValidationError(f"assumptions[{i}] contains unknown fields: {sorted(unk_ass)}")
            if item.get("kind") not in ("file", "config"):
                raise CompletionRecordValidationError(f"assumptions[{i}] 'kind' must be 'file' or 'config'")
            if "key_or_path" not in item or not isinstance(item["key_or_path"], str) or not item["key_or_path"].strip():
                raise CompletionRecordValidationError(f"assumptions[{i}] must contain a non-empty 'key_or_path'")
            if (
                "expected_value_or_hash" not in item
                or not isinstance(item["expected_value_or_hash"], str)
                or not item["expected_value_or_hash"].strip()
            ):
                raise CompletionRecordValidationError(
                    f"assumptions[{i}] must contain a non-empty 'expected_value_or_hash'"
                )
            if item["kind"] == "file" and not _is_valid_hash(item["expected_value_or_hash"]):
                raise CompletionRecordValidationError(f"assumptions[{i}] contains malformed 'expected_value_or_hash'")

    if "receipt_refs" in data:
        if not isinstance(data["receipt_refs"], list):
            raise CompletionRecordValidationError("Field 'receipt_refs' must be a list")
        for i, ref in enumerate(data["receipt_refs"]):
            if not _is_valid_hash(ref):
                raise CompletionRecordValidationError(f"receipt_refs[{i}] contains malformed receipt hash: {ref!r}")

    if "reopen_triggers" in data:
        if not isinstance(data["reopen_triggers"], list):
            raise CompletionRecordValidationError("Field 'reopen_triggers' must be a list")
        valid_kinds = {k.value for k in ReopenTriggerKind}
        for i, item in enumerate(data["reopen_triggers"]):
            if not isinstance(item, dict):
                raise CompletionRecordValidationError(f"reopen_triggers[{i}] must be a dict")
            unk_trig = set(item.keys()) - KNOWN_TRIGGER_FIELDS
            if unk_trig:
                raise CompletionRecordValidationError(
                    f"reopen_triggers[{i}] contains unknown fields: {sorted(unk_trig)}"
                )
            if item.get("kind") not in valid_kinds:
                raise CompletionRecordValidationError(
                    f"reopen_triggers[{i}] has invalid trigger kind: {item.get('kind')!r}"
                )
            if "referenced_hash" not in item or not _is_valid_hash(item.get("referenced_hash", "")):
                raise CompletionRecordValidationError(f"reopen_triggers[{i}] contains malformed 'referenced_hash'")


def validate_completion_record(record: TaskCompletionRecord) -> None:
    """Validate internal semantic consistency of a TaskCompletionRecord."""
    # AC: A claim without evidence doesn't serialize: a record whose receipt_refs
    # is empty while exports assert verification fails validation
    if record.exports and not record.receipt_refs:
        raise CompletionRecordValidationError(
            "Section 'receipt_refs' cannot be empty when section 'exports' asserts verification"
        )

    # Collect all known hashes declared within the record's sections
    known_hashes: set[str] = set()
    for e in record.exports:
        known_hashes.add(e.content_hash.lower())
        if e.content_hash.startswith("sha256:"):
            known_hashes.add(e.content_hash[7:].lower())
    for a in record.assumptions:
        if a.kind == "file":
            known_hashes.add(a.expected_value_or_hash.lower())
            if a.expected_value_or_hash.startswith("sha256:"):
                known_hashes.add(a.expected_value_or_hash[7:].lower())
    for r in record.receipt_refs:
        known_hashes.add(r.lower())
        if r.startswith("sha256:"):
            known_hashes.add(r[7:].lower())

    # AC: Every trigger referencing a hash present nowhere in the record is rejected
    for i, t in enumerate(record.reopen_triggers):
        target = t.referenced_hash.lower()
        target_clean = target[7:] if target.startswith("sha256:") else target
        if target not in known_hashes and target_clean not in known_hashes:
            raise CompletionRecordValidationError(
                f"reopen_triggers[{i}] references hash {t.referenced_hash!r} present nowhere in record's sections"
            )
