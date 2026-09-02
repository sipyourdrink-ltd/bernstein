"""One durable record per adapter invocation, joinable from the cost side.

``bernstein.core.cost.model_prices`` prices a call's tokens and nothing
else: :func:`~bernstein.core.cost.model_prices.price_model_usage` never
sees the capability that made the call, the adapter it went to, the
resolved parameters, or the payload that went in and came back. Task
records and dead-letter entries each replay their own kind of row, and
neither is a model call. So "we spent $40 yesterday" has never been
answerable as "we spent $40 on these 30 calls, here is what each one sent
and got back."

This module holds the missing row. :class:`ModelCallRecord` carries the
whole call - capability id, adapter id, model identifier with version,
resolved parameters and the schema version they were validated against,
input, output, status, the three instants, and the journal entry id that
ties the call back to the work ledger. :class:`ModelCallLedger` appends
those records to ``.sdd/runtime/model_calls.jsonl`` and owns the three
operations over them: the guarded status graph, the opt-in short circuit
for an identical call, and operator-invoked replay.

Where the record lives. It is its own append-only JSONL store rather than
a new entry kind on the hash-chained work ledger
(``core/persistence/work_ledger.py``). The record already carries a
``journal_entry_id`` field, so the join to the chain exists without
putting a per-call, per-token-payload row into a per-task chain whose
growth rate is orders of magnitude lower; and reuse lookup is a hash
index over this file rather than a walk of the task-graph chain.

Usage::

    ledger = ModelCallLedger(sdd_dir=Path(".sdd"))
    record = ledger.invoke(
        capability_id="review.summarise",
        adapter_id="claude",
        model="claude-opus-4",
        parameters={"temperature": 0.0},
        input_text=prompt,
        call=lambda: adapter.complete(prompt),
    )
    priced = price_model_usage(record.model, n_in, n_out, model_call_id=record.id)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

MODEL_CALL_SCHEMA_VERSION = 1
"""Schema version stamped on every row this module writes."""

ModelCallStatus = Literal["pending", "running", "succeeded", "failed"]

_ALLOWED_TRANSITIONS: dict[ModelCallStatus, frozenset[ModelCallStatus]] = {
    "pending": frozenset({"running"}),
    "running": frozenset({"succeeded", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
}
"""The whole status graph, in one place.

``succeeded`` and ``failed`` have no outgoing edges: a finished call is a
fact, not a snapshot a later message may overwrite. Every transition goes
through :meth:`ModelCallLedger.transition`, which consults this mapping;
nothing else is allowed to set a status.
"""

_ROW_KIND_RECORD = "model_call"
_ROW_KIND_REFUSAL = "transition_refused"


class InvalidStatusTransitionError(RuntimeError):
    """A status transition outside :data:`_ALLOWED_TRANSITIONS` was attempted.

    Attributes:
        record_id: The record whose status the caller tried to change.
        from_status: The status the record actually holds.
        to_status: The status the caller asked for.
    """

    def __init__(
        self,
        record_id: str,
        from_status: ModelCallStatus,
        to_status: ModelCallStatus,
    ) -> None:
        super().__init__(f"model call {record_id}: refused transition {from_status} -> {to_status}")
        self.record_id = record_id
        self.from_status = from_status
        self.to_status = to_status


def _as_object(value: Any) -> dict[str, Any]:
    """Return *value* as a str-keyed dict, or ``{}`` when it is not a mapping.

    Rows reach the reader from a file other processes append to, so a row
    that is not a JSON object is skipped rather than raising: one bad line
    must not cost an operator the rest of the ledger.
    """
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast("dict[Any, Any]", value).items()}


def _canonical(value: Any) -> Any:
    """Return a JSON-encodable projection of *value*.

    Parameters and payloads reach the record from adapter call sites, so
    they may hold objects ``json`` cannot encode. Anything outside the
    JSON scalar/container set is projected to its ``repr`` rather than
    raising: a record that cannot be hashed is worse than one whose exotic
    parameter is recorded as text.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in cast("dict[Any, Any]", value).items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in cast("list[Any] | tuple[Any, ...]", value)]
    return repr(value)


@dataclass(frozen=True)
class ModelCallRecord:
    """One adapter invocation, in full.

    Attributes:
        id: Unique record id; the join key the cost row references.
        capability_id: The capability that made the call.
        adapter_id: The adapter the call went to.
        model: Model identifier as the adapter names it.
        model_version: Provider version string for ``model``, or ``""``.
        parameters: Resolved parameters as sent to the adapter.
        parameter_schema_version: Version of the adapter's declared
            parameter schema the parameters were validated against.
        input_text: What was sent.
        output_text: What came back; ``""`` until the call finishes.
        status: One of :data:`ModelCallStatus`; see
            :data:`_ALLOWED_TRANSITIONS` for the legal graph.
        error: Failure message when ``status`` is ``"failed"``.
        created_at: Unix instant the record was created.
        started_at: Unix instant of ``pending`` -> ``running``, or ``0.0``.
        finished_at: Unix instant of the terminal transition, or ``0.0``.
        journal_entry_id: Work-ledger entry this call belongs to, if any.
        reused_from: Id of the record whose stored output served this call
            under ``--reuse-identical``; ``""`` when the adapter ran.
        replay_of: Id of the record this one replays; ``""`` otherwise.
        schema_version: :data:`MODEL_CALL_SCHEMA_VERSION` at write time.
    """

    id: str
    capability_id: str
    adapter_id: str
    model: str
    model_version: str = ""
    parameters: dict[str, Any] = field(default_factory=dict[str, Any])
    parameter_schema_version: str = ""
    input_text: str = ""
    output_text: str = ""
    status: ModelCallStatus = "pending"
    error: str = ""
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    journal_entry_id: str = ""
    reused_from: str = ""
    replay_of: str = ""
    schema_version: int = MODEL_CALL_SCHEMA_VERSION

    @classmethod
    def new(
        cls,
        *,
        capability_id: str,
        adapter_id: str,
        model: str,
        model_version: str = "",
        parameters: Mapping[str, Any] | None = None,
        parameter_schema_version: str = "",
        input_text: str = "",
        journal_entry_id: str = "",
        replay_of: str = "",
    ) -> ModelCallRecord:
        """Build a ``pending`` record with a fresh id and ``created_at``.

        Args:
            capability_id: The capability making the call.
            adapter_id: The adapter the call goes to.
            model: Model identifier as the adapter names it.
            model_version: Provider version string for ``model``.
            parameters: Resolved parameters for the call.
            parameter_schema_version: Version of the adapter's declared
                parameter schema these parameters satisfy.
            input_text: What is being sent.
            journal_entry_id: Work-ledger entry this call belongs to.
            replay_of: Id of the record this one replays.

        Returns:
            A new ``pending`` record.
        """
        return cls(
            id=uuid.uuid4().hex[:16],
            capability_id=capability_id,
            adapter_id=adapter_id,
            model=model,
            model_version=model_version,
            parameters=dict(_canonical(dict(parameters or {}))),
            parameter_schema_version=parameter_schema_version,
            input_text=input_text,
            journal_entry_id=journal_entry_id,
            replay_of=replay_of,
            created_at=time.time(),
        )

    @property
    def model_identifier(self) -> str:
        """Model and version as one string, e.g. ``"claude-opus-4@20260501"``."""
        return f"{self.model}@{self.model_version}" if self.model_version else self.model

    @property
    def reused(self) -> bool:
        """Whether a stored record's output served this call."""
        return bool(self.reused_from)

    def content_hash(self) -> str:
        """Return the SHA-256 digest over this call's identity.

        The digest covers exactly the four fields that decide whether two
        calls are the same call - capability id, model identifier,
        resolved parameters, input - and nothing that varies between two
        identical calls (id, instants, output, status). Keys are sorted at
        every depth, so parameter dicts built in different insertion
        orders hash the same.

        Returns:
            Hex digest usable as the ``--reuse-identical`` lookup key.
        """
        document = {
            "capability_id": self.capability_id,
            "input": self.input_text,
            "model": self.model_identifier,
            "parameters": _canonical(self.parameters),
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of the record."""
        return {
            "id": self.id,
            "capability_id": self.capability_id,
            "adapter_id": self.adapter_id,
            "model": self.model,
            "model_version": self.model_version,
            "parameters": self.parameters,
            "parameter_schema_version": self.parameter_schema_version,
            "input_text": self.input_text,
            "output_text": self.output_text,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "journal_entry_id": self.journal_entry_id,
            "reused_from": self.reused_from,
            "replay_of": self.replay_of,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelCallRecord:
        """Rebuild a record from a deserialised row.

        Args:
            data: One decoded JSONL row.

        Returns:
            The record. An unrecognised ``status`` degrades to
            ``"failed"`` rather than widening the status set.
        """
        status = str(data.get("status", "pending"))
        if status not in get_args(ModelCallStatus):
            logger.warning("model call ledger: unknown status %r; reading as failed", status)
            status = "failed"
        params: Any = data.get("parameters")
        return cls(
            id=str(data.get("id", "")),
            capability_id=str(data.get("capability_id", "")),
            adapter_id=str(data.get("adapter_id", "")),
            model=str(data.get("model", "")),
            model_version=str(data.get("model_version", "")),
            parameters=_as_object(params),
            parameter_schema_version=str(data.get("parameter_schema_version", "")),
            input_text=str(data.get("input_text", "")),
            output_text=str(data.get("output_text", "")),
            status=status,  # type: ignore[arg-type]
            error=str(data.get("error", "")),
            created_at=float(data.get("created_at", 0.0)),
            started_at=float(data.get("started_at", 0.0)),
            finished_at=float(data.get("finished_at", 0.0)),
            journal_entry_id=str(data.get("journal_entry_id", "")),
            reused_from=str(data.get("reused_from", "")),
            replay_of=str(data.get("replay_of", "")),
            schema_version=int(data.get("schema_version", MODEL_CALL_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class TransitionRefusal:
    """A refused status transition, journaled next to the records.

    Attributes:
        record_id: Record whose status the caller tried to change.
        from_status: Status the record actually held.
        to_status: Status the caller asked for.
        at: Unix instant of the refusal.
    """

    record_id: str
    from_status: str
    to_status: str
    at: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of the refusal."""
        return {
            "record_id": self.record_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "at": self.at,
        }


class ModelCallLedger:
    """Append-only store of :class:`ModelCallRecord` rows.

    Backed by ``<sdd_dir>/runtime/model_calls.jsonl``. Rows are never
    rewritten: a replay appends a new record pointing at the original with
    ``replay_of`` instead of editing the original in place, so an operator
    reading a record sees what that invocation actually sent and received,
    forever.

    Args:
        sdd_dir: Path to the ``.sdd`` state directory.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._path = sdd_dir / "runtime" / "model_calls.jsonl"
        self._records: list[ModelCallRecord] = []
        self._refusals: list[TransitionRefusal] = []
        self._loaded = False

    @property
    def path(self) -> Path:
        """Path of the JSONL file backing this ledger."""
        return self._path

    # -- reading ---------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load rows from disk once."""
        if self._loaded:
            return
        self._records = []
        self._refusals = []
        if self._path.exists():
            try:
                lines = self._path.read_text().splitlines()
            except OSError as exc:
                logger.warning("model call ledger: failed to read %s: %s", self._path, exc)
                lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed: Any = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    logger.debug("model call ledger: skipping malformed row: %s", exc)
                    continue
                if not isinstance(parsed, dict):
                    continue
                row = _as_object(parsed)
                kind = row.get("kind")
                if kind == _ROW_KIND_RECORD:
                    self._records.append(ModelCallRecord.from_dict(_as_object(row.get("record"))))
                elif kind == _ROW_KIND_REFUSAL:
                    self._refusals.append(
                        TransitionRefusal(
                            record_id=str(row.get("record_id", "")),
                            from_status=str(row.get("from_status", "")),
                            to_status=str(row.get("to_status", "")),
                            at=float(row.get("at", 0.0)),
                        )
                    )
        self._loaded = True

    def list_records(self, *, limit: int = 100) -> list[ModelCallRecord]:
        """Return records in write order, oldest first.

        Args:
            limit: Maximum records to return.

        Returns:
            Up to *limit* records.
        """
        self._ensure_loaded()
        return self._records[:limit]

    def get_record(self, record_id: str) -> ModelCallRecord | None:
        """Return the record with *record_id*, or ``None``.

        Args:
            record_id: Record id, e.g. a cost row's ``model_call_id``.

        Returns:
            The matching record, or ``None`` when unknown.
        """
        self._ensure_loaded()
        for record in self._records:
            if record.id == record_id:
                return record
        return None

    def refusals(self) -> list[TransitionRefusal]:
        """Return every journaled transition refusal, oldest first."""
        self._ensure_loaded()
        return list(self._refusals)

    def replays_of(self, record_id: str) -> list[str]:
        """Return the ids of records that replay *record_id*.

        Args:
            record_id: The original record's id.

        Returns:
            Replay record ids in write order.
        """
        self._ensure_loaded()
        return [r.id for r in self._records if r.replay_of == record_id]

    def find_reusable(self, content_hash: str) -> ModelCallRecord | None:
        """Return the newest succeeded record with *content_hash*.

        Args:
            content_hash: Digest from :meth:`ModelCallRecord.content_hash`.

        Returns:
            The record whose stored output may serve an identical call, or
            ``None`` when no succeeded record matches.
        """
        self._ensure_loaded()
        for record in reversed(self._records):
            if record.status == "succeeded" and record.content_hash() == content_hash:
                return record
        return None

    # -- transitions -----------------------------------------------------

    def transition(self, record: ModelCallRecord, to_status: ModelCallStatus) -> ModelCallRecord:
        """Return *record* moved to *to_status*, or refuse the move.

        The only way a record's status changes. ``pending`` -> ``running``
        stamps ``started_at``; ``running`` -> ``succeeded``/``failed``
        stamps ``finished_at``. Any other pair - including any edge out of
        a terminal status - is journaled to the ledger file and raised, so
        a refusal survives the process that attempted it.

        Args:
            record: The record to move.
            to_status: The status asked for.

        Returns:
            A new record at *to_status*; *record* itself is unchanged.

        Raises:
            InvalidStatusTransitionError: *to_status* is not reachable
                from ``record.status``.
        """
        if to_status not in _ALLOWED_TRANSITIONS[record.status]:
            refusal = TransitionRefusal(
                record_id=record.id,
                from_status=record.status,
                to_status=to_status,
                at=time.time(),
            )
            self._ensure_loaded()
            self._refusals.append(refusal)
            self._append_row({"kind": _ROW_KIND_REFUSAL, **refusal.to_dict()})
            logger.warning(
                "model call ledger: refused transition %s -> %s on record %s",
                record.status,
                to_status,
                record.id,
            )
            raise InvalidStatusTransitionError(record.id, record.status, to_status)

        now = time.time()
        if to_status == "running":
            return replace(record, status=to_status, started_at=now)
        return replace(record, status=to_status, finished_at=now)

    # -- writing ---------------------------------------------------------

    def invoke(
        self,
        *,
        capability_id: str,
        adapter_id: str,
        model: str,
        call: Callable[[], str],
        model_version: str = "",
        parameters: Mapping[str, Any] | None = None,
        parameter_schema_version: str = "",
        input_text: str = "",
        journal_entry_id: str = "",
        reuse_identical: bool = False,
    ) -> ModelCallRecord:
        """Run one adapter invocation and write the record for it.

        Args:
            capability_id: The capability making the call.
            adapter_id: The adapter the call goes to.
            model: Model identifier as the adapter names it.
            call: Zero-argument callable that performs the invocation and
                returns the adapter's output.
            model_version: Provider version string for ``model``.
            parameters: Resolved parameters for the call.
            parameter_schema_version: Version of the adapter's declared
                parameter schema these parameters satisfy.
            input_text: What is being sent.
            journal_entry_id: Work-ledger entry this call belongs to.
            reuse_identical: When ``True`` and a succeeded record with the
                same content hash exists, serve that record's output
                instead of calling the adapter. Off by default: reuse
                changes what an invocation means, so it is opt-in.

        Returns:
            The written record. ``reused`` is ``True`` when the adapter
            was not called.

        Raises:
            Exception: Whatever *call* raised, after the failed record has
                been written.
        """
        record = ModelCallRecord.new(
            capability_id=capability_id,
            adapter_id=adapter_id,
            model=model,
            model_version=model_version,
            parameters=parameters,
            parameter_schema_version=parameter_schema_version,
            input_text=input_text,
            journal_entry_id=journal_entry_id,
        )

        if reuse_identical:
            source = self.find_reusable(record.content_hash())
            if source is not None:
                now = time.time()
                reused = replace(
                    record,
                    status="succeeded",
                    output_text=source.output_text,
                    reused_from=source.id,
                    started_at=now,
                    finished_at=now,
                )
                self._store(reused)
                logger.info(
                    "model call ledger: %s reused output of %s (%s)",
                    reused.id,
                    source.id,
                    capability_id,
                )
                return reused

        return self._run(record, call)

    def replay(self, record_id: str, *, call: Callable[[], str]) -> ModelCallRecord | None:
        """Re-execute a stored call and write a new record linked to it.

        The original is not touched: the replay is a separate record whose
        ``replay_of`` names the original, so both invocations keep the
        payload they actually saw. Operator-invoked - this is not a retry
        policy.

        Args:
            record_id: Id of the record to replay.
            call: Zero-argument callable that performs the re-execution
                with the original's stored parameters.

        Returns:
            The new record, or ``None`` when *record_id* is unknown.

        Raises:
            Exception: Whatever *call* raised, after the failed replay
                record has been written.
        """
        original = self.get_record(record_id)
        if original is None:
            logger.warning("model call ledger: replay of unknown record %s", record_id)
            return None

        record = ModelCallRecord.new(
            capability_id=original.capability_id,
            adapter_id=original.adapter_id,
            model=original.model,
            model_version=original.model_version,
            parameters=original.parameters,
            parameter_schema_version=original.parameter_schema_version,
            input_text=original.input_text,
            journal_entry_id=original.journal_entry_id,
            replay_of=original.id,
        )
        return self._run(record, call)

    def _run(self, record: ModelCallRecord, call: Callable[[], str]) -> ModelCallRecord:
        """Drive *record* through the status graph around *call*."""
        running = self.transition(record, "running")
        try:
            output = call()
        except Exception as exc:
            failed = replace(
                self.transition(running, "failed"),
                error=f"{type(exc).__name__}: {exc}",
            )
            self._store(failed)
            raise
        succeeded = replace(
            self.transition(running, "succeeded"),
            output_text=output,
        )
        self._store(succeeded)
        return succeeded

    def _store(self, record: ModelCallRecord) -> None:
        """Append *record* to memory and to the JSONL file."""
        self._ensure_loaded()
        self._records.append(record)
        self._append_row({"kind": _ROW_KIND_RECORD, "record": record.to_dict()})

    def _append_row(self, row: dict[str, Any]) -> None:
        """Append one JSON row to the ledger file."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as handle:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.warning("model call ledger: failed to write %s: %s", self._path, exc)
