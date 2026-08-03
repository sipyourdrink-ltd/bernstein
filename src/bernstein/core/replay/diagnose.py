"""Single-run first-fault locator over the signed per-step journal (#2928).

The per-run event journal (:mod:`bernstein.core.replay.journal`) already
records every step of a run as a Merkle-chained, hash-addressed row.
``verify_journal`` proves that chain is intact and ``diff_event_logs``
(:mod:`bernstein.core.replay.diff`) localises where two runs diverge -- but
neither can point at the first faulty step of a *single* run that recorded
cleanly and still went wrong. :func:`diagnose_run` closes that gap: it
evaluates a pure failure predicate over growing prefixes of the journal and
names the minimal step index at which the predicate first holds, together
with that step's exact ``event_hash``.

The finding follows the reporting shape transparency-log verifiers use
(RFC 6962 style): a 0-based entry index plus the entry's hash, bound to a
head recomputed over the whole log, so any independent holder of the journal
re-derives the identical finding offline. No wall-clock value enters the
result, and the predicate is a pure function of on-disk records, so two
invocations over the same journal are byte-identical.

Fail-closed contract
--------------------
The diagnosis is a projection of the signed record, never an inference
beside it:

* a missing or empty journal refuses with :class:`DiagnoseError`;
* a journal containing any non-blank line that does not parse as a JSON
  object refuses -- unlike ordinary journal readers, which tolerate a torn
  trailing write, the diagnostic reader never operates on a filtered
  sequence, so every reported index counts physical journal lines;
* a content predicate is only evaluated over a journal whose chain
  recomputes cleanly -- a chain-broken journal refuses and points the
  operator at ``--signal replay``, which reports the break itself;
* a signal whose fingerprint never appears in the journal refuses with
  :class:`SignalNotLocatedError` rather than guessing a step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.diff import (
    REASON_CODE_CHAIN_BREAK,
    REASON_CODE_NONE,
    REASON_CODE_PROVIDER_STATE_MUTATION,
)
from bernstein.core.replay.journal import (
    _NON_DETERMINISTIC_FIELDS,  # pyright: ignore[reportPrivateUsage] - shared journal projection
    JournalParseError,
    load_events,
    rebuild_state,
    verify_journal,
)
from bernstein.core.replay.provider_state import PROVIDER_STATE_MUTATION_EVENT

if TYPE_CHECKING:
    from pathlib import Path

#: Predicate evaluation modes. ``content`` scans step payloads for the
#: signal's fingerprint over an intact chain; ``chain`` reports the first
#: chain-integrity break located by ``verify_journal``.
SIGNAL_MODE_CONTENT = "content"
SIGNAL_MODE_CHAIN = "chain"


class DiagnoseError(RuntimeError):
    """Raised when a diagnosis cannot be derived from the signed record.

    Fail-closed by design: callers must emit no receipt on this error.
    """


class SignalNotLocatedError(DiagnoseError):
    """Raised when the failure signal resolves to no recorded step.

    The signal itself exists (a gate rejected, an artefact is tainted) but
    the journal never recorded the offending content, so there is no step
    to name. Refusing is the honest outcome -- a guess would not be a
    projection of the signed chain.
    """


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON text (sorted keys, compact separators)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class SignalPredicate:
    """A failure signal resolved to a pure predicate over journal rows.

    Built by :mod:`bernstein.core.replay.diagnose_signals` from on-disk
    records only. The ``params`` dict is the predicate's full parameterisation
    and is embedded verbatim in the diagnosis receipt, so an offline verifier
    reconstructs exactly the same predicate without re-reading the gate /
    lineage / incident stores.

    Attributes:
        predicate_id: Versioned identifier of the predicate semantics
            (e.g. ``"gate/v1"``).
        params: JSON-safe parameterisation (kind, needles, anchors).
        default_reason_code: Attribution class applied when the culprit row
            is not a provider-state mutation entry.
        needles: Content fingerprints; a row matches when any needle occurs
            in its canonical timing-excluded payload text.
        lineage_path: Optional content-addressed evidence chain
            (culprit entry -> failing artefact tip), attached unchanged to
            the result.
        mode: :data:`SIGNAL_MODE_CONTENT` or :data:`SIGNAL_MODE_CHAIN`.
    """

    predicate_id: str
    params: dict[str, Any]
    default_reason_code: str
    needles: tuple[str, ...] = ()
    lineage_path: tuple[str, ...] = ()
    mode: str = SIGNAL_MODE_CONTENT

    def predicate_hash(self) -> str:
        """Content hash of the predicate identity and parameterisation.

        A verifier recomputes this from the receipt's embedded ``signal``
        block, so "what *failed* meant" is pinned by hash rather than prose.
        """
        payload = _canonical_json({"predicate_id": self.predicate_id, "params": self.params})
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DiagnosisResult:
    """Outcome of :func:`diagnose_run`, mirroring ``DivergenceResult``.

    Attributes:
        run_id: The diagnosed run.
        journal_head: Head hash recomputed over every on-disk row's payload
            (pure function of the file; equals the stored Merkle head for an
            intact chain).
        journal_file_sha256: SHA-256 of the raw journal bytes, binding the
            finding to the exact file it was derived from.
        event_count: Number of rows walked.
        located: ``True`` when a culprit step was named.
        culprit_index: 0-based index of the first faulty step, or ``None``
            for a clean chain-mode diagnosis.
        culprit_step_hash: The stored ``event_hash`` at
            :attr:`culprit_index` (empty when not located).
        reason_code: Machine-readable attribution from the shared
            ``REASON_CODE_*`` vocabulary in
            :mod:`bernstein.core.replay.diff`.
        reason: Human-readable explanation.
        predicate_id: The evaluated predicate's identifier.
        predicate_hash: The evaluated predicate's content hash.
        signal_params: The predicate's full parameterisation (JSON-safe).
        lineage_path: Content-addressed evidence chain when the signal
            carried one.
    """

    run_id: str
    journal_head: str
    journal_file_sha256: str
    event_count: int
    located: bool
    culprit_index: int | None
    culprit_step_hash: str
    reason_code: str
    reason: str
    predicate_id: str
    predicate_hash: str
    signal_params: dict[str, Any] = field(default_factory=dict[str, Any])
    lineage_path: tuple[str, ...] = ()


def _load_events_strict(journal_path: Path, *, run_id: str) -> list[dict[str, Any]]:
    """Parse every non-blank journal line, refusing any that fails to decode.

    An ordinary reader survives a torn trailing write; a *diagnostic* reader
    must not: a silently dropped physical line would let the surviving prefix
    chain-verify cleanly and a signed receipt would then cover a filtered
    sequence, and every reported index would count parsed rows rather than
    physical journal lines (bot-ack: 3705961185). The scan itself is
    single-sourced in :func:`journal.load_events` via its ``strict`` policy,
    so the diagnostic reader can never drift from the rest of replay
    (bot-ack: 3706042994).

    Raises:
        DiagnoseError: A non-blank line is not a JSON object; the message
            names the 0-based physical line index.
    """
    try:
        return load_events(journal_path, strict=True)
    except JournalParseError as exc:
        raise DiagnoseError(
            f"journal for run {run_id}: {exc}; refusing to diagnose a filtered sequence -- "
            "capture the corrupted journal forensically before re-running"
        ) from exc


def _row_payload_text(row: dict[str, Any]) -> str:
    """Canonical timing-excluded payload text of one journal row.

    Uses the same field projection the journal's payload hash uses
    (:data:`~bernstein.core.replay.journal._NON_DETERMINISTIC_FIELDS`
    excluded), so needle matching sees exactly the bytes the chain covers --
    never the wall-clock envelope or derived chain fields.
    """
    projected = {k: v for k, v in row.items() if k not in _NON_DETERMINISTIC_FIELDS}
    return _canonical_json(projected)


def _reason_code_for_row(row: dict[str, Any], default_code: str) -> str:
    """Attribute a culprit row, naming provider-state mutations explicitly.

    Mirrors the attribution rule of ``diff_event_logs``: a provider-side
    context mutation entry is classified as
    :data:`REASON_CODE_PROVIDER_STATE_MUTATION` regardless of the signal's
    default, so the two surfaces agree on the same step.
    """
    if str(row.get("event", "")) == PROVIDER_STATE_MUTATION_EVENT:
        return REASON_CODE_PROVIDER_STATE_MUTATION
    return default_code


def diagnose_run(journal_path: Path, predicate: SignalPredicate, *, run_id: str) -> DiagnosisResult:
    """Name the first step of *run_id*'s journal at which *predicate* holds.

    A linear first-index scan over the prefix is used rather than a binary
    bisection: it is equally deterministic, costs one pass over a file that
    is already being read in full, and stays correct for any predicate --
    bisection is only sound for monotone ones.

    Args:
        journal_path: Path to the run's ``journal.jsonl`` (derive it via
            :func:`bernstein.core.replay.journal.run_journal_path`).
        predicate: The resolved failure signal.
        run_id: Run identifier recorded in the result.

    Returns:
        A :class:`DiagnosisResult`. ``located`` is ``False`` only for a
        chain-mode predicate over an intact chain (nothing to report).

    Raises:
        DiagnoseError: The journal is missing or empty, or a content
            predicate was requested over a chain that does not recompute.
        SignalNotLocatedError: The signal's fingerprint appears in no
            recorded step.
    """
    if not journal_path.exists():
        raise DiagnoseError(f"no signed per-step record for run {run_id}: {journal_path} does not exist")
    # Strict load first: an unparsable line refuses the diagnosis before any
    # head, count, chain, or culprit computation can observe a filtered view.
    events = _load_events_strict(journal_path, run_id=run_id)
    if not events:
        raise DiagnoseError(f"no signed per-step record for run {run_id}: journal at {journal_path} is empty")

    journal_file_sha256 = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    journal_head = str(rebuild_state(journal_path, from_step=len(events))["head_hash"])
    chain = verify_journal(journal_path)

    if predicate.mode == SIGNAL_MODE_CHAIN:
        if chain.ok:
            return DiagnosisResult(
                run_id=run_id,
                journal_head=journal_head,
                journal_file_sha256=journal_file_sha256,
                event_count=len(events),
                located=False,
                culprit_index=None,
                culprit_step_hash="",
                reason_code=REASON_CODE_NONE,
                reason=f"chain intact: {len(events)} step(s) recompute cleanly",
                predicate_id=predicate.predicate_id,
                predicate_hash=predicate.predicate_hash(),
                signal_params=predicate.params,
                lineage_path=predicate.lineage_path,
            )
        culprit = chain.divergent_index if chain.divergent_index is not None else 0
        row = events[culprit]
        detail = chain.errors[0] if chain.errors else "chain verification failed"
        return DiagnosisResult(
            run_id=run_id,
            journal_head=journal_head,
            journal_file_sha256=journal_file_sha256,
            event_count=len(events),
            located=True,
            culprit_index=culprit,
            culprit_step_hash=str(row.get("event_hash", "")),
            reason_code=REASON_CODE_CHAIN_BREAK,
            reason=f"step {culprit}: {detail}",
            predicate_id=predicate.predicate_id,
            predicate_hash=predicate.predicate_hash(),
            signal_params=predicate.params,
            lineage_path=predicate.lineage_path,
        )

    # Content mode: the predicate is only meaningful over a verified chain.
    if not chain.ok:
        detail = chain.errors[0] if chain.errors else "chain verification failed"
        raise DiagnoseError(
            f"journal for run {run_id} fails chain verification ({detail}); "
            "refusing to evaluate a content signal over an unverified record -- "
            "run `bernstein audit diagnose --signal replay` to localise the break"
        )
    if not predicate.needles:
        raise DiagnoseError(f"signal {predicate.predicate_id} resolved to no content fingerprint; nothing to locate")

    for i, row in enumerate(events):
        text = _row_payload_text(row)
        if any(needle in text for needle in predicate.needles):
            event_type = str(row.get("event", ""))
            return DiagnosisResult(
                run_id=run_id,
                journal_head=journal_head,
                journal_file_sha256=journal_file_sha256,
                event_count=len(events),
                located=True,
                culprit_index=i,
                culprit_step_hash=str(row.get("event_hash", "")),
                reason_code=_reason_code_for_row(row, predicate.default_reason_code),
                reason=(
                    f"step {i} ({event_type}): first step whose recorded payload "
                    f"carries the offending content (signal {predicate.predicate_id})"
                ),
                predicate_id=predicate.predicate_id,
                predicate_hash=predicate.predicate_hash(),
                signal_params=predicate.params,
                lineage_path=predicate.lineage_path,
            )

    raise SignalNotLocatedError(
        f"signal {predicate.predicate_id} does not resolve to any recorded step of "
        f"run {run_id} (0 of {len(events)} steps carry the fingerprint); "
        "refusing to guess -- the diagnosis must be a projection of the signed record"
    )


__all__ = [
    "SIGNAL_MODE_CHAIN",
    "SIGNAL_MODE_CONTENT",
    "DiagnoseError",
    "DiagnosisResult",
    "SignalNotLocatedError",
    "SignalPredicate",
    "diagnose_run",
]
