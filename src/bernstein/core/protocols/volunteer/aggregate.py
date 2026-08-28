"""Volunteer receipt aggregate — append-only proof-of-reward ledger.

A ``ReceiptAggregate`` holds a list of Merkle-tree-style receipts
and a current root computed as the SHA-256 of the sorted list of
envelope hashes.  This provides an auditable, tamper-evident ledger
of all merge rewards.

Design decisions
----------------

* **Append-only.**  Entries can only be added; existing entries
  cannot be mutated.  This is enforced by the API — there is no
  ``remove`` method.

* **Deterministic root.**  The root is ``sha256`` of the sorted list
  of ``envelope_hash`` strings, encoded as a JSON array.
  Sorting ensures the root is order-independent over the set of
  receipts, and re-computing it always yields the same digest for the
  same set of hashes.

* **Minimal footprint.**  Each receipt entry stores only what is
  needed to prove provenance: the document kind, the envelope hash,
  and the attestation timestamp.  The full envelope is not stored
  here — it is retrieved by hash from the envelope store if needed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.security.audit_dsse import Envelope


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version for the receipt aggregate.
AGGREGATE_SCHEMA_VERSION: str = "1.0.0"


# ---------------------------------------------------------------------------
# ReceiptAggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceiptAggregate:
    """An append-only proof-of-reward ledger.

    Attributes:
        receipts: List of receipt entries.  Each entry is a dict with
            ``document_kind`` (str), ``envelope_hash`` (str), and
            ``attested_at`` (str, ISO-8601).
        current_root: SHA-256 hex digest of the sorted list of
            ``envelope_hash`` values.  Empty list → empty root.
        schema_version: Document schema version.
        updated_at: ISO-8601 timestamp of the last update.
    """

    receipts: list[dict[str, Any]] = field(default_factory=list)
    current_root: str = ""
    schema_version: str = AGGREGATE_SCHEMA_VERSION
    updated_at: str = ""

    # -----------------------------------------------------------------------
    # Core operations
    # -----------------------------------------------------------------------

    def add_receipt(self, envelope: Envelope) -> ReceiptAggregate:
        """Append a receipt entry and recompute the root.

        Returns a new ``ReceiptAggregate`` (the dataclass is frozen, so
        mutation is via ``object.__setattr__`` to update in place).

        Args:
            envelope: The signed DSSE envelope to record.

        Returns:
            ``self`` (updated in place) so callers can chain.
        """
        # Extract envelope hash (SHA-256 of the canonical payload bytes).
        payload_bytes = envelope.payload_bytes
        envelope_hash = hashlib.sha256(payload_bytes).hexdigest()

        # Extract attested_at from the statement payload.
        statement = envelope.statement
        subject = statement.get("subject", [])
        attested_at = ""
        if isinstance(subject, list) and subject:
            first = subject[0]
            if isinstance(first, dict):
                # We use the subject digest as a proxy — but the real
                # timestamp lives in the predicate document's verified_at.
                # For the aggregate we record the hash of the payload.
                pass

        # Read attested_at from the embedded document if present.
        predicate = statement.get("predicate", {})
        if isinstance(predicate, dict):
            doc = predicate.get("document", {})
            if isinstance(doc, dict):
                val = doc.get("verified_at") or doc.get("merged_at")
                attested_at = val if val else ""

        entry: dict[str, Any] = {
            "document_kind": predicate.get("document_kind", "") if isinstance(predicate, dict) else "",
            "envelope_hash": envelope_hash,
            "attested_at": attested_at,
        }

        new_receipts = [*list(self.receipts), entry]
        # Sort envelope hashes to make root deterministic over the set.
        sorted_hashes = sorted([r["envelope_hash"] for r in new_receipts])
        root_bytes = hashlib.sha256(
            json.dumps(sorted_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        object.__setattr__(self, "receipts", new_receipts)
        object.__setattr__(self, "current_root", root_bytes)
        if attested_at and not self.updated_at:
            object.__setattr__(self, "updated_at", attested_at)
        return self

    def verify_root(self) -> bool:
        """Recompute the root from receipts and compare with current_root.

        Returns:
            True if the root matches the recomputed root.
        """
        if not self.receipts:
            return self.current_root == ""
        sorted_hashes = sorted([r["envelope_hash"] for r in self.receipts])
        root_bytes = hashlib.sha256(
            json.dumps(sorted_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return root_bytes == self.current_root

    # -----------------------------------------------------------------------
    # Serialisation
    # -----------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the aggregate as a serialisable dict."""
        return {
            "schema_version": self.schema_version,
            "receipts": list(self.receipts),
            "current_root": self.current_root,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReceiptAggregate:
        """Reconstruct a ``ReceiptAggregate`` from its dict representation."""
        return cls(
            receipts=data.get("receipts", []),
            current_root=data.get("current_root", ""),
            schema_version=data.get("schema_version", AGGREGATE_SCHEMA_VERSION),
            updated_at=data.get("updated_at", ""),
        )

    # -----------------------------------------------------------------------
    # Equality / hashing
    # -----------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ReceiptAggregate):
            return NotImplemented
        return (
            self.receipts == other.receipts
            and self.current_root == other.current_root
            and self.schema_version == other.schema_version
            and self.updated_at == other.updated_at
        )

    def __hash__(self) -> int:
        return hash(
            (
                tuple((r["document_kind"], r["envelope_hash"], r["attested_at"]) for r in self.receipts),
                self.current_root,
                self.schema_version,
                self.updated_at,
            )
        )
