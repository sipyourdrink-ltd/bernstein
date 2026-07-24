"""Deterministic replay-debugging projection (#2605).

This module is the *forensic* half of replay debugging. It freezes the
recorded Merkle step chain and proves where a single run was tampered or
where two runs diverged; it never re-executes anything. (The exploratory
what-if surface - re-run and see what changes - is a distinct concern; see
``docs/operations/replay.md``.)

Two projections, both built on the single chain primitive
(``journal.compute_step_hash``) and the single field-attribution primitive
(``journal_diff.diff_steps``) so there is no second hashing or diffing
scheme to drift against :meth:`JournalReader.verify`:

* :func:`walk_and_verify` streams a :class:`HashMismatch` for every step
  whose recomputed ``step_hash`` or chain linkage disagrees with the stored
  value. It reads the journal one entry at a time from
  :class:`JournalReader` and never materialises the whole chain, so the
  first divergent step in a large journal is localised without loading it
  all - take ``next(walk_and_verify(reader), None)`` to stop at the first.

* :func:`two_run_path_diff` builds an ordered, content-addressed
  side-by-side projection of two chains up to and including the first
  divergence found by :func:`journal_diff.diff_journals`. The artifact
  carries its own SHA-256 over the sorted-key JSON body (chain content
  only, never a filesystem path), so two operators on the same journals
  produce the byte-identical diff artifact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.journal import (
    GENESIS_HASH,
    JournalReader,
    compute_step_hash,
)
from bernstein.core.persistence.journal_diff import (
    StepDivergence,
    diff_journals,
    diff_steps,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Canonical six fields projected side-by-side in a two-run path diff. These
#: are the human-legible payload fields; ``effort`` folds into the hash but is
#: surfaced via ``fields_changed`` when it is what diverged.
_PROJECTED_FIELDS = (
    "prev_hash",
    "input_hash",
    "model",
    "prompt",
    "tool_call",
    "tool_result",
)


# ---------------------------------------------------------------------------
# Single-run recompute-mismatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HashMismatch:
    """A step whose stored digest or chain linkage does not verify.

    Attributes:
        seq: Zero-based index of the divergent step.
        expected_hash: The hash the chain expects at this position - the
            value recomputed from the row's canonical fields for a digest
            mismatch, or the previous step's stored ``step_hash`` for a
            ``prev_hash`` break.
        actual_hash: The value stored on disk (the tampered/inconsistent
            digest, or the row's stored ``prev_hash`` for a linkage break).
        first_divergent_field: Name of the first canonical field that
            diverged, attributed via :func:`journal_diff.diff_steps`.
            ``"prev_hash"`` for a chain-linkage break; ``"step_hash"`` when
            the row's fields no longer hash to its stored digest.
    """

    seq: int
    expected_hash: str
    actual_hash: str
    first_divergent_field: str


def _recompute_step_hash(row: dict[str, Any]) -> str:
    """Recompute a row's ``step_hash`` from its canonical fields."""
    return compute_step_hash(
        prev_hash=str(row.get("prev_hash", "")),
        input_hash=str(row.get("input_hash", "")),
        model=row.get("model"),
        prompt=row.get("prompt"),
        tool_call=row.get("tool_call"),
        tool_result=row.get("tool_result"),
        # Legacy rows lack ``effort`` (get -> None), matching the pre-effort
        # payload so old chains recompute unchanged.
        effort=row.get("effort"),
    )


def _first_divergent_field(row: dict[str, Any], expected_prev_hash: str) -> str:
    """Attribute the first divergent field of a mismatched step.

    Compares the recorded row against the row the chain *expected* at this
    position - identical fields except ``prev_hash``, which is pinned to the
    walk's expected value. :func:`diff_steps` names ``prev_hash`` when the
    linkage broke; when it finds no canonical-field difference the mismatch
    is a bare digest tamper, so the divergent field is the ``step_hash``
    itself.
    """
    reference = dict(row)
    reference["prev_hash"] = expected_prev_hash
    divergence = diff_steps(reference, row)
    if divergence is not None:
        return divergence.fields_changed[0]
    return "step_hash"


def walk_and_verify(reader: JournalReader) -> Iterator[HashMismatch]:
    """Yield a :class:`HashMismatch` for every non-verifying step, in order.

    Streams entries from *reader* one at a time and recomputes each
    ``step_hash`` from the canonical six-field payload, comparing to the
    stored value and checking ``prev_hash`` linkage against the previous
    step's stored digest. A ``prev_hash`` break is reported in preference to
    a co-located digest mismatch. Yields nothing for an intact chain.

    The generator never materialises the whole journal, so a caller that
    only needs the first divergent step (``next(walk_and_verify(reader),
    None)``) stops reading as soon as it is found.
    """
    prev_hash = GENESIS_HASH
    for entry in reader.entries():
        row = entry.to_dict()
        stored_prev = str(row.get("prev_hash", ""))
        stored_hash = str(row.get("step_hash", ""))

        if stored_prev != prev_hash:
            # Chain linkage broke: the row does not chain onto the previous
            # step's stored digest. Attribute the field via diff_steps.
            yield HashMismatch(
                seq=entry.seq,
                expected_hash=prev_hash,
                actual_hash=stored_prev,
                first_divergent_field=_first_divergent_field(row, prev_hash),
            )
        else:
            recomputed = _recompute_step_hash(row)
            if recomputed != stored_hash:
                yield HashMismatch(
                    seq=entry.seq,
                    expected_hash=recomputed,
                    actual_hash=stored_hash,
                    first_divergent_field=_first_divergent_field(row, prev_hash),
                )

        # Continue from the on-disk digest (mirrors JournalReader.verify) so a
        # tampered digest surfaces as a downstream prev_hash break rather than
        # silently re-anchoring the walk.
        prev_hash = stored_hash


# ---------------------------------------------------------------------------
# Two-run time-travel path diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathDiff:
    """Ordered, content-addressed side-by-side of two chains to divergence.

    Attributes:
        diverged: ``True`` when the two chains disagree.
        divergence: The first :class:`StepDivergence` from
            :func:`diff_journals`, or ``None`` when the chains match.
        steps: Per-seq side-by-side of the canonical six fields, ordered
            from seq 0 up to and including the divergence (or the full
            paired length when the chains match). Each entry is
            ``{"seq", "left", "right", "diverged", "fields_changed"}`` where
            ``left`` / ``right`` are ``None`` when that chain has no step at
            the seq (length mismatch).
        diff_hash: SHA-256 hex over the sorted-key JSON of the diff *body*
            (``diverged`` + ``divergence`` + ``steps``) - chain content only,
            never a filesystem path - so the artifact is byte-identical for
            any two operators holding the same journals.
    """

    diverged: bool
    divergence: StepDivergence | None
    steps: list[dict[str, Any]] = field(default_factory=list)
    diff_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly artifact (``diff_hash`` + body)."""
        return {
            "diff_hash": self.diff_hash,
            "diverged": self.diverged,
            "divergence": _divergence_to_dict(self.divergence),
            "steps": self.steps,
        }


def _divergence_to_dict(divergence: StepDivergence | None) -> dict[str, Any] | None:
    """Serialise a :class:`StepDivergence` deterministically (or ``None``)."""
    if divergence is None:
        return None
    return {
        "seq": divergence.seq,
        "fields_changed": sorted(divergence.fields_changed),
        "left_values": divergence.left_values,
        "right_values": divergence.right_values,
        "reason": divergence.reason,
    }


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """Project a journal row onto the canonical six fields."""
    return {name: row.get(name) for name in _PROJECTED_FIELDS}


def _collect_window(reader: JournalReader, upto_seq: int | None) -> dict[int, dict[str, Any]]:
    """Stream entries ``[0..upto_seq]`` (or all) as ``{seq: projected_row}``.

    Reads one entry at a time and stops past ``upto_seq`` so a long journal
    is not fully materialised when the divergence is early.
    """
    window: dict[int, dict[str, Any]] = {}
    for entry in reader.entries():
        if upto_seq is not None and entry.seq > upto_seq:
            break
        window[entry.seq] = _project(entry.to_dict())
    return window


def two_run_path_diff(left_dir: Path, right_dir: Path) -> PathDiff:
    """Build the content-addressed path diff between two agent journals.

    Reuses :func:`diff_journals` for the first-divergence locator, then emits
    the ordered side-by-side projection up to and including that divergence.
    When the chains match, the projection spans the full paired length.
    """
    divergence = diff_journals(left_dir, right_dir)
    upto = divergence.seq if divergence is not None else None

    left_window = _collect_window(JournalReader(left_dir), upto)
    right_window = _collect_window(JournalReader(right_dir), upto)

    max_seq = -1
    for seq in (*left_window.keys(), *right_window.keys()):
        max_seq = max(max_seq, seq)

    steps: list[dict[str, Any]] = []
    for seq in range(max_seq + 1):
        diverged_here = divergence is not None and seq == divergence.seq
        fields_changed = sorted(divergence.fields_changed) if divergence is not None and diverged_here else []
        steps.append(
            {
                "seq": seq,
                "left": left_window.get(seq),
                "right": right_window.get(seq),
                "diverged": diverged_here,
                "fields_changed": fields_changed,
            }
        )

    body = {
        "diverged": divergence is not None,
        "divergence": _divergence_to_dict(divergence),
        "steps": steps,
    }
    diff_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    return PathDiff(
        diverged=divergence is not None,
        divergence=divergence,
        steps=steps,
        diff_hash=diff_hash,
    )


__all__ = [
    "HashMismatch",
    "PathDiff",
    "two_run_path_diff",
    "walk_and_verify",
]
