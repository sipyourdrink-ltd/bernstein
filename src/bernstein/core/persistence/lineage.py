"""Per-artifact lineage trail (output → producer + inputs).

Adopts the manifest shape used by lineage-end-to-end systems
(``cocoindex``-style): each transform that lands a file write emits a
record of ``(output_artifact, [inputs], producer, prompt_sha)`` so a
later compliance/drift query can walk the chain back to the originating
prompt and source bytes.

Storage strategy: lineage records are appended to the existing
hash-chained WAL (``core.persistence.wal``) using
``decision_type="lineage"``. Reusing the WAL writer keeps the hash
chain intact -- lineage records are signed by the same chain that the
audit log relies on -- and avoids introducing a second durability
surface.

The CLI ``bernstein lineage <file>:<line>`` walks every WAL file under
``.sdd/runtime/wal/`` (current run only -- cross-run stitching is out
of scope) and returns every record whose output artifact matches the
requested file/line.

Schema versioning
-----------------
Records are written with an explicit ``schema_version`` field. v1
records (PR #996) carry only the producer/prompt/cost fields; v2
records add ``regulatory_class`` (free-text class for compliance
filtering) and ``customer_signature`` (base64-encoded detached
signature produced by an injected :class:`LineageSigner`). The reader
accepts both - missing v2 fields are read as ``None`` so a chain
written before v2 keeps walking.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path  # noqa: TC003 -- runtime use in dataclass and helpers
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.persistence.wal import WALReader, WALWriter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.persistence.lineage_signer import LineageSigner

logger = logging.getLogger(__name__)

LINEAGE_DECISION_TYPE = "lineage"

SCHEMA_VERSION_V1 = 1
SCHEMA_VERSION_V2 = 2
CURRENT_SCHEMA_VERSION = SCHEMA_VERSION_V2


class LineageRunIdError(ValueError):
    """Raised when a ``run_id`` would land the WAL outside the run directory.

    The WAL writer composes ``<sdd>/runtime/wal/<run_id>.wal.jsonl``; if
    *run_id* contains a path separator (``/``, ``\\``) or starts with
    ``..``, the resulting file lands in a sibling directory. The
    lineage *reader* uses a non-recursive ``glob("*.wal.jsonl")`` and
    will not surface those records, so the chain is silently lost.
    Compliance flows treat that as an integrity break -- we fail fast
    at write time instead of letting an auditor discover the gap.
    """


def _validate_run_id(run_id: str) -> str:
    """Return *run_id* unchanged when safe; raise :class:`LineageRunIdError` otherwise.

    Rejects empty strings, separators, and traversal sequences. The
    check is intentionally narrow -- it does not enforce a character
    set, only that the value composes a single file under the run
    directory.
    """
    if not run_id:
        raise LineageRunIdError("run_id must not be empty")
    if "/" in run_id or "\\" in run_id:
        raise LineageRunIdError(
            f"run_id contains a path separator ({run_id!r}); "
            "lineage records would land in a sibling directory and the "
            "reader would silently skip them",
        )
    if run_id in {".", ".."} or run_id.startswith(".."):
        raise LineageRunIdError(f"run_id resolves to a parent path: {run_id!r}")
    if "\x00" in run_id:
        raise LineageRunIdError("run_id contains a NUL byte")
    return run_id


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to a file region (an output cell or an input source).

    ``byte_start`` / ``byte_end`` are inclusive/exclusive byte offsets
    inside the file. They are optional -- a write that touches the
    whole file leaves both ``None``. ``line_start`` / ``line_end`` are
    1-indexed line numbers covering the same region; either or both
    may be ``None`` if the producer cannot supply them.
    """

    path: str
    sha256: str
    byte_start: int | None = None
    byte_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None

    def covers_line(self, line: int) -> bool:
        """Return True when *line* falls inside ``[line_start, line_end]``.

        When either bound is ``None``, the artifact is treated as
        covering the whole file, so any line matches.
        """
        if self.line_start is None or self.line_end is None:
            return True
        return self.line_start <= line <= self.line_end


@dataclass(frozen=True)
class AgentRef:
    """Identity of the producing agent run."""

    agent_id: str
    run_id: str
    tick_id: str | None = None


@dataclass(frozen=True)
class LineageRecord:
    """One lineage manifest entry (output → producer + inputs).

    Matches the cocoindex manifest shape:
    ``{output_id, inputs, fn_hash, ts}`` -- the fn_hash here is the
    producing agent's rendered-prompt SHA so two replays with identical
    prompts produce identical fn_hashes.

    Schema-v2 fields ``regulatory_class`` and ``customer_signature`` are
    optional; v1 records read back with both set to ``None``.
    ``schema_version`` is informational on the in-memory record (the
    serialised form on the WAL is the source of truth).
    """

    output_artifact: ArtifactRef
    inputs: list[ArtifactRef] = field(default_factory=list[ArtifactRef])
    producer: AgentRef = field(default_factory=lambda: AgentRef(agent_id="unknown", run_id="unknown"))
    prompt_sha: str = ""
    model: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    timestamp: float = 0.0
    regulatory_class: str | None = None
    customer_signature: str | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _artifact_to_dict(ref: ArtifactRef) -> dict[str, Any]:
    return asdict(ref)


def _artifact_from_dict(data: Any) -> ArtifactRef:
    """Reconstruct an :class:`ArtifactRef` from its serialised dict form.

    Tolerates missing or malformed v1 fields by defaulting ``path`` and
    ``sha256`` to empty strings: the reader contract guarantees that a
    truncated or partially-written WAL entry yields an ``ArtifactRef("", "")``
    rather than crashing the entire iterator. Empty-path records are
    still useful in the bundle export and signature-verification paths
    because they keep producer/prompt metadata intact.

    Parameter is typed ``Any`` because callers feed us untyped JSON -
    a corrupt WAL row could land us a list, scalar, or null. The
    isinstance narrowing converts the input into a concrete dict
    before field extraction; non-dict values short-circuit to the
    empty-ref sentinel.
    """
    if not isinstance(data, dict):
        return ArtifactRef(path="", sha256="")
    coerced = cast("dict[str, Any]", data)
    return ArtifactRef(
        path=str(coerced.get("path", "")),
        sha256=str(coerced.get("sha256", "")),
        byte_start=coerced.get("byte_start"),
        byte_end=coerced.get("byte_end"),
        line_start=coerced.get("line_start"),
        line_end=coerced.get("line_end"),
    )


def _coerce_dict(value: Any) -> dict[str, Any]:
    """Return *value* as a ``dict[str, Any]`` or ``{}`` if non-dict.

    The lineage reader receives untyped JSON; a corrupted entry where
    ``output_artifact`` is a string or null would otherwise crash
    ``_artifact_from_dict``. Centralising the narrowing keeps the
    strict-mode pyright config clean.
    """
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return {}


def _record_to_payload(record: LineageRecord) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a record into (inputs, output) dicts for the WAL append call.

    The serialised form keeps schema-v2 fields under the ``output``
    payload so old readers (v1) that only look at the legacy keys can
    still extract producer/prompt info; new readers see the v2 fields
    via ``output.regulatory_class`` and ``output.customer_signature``.
    """
    inputs_payload: dict[str, Any] = {
        "inputs": [_artifact_to_dict(a) for a in record.inputs],
        "producer": asdict(record.producer),
        "prompt_sha": record.prompt_sha,
        "model": record.model,
    }
    output_payload: dict[str, Any] = {
        "output_artifact": _artifact_to_dict(record.output_artifact),
        "cost_usd": record.cost_usd,
        "tokens": record.tokens,
        "timestamp": record.timestamp,
        "schema_version": record.schema_version,
    }
    if record.regulatory_class is not None:
        output_payload["regulatory_class"] = record.regulatory_class
    if record.customer_signature is not None:
        output_payload["customer_signature"] = record.customer_signature
    return inputs_payload, output_payload


def _record_from_wal(inputs: dict[str, Any], output: dict[str, Any], ts: float) -> LineageRecord:
    out_dict = _coerce_dict(output.get("output_artifact"))
    producer_dict = _coerce_dict(inputs.get("producer"))
    # v1 records pre-date the explicit field; treat any non-int read as v1
    # so callers can branch on schema_version unconditionally.
    schema_version_raw = output.get("schema_version")
    schema_version = schema_version_raw if isinstance(schema_version_raw, int) else SCHEMA_VERSION_V1
    regulatory_class = output.get("regulatory_class")
    customer_signature = output.get("customer_signature")
    # Normalise empty-string sentinels to ``None``. A misconfigured importer
    # that writes ``customer_signature=""`` would otherwise produce a
    # misleading "signature failed to verify" tamper alert in
    # :func:`verify_run_chain` because base64-decoding the empty string
    # yields a zero-length byte string that no Ed25519 verifier accepts.
    # Empty ``regulatory_class`` is similarly meaningless and would only
    # confuse compliance filters that test ``record.regulatory_class is
    # not None`` to detect classified records.
    regulatory_class_norm: str | None = None
    if regulatory_class is not None:
        rc_str = str(regulatory_class)
        regulatory_class_norm = rc_str or None
    customer_signature_norm: str | None = None
    if customer_signature is not None:
        cs_str = str(customer_signature)
        customer_signature_norm = cs_str or None
    inputs_list_raw = inputs.get("inputs", [])
    inputs_list: list[Any] = cast("list[Any]", inputs_list_raw) if isinstance(inputs_list_raw, list) else []
    return LineageRecord(
        output_artifact=_artifact_from_dict(out_dict),
        inputs=[_artifact_from_dict(_coerce_dict(a)) for a in inputs_list],
        producer=AgentRef(
            agent_id=str(producer_dict.get("agent_id", "unknown")),
            run_id=str(producer_dict.get("run_id", "unknown")),
            tick_id=producer_dict.get("tick_id"),
        ),
        prompt_sha=str(inputs.get("prompt_sha", "")),
        model=str(inputs.get("model", "")),
        cost_usd=float(output.get("cost_usd", 0.0)),
        tokens=int(output.get("tokens", 0)),
        timestamp=float(output.get("timestamp", ts)),
        regulatory_class=regulatory_class_norm,
        customer_signature=customer_signature_norm,
        schema_version=schema_version,
    )


def canonical_record_bytes(record: LineageRecord) -> bytes:
    """Return the canonical byte representation a signer covers.

    Uses sorted-key UTF-8 JSON without whitespace so the bytes a signer
    produces are stable across writes/reads and across Python versions.
    The ``customer_signature`` field is excluded from the canonical
    payload - a signer cannot sign over its own output.
    """
    payload: dict[str, Any] = {
        "schema_version": record.schema_version,
        "output_artifact": _artifact_to_dict(record.output_artifact),
        "inputs": [_artifact_to_dict(a) for a in record.inputs],
        "producer": asdict(record.producer),
        "prompt_sha": record.prompt_sha,
        "model": record.model,
        "cost_usd": record.cost_usd,
        "tokens": record.tokens,
        "timestamp": record.timestamp,
        "regulatory_class": record.regulatory_class,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encode_signature(sig: bytes) -> str:
    """Encode raw signature bytes for storage on the record."""
    return base64.b64encode(sig).decode("ascii")


def decode_signature(sig: str) -> bytes:
    """Reverse of :func:`encode_signature`."""
    return base64.b64decode(sig.encode("ascii"))


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of *path*, or ``""`` on read error."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


# ---------------------------------------------------------------------------
# LineageWriter
# ---------------------------------------------------------------------------


class LineageWriter:
    """Append :class:`LineageRecord` entries to the run's hash-chained WAL.

    The writer reuses :class:`WALWriter` so every emitted record is part
    of the existing chain -- no separate file, no separate signing key,
    no parallel verification path. Verifying the WAL hash chain
    (``WALReader.verify_chain``) and the audit-log HMAC chain remains a
    single operation.

    A customer-provided :class:`LineageSigner` may be injected; when
    set, every emitted record is signed (over the canonicalised v2
    payload, see :func:`canonical_record_bytes`) and the signature
    lands in :attr:`LineageRecord.customer_signature`. When unset, the
    field stays ``None`` and the chain remains v1-compatible on the
    wire (``schema_version=2`` plus null fields).
    """

    def __init__(
        self,
        writer: WALWriter,
        *,
        signer: LineageSigner | None = None,
        default_regulatory_class: str | None = None,
    ) -> None:
        self._writer = writer
        self._signer = signer
        self._default_regulatory_class = default_regulatory_class

    @classmethod
    def for_run(
        cls,
        run_id: str,
        sdd_dir: Path,
        *,
        signer: LineageSigner | None = None,
        default_regulatory_class: str | None = None,
    ) -> LineageWriter:
        """Construct a writer bound to *run_id* under *sdd_dir*.

        Raises :class:`LineageRunIdError` when *run_id* contains a path
        separator or traversal sequence -- such a value would emit
        records the lineage *reader* cannot find on subsequent walks
        (silent compliance gap).
        """
        _validate_run_id(run_id)
        return cls(
            WALWriter(run_id=run_id, sdd_dir=sdd_dir),
            signer=signer,
            default_regulatory_class=default_regulatory_class,
        )

    def emit(self, record: LineageRecord, *, actor: str | None = None) -> None:
        """Append *record* to the WAL with ``decision_type='lineage'``.

        Args:
            record: The lineage record to persist.
            actor: Optional override for the WAL ``actor`` field.
                Defaults to the producing agent_id.
        """
        if record.regulatory_class is None and self._default_regulatory_class is not None:
            record = _replace_regulatory_class(record, self._default_regulatory_class)
        if self._signer is not None and record.customer_signature is None:
            sig_bytes = self._signer.sign(canonical_record_bytes(record))
            record = _replace_customer_signature(record, encode_signature(sig_bytes))
        inputs_payload, output_payload = _record_to_payload(record)
        self._writer.append(
            decision_type=LINEAGE_DECISION_TYPE,
            inputs=inputs_payload,
            output=output_payload,
            actor=actor or record.producer.agent_id,
        )


def _replace_regulatory_class(record: LineageRecord, value: str) -> LineageRecord:
    return LineageRecord(
        output_artifact=record.output_artifact,
        inputs=record.inputs.copy(),
        producer=record.producer,
        prompt_sha=record.prompt_sha,
        model=record.model,
        cost_usd=record.cost_usd,
        tokens=record.tokens,
        timestamp=record.timestamp,
        regulatory_class=value,
        customer_signature=record.customer_signature,
        schema_version=record.schema_version,
    )


def _replace_customer_signature(record: LineageRecord, value: str) -> LineageRecord:
    return LineageRecord(
        output_artifact=record.output_artifact,
        inputs=record.inputs.copy(),
        producer=record.producer,
        prompt_sha=record.prompt_sha,
        model=record.model,
        cost_usd=record.cost_usd,
        tokens=record.tokens,
        timestamp=record.timestamp,
        regulatory_class=record.regulatory_class,
        customer_signature=value,
        schema_version=record.schema_version,
    )


# ---------------------------------------------------------------------------
# LineageReader (queries across all WAL files in the run)
# ---------------------------------------------------------------------------


class LineageReader:
    """Read lineage records from one or more WAL files.

    Matches the artifact-indexed access pattern: callers typically ask
    "show me everything that produced ``src/foo.py``" rather than
    "show me everything in event order".
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._wal_dir = sdd_dir / "runtime" / "wal"

    def _iter_run_ids(self) -> Iterator[str]:
        if not self._wal_dir.is_dir():
            return
        for wal_file in sorted(self._wal_dir.glob("*.wal.jsonl")):
            yield wal_file.name.removesuffix(".wal.jsonl")

    def iter_records(self, run_id: str | None = None) -> Iterator[LineageRecord]:
        """Yield every lineage record for *run_id* (or every run when ``None``)."""
        for record, _entry_hash, _seq, _prev_hash in self.iter_records_with_chain(run_id=run_id):
            yield record

    def iter_records_with_chain(
        self,
        run_id: str | None = None,
    ) -> Iterator[tuple[LineageRecord, str, int, str]]:
        """Yield ``(record, entry_hash, seq, prev_hash)`` for lineage WAL rows.

        OpenLineage export (#4914) needs the chain coordinates beside each
        record so the projection can bind ``bernstein_chain.chain_head_hash``
        without re-parsing the WAL a second time.
        """
        run_ids = [run_id] if run_id else list(self._iter_run_ids())
        for rid in run_ids:
            try:
                reader = WALReader(run_id=rid, sdd_dir=self._sdd_dir)
                for _raw, entry in reader.iter_parsed_entries():
                    if entry.decision_type != LINEAGE_DECISION_TYPE:
                        continue
                    yield (
                        _record_from_wal(entry.inputs, entry.output, entry.timestamp),
                        entry.entry_hash,
                        entry.seq,
                        entry.prev_hash,
                    )
            except FileNotFoundError:
                continue

    def lookup(
        self,
        path: str,
        line: int | None = None,
        *,
        run_id: str | None = None,
    ) -> list[LineageRecord]:
        """Return records whose ``output_artifact`` matches ``path[:line]``.

        Args:
            path: File path (matched by exact string equality).
            line: 1-indexed line. When ``None``, every record for *path*
                is returned. When set, only records whose artifact
                covers that line are returned.
            run_id: Restrict to one run; defaults to all runs in the WAL
                directory.

        Returns:
            Newest record last (chronological order).
        """
        results: list[LineageRecord] = []
        for record in self.iter_records(run_id=run_id):
            if record.output_artifact.path != path:
                continue
            if line is not None and not record.output_artifact.covers_line(line):
                continue
            results.append(record)
        return results


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def compress_rotated_lineage(sdd_dir: Path) -> list[str]:
    """Gzip rotated WAL files containing lineage records.

    The active ``<run_id>.wal.jsonl`` is left alone so the hash-chain
    writer does not race with the compactor. Only rotated backups
    (``<run_id>.wal.jsonl.<N>``) are compressed in place; the
    ``.gz`` file replaces the rotated original on success.

    Args:
        sdd_dir: ``.sdd`` directory root.

    Returns:
        List of file names that were compressed.
    """
    wal_dir = sdd_dir / "runtime" / "wal"
    if not wal_dir.is_dir():
        return []

    compressed: list[str] = []
    for wal_file in wal_dir.iterdir():
        name = wal_file.name
        if not wal_file.is_file() or ".wal.jsonl" not in name:
            continue
        if name.endswith(".wal.jsonl"):
            continue  # active file, skip
        if name.endswith(".gz"):
            continue
        gz_path = wal_file.with_suffix(wal_file.suffix + ".gz")
        if gz_path.exists():
            continue
        try:
            with wal_file.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            wal_file.unlink()
            compressed.append(name)
        except OSError:
            logger.warning("lineage: failed to compress %s", wal_file, exc_info=True)
            if gz_path.exists():
                gz_path.unlink(missing_ok=True)
    return compressed


# ---------------------------------------------------------------------------
# Bundle export (for ``bernstein debug bundle``)
# ---------------------------------------------------------------------------


def collect_bundle_records(sdd_dir: Path, *, max_records: int = 500) -> list[dict[str, Any]]:
    """Return at most *max_records* lineage records as plain dicts.

    Used by :mod:`bernstein.core.observability.debug_bundle` so the
    debug bundle can include a per-run ``lineage.jsonl`` slice. Records
    are tail-truncated (newest *max_records* kept) -- compliance use
    cases generally want the latest activity, not the oldest.
    """
    reader = LineageReader(sdd_dir)
    records: list[dict[str, Any]] = [record_to_dict(record) for record in reader.iter_records()]
    if len(records) > max_records:
        records = records[-max_records:]
    return records


def record_to_dict(record: LineageRecord) -> dict[str, Any]:
    """Return a plain-dict view of *record*, including v2 fields."""
    return {
        "schema_version": record.schema_version,
        "output_artifact": _artifact_to_dict(record.output_artifact),
        "inputs": [_artifact_to_dict(a) for a in record.inputs],
        "producer": asdict(record.producer),
        "prompt_sha": record.prompt_sha,
        "model": record.model,
        "cost_usd": record.cost_usd,
        "tokens": record.tokens,
        "timestamp": record.timestamp,
        "regulatory_class": record.regulatory_class,
        "customer_signature": record.customer_signature,
    }


def bundle_records_to_jsonl(records: list[dict[str, Any]]) -> str:
    """Serialise *records* as a JSONL string for the debug bundle."""
    return "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records) + ("\n" if records else "")


# ---------------------------------------------------------------------------
# Chain verification (Phase 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineageVerificationResult:
    """Outcome of a chain verification pass.

    ``ok`` is True only when both the WAL hash chain is intact and
    every customer signature (when a verifier is supplied) validates.
    ``errors`` accumulates one entry per problem so a single broken
    record in a long chain does not mask the rest.
    """

    ok: bool
    errors: list[str] = field(default_factory=list[str])
    record_count: int = 0
    run_ids: list[str] = field(default_factory=list[str])


def verify_run_chain(
    sdd_dir: Path,
    run_id: str,
    *,
    verifier: Any = None,
) -> LineageVerificationResult:
    """Verify the WAL hash chain and (optionally) customer signatures for *run_id*.

    Args:
        sdd_dir: ``.sdd`` root.
        run_id: The run whose WAL is verified.
        verifier: Optional :class:`LineageVerifier` (a duck-typed object
            with ``verify(payload, signature) -> bool``). When provided,
            every record carrying a ``customer_signature`` is re-verified
            against the canonicalised payload.

    Returns:
        A :class:`LineageVerificationResult`. ``ok=False`` when the WAL
        chain is broken or any signature fails to validate.
    """
    errors: list[str] = []
    try:
        wal_reader = WALReader(run_id=run_id, sdd_dir=sdd_dir)
    except FileNotFoundError as exc:
        return LineageVerificationResult(ok=False, errors=[f"wal not found for run {run_id}: {exc}"], run_ids=[run_id])

    try:
        chain_ok, chain_errors = wal_reader.verify_chain()
    except FileNotFoundError as exc:
        return LineageVerificationResult(ok=False, errors=[f"wal missing for run {run_id}: {exc}"], run_ids=[run_id])
    if not chain_ok:
        errors.extend(f"wal: {e}" for e in chain_errors)

    record_count = 0
    if verifier is not None:
        for entry in wal_reader.iter_entries():
            if entry.decision_type != LINEAGE_DECISION_TYPE:
                continue
            record = _record_from_wal(entry.inputs, entry.output, entry.timestamp)
            record_count += 1
            sig_b64 = record.customer_signature
            if sig_b64 is None:
                continue
            try:
                sig_bytes = decode_signature(sig_b64)
            except (ValueError, TypeError) as exc:
                errors.append(f"record seq={entry.seq}: malformed signature ({exc})")
                continue
            if not verifier.verify(canonical_record_bytes(record), sig_bytes):
                errors.append(f"record seq={entry.seq} ({record.output_artifact.path}): signature failed to verify")
    else:
        for entry in wal_reader.iter_entries():
            if entry.decision_type == LINEAGE_DECISION_TYPE:
                record_count += 1

    return LineageVerificationResult(
        ok=len(errors) == 0,
        errors=errors,
        record_count=record_count,
        run_ids=[run_id],
    )
