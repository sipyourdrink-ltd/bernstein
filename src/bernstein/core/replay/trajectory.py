"""Step-granular trajectory rows projected from the per-step journal (#2926).

The per-step journal (``bernstein.core.persistence.journal``, #1799) already
hash-chains every step of an agent run. What it does not offer is a
**schema-stable row** a downstream consumer can read without knowing journal
internals: today a consumer either takes run-level aggregates, which have no
notion of individual steps, or reaches into the journal's own record shape.

This module is that row. :class:`TrajectoryStep` is one executed step in
consumer vocabulary - ``observation`` / ``action`` / ``outcome`` - carrying
the chain fields (``index``, ``prev_step_hash``, ``step_hash``) that let a
verifier walk the trajectory offline.

Re-emit, never recompute (load-bearing)
---------------------------------------
The projection **copies** ``step_hash`` from the journal entry. It never
calls :func:`~bernstein.core.persistence.journal.compute_step_hash`. That is
the whole point of the row: the hash it carries is the hash the run
committed, so a row whose ``observation`` was edited after the fact still
carries the original digest and the edit stays detectable. If the projection
re-derived the hash, an edited row would carry a hash that agrees with the
edit, and the export would attest only to itself.

Canonical encoding contract
---------------------------
:meth:`TrajectoryStep.canonical_bytes` follows the same discipline as
``canonical_step_payload`` in the journal - ``json.dumps(..., sort_keys=True,
separators=(",", ":"))``, UTF-8 - so two exports of one recorded run produce
byte-identical rows. Wall-clock (``ts``) is excluded, exactly as it is
excluded from the step hash, so nothing about *when* the export ran enters
the bytes.

``effort`` keeps the journal's back-compat rule of versioning by omission: an
unrecorded (``None``) effort leaves the key out of the document entirely, so
a row that predates the effort dimension canonicalises exactly as it did
before that dimension existed.

Scope: library-only. The signed manifest that binds a dataset to the replay
head, the lineage spine and the audit chain, the redaction projection, and
the ``bernstein trajectory`` CLI are separate, later work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.persistence.journal import JournalEntry, JournalReader

__all__ = [
    "TRAJECTORY_SCHEMA_VERSION",
    "TrajectoryStep",
    "project_trajectory",
    "trajectory_step_from_entry",
]

#: Trajectory row schema version. This is a public contract read by consumers
#: outside bernstein; bump it on any change to the field set or the canonical
#: encoding, and document the migration alongside the journal's own contract.
TRAJECTORY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TrajectoryStep:
    """One executed step of an agent run, in consumer vocabulary.

    Attributes:
        index: Position in the run, from the journal entry's ``seq``.
        prev_step_hash: ``step_hash`` of the preceding step, or the journal's
            genesis hash for the first step. Lets a consumer walk the chain
            without reading the journal.
        step_hash: The hash the run committed for this step, copied verbatim
            from the journal. Never recomputed here.
        input_hash: SHA-256 hex of the input blob the step was driven by. Part
            of the hashed document, so it is carried as its own column - a
            verifier needs it to re-derive ``step_hash`` by hand.
        observation: The prompt the adapter received, or ``None``.
        action: The serialised tool invocation, or ``None``.
        outcome: The serialised tool result, or ``None``.
        model: Model id the step was routed to, or ``None``.
        effort: Reasoning effort the step was routed at, or ``None`` for a row
            recorded before the effort dimension existed. Omitted from
            :meth:`canonical_bytes` when ``None`` (see the module docstring).
        schema_version: Row schema version; see
            :data:`TRAJECTORY_SCHEMA_VERSION`.
    """

    index: int
    prev_step_hash: str
    step_hash: str
    input_hash: str
    observation: str | None
    action: Any
    outcome: Any
    model: str | None
    effort: str | None = None
    schema_version: int = TRAJECTORY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly wire shape of the row."""
        row: dict[str, Any] = {
            "schema_version": self.schema_version,
            "index": self.index,
            "prev_step_hash": self.prev_step_hash,
            "step_hash": self.step_hash,
            "input_hash": self.input_hash,
            "observation": self.observation,
            "action": self.action,
            "outcome": self.outcome,
            "model": self.model,
        }
        # Sentinel-by-omission, mirroring ``canonical_step_payload``: only a
        # recorded effort is serialised, so a legacy row keeps its old shape.
        if self.effort is not None:
            row["effort"] = self.effort
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrajectoryStep:
        """Build a row from its wire shape.

        A document with no ``effort`` key reads back as ``effort=None``, the
        sentinel for an unrecorded effort.
        """
        return cls(
            index=int(raw["index"]),
            prev_step_hash=str(raw["prev_step_hash"]),
            step_hash=str(raw["step_hash"]),
            input_hash=str(raw["input_hash"]),
            observation=raw.get("observation"),
            action=raw.get("action"),
            outcome=raw.get("outcome"),
            model=raw.get("model"),
            effort=raw.get("effort"),
            schema_version=int(raw.get("schema_version", TRAJECTORY_SCHEMA_VERSION)),
        )

    def canonical_bytes(self) -> bytes:
        """Return the canonical UTF-8 bytes of this row.

        Sorted keys, compact separators, no wall-clock - so two exports of the
        same recorded run produce byte-identical rows.
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def trajectory_step_from_entry(entry: JournalEntry) -> TrajectoryStep:
    """Project one journal entry onto a trajectory row.

    ``step_hash`` and ``prev_step_hash`` are copied from the entry; this
    function never computes a hash. ``ts``, ``blob_refs`` and the journal's
    own ``schema_version`` stay behind: none of them is part of the step
    hash, and the row's version tracks the trajectory contract, not the
    journal's storage format.
    """
    return TrajectoryStep(
        index=entry.seq,
        prev_step_hash=entry.prev_hash,
        step_hash=entry.step_hash,
        input_hash=entry.input_hash,
        observation=entry.prompt,
        action=entry.tool_call,
        outcome=entry.tool_result,
        model=entry.model,
        effort=entry.effort,
    )


def project_trajectory(reader: JournalReader) -> list[TrajectoryStep]:
    """Return every step of the journal behind *reader* as trajectory rows.

    Rows come back in ``seq`` order. A journal that was never written yields
    an empty list - a run that never stepped has an empty trajectory, which is
    a fact about the run rather than an error.
    """
    return [trajectory_step_from_entry(entry) for entry in reader.entries()]
