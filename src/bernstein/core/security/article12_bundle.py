"""EU AI Act Article 12 evidence-pack generator.

Article 12 of the EU AI Act (Regulation (EU) 2024/1689) requires high-risk
AI systems to keep automatic event-logs, retain them at least 6 months
and up to 10 years for high-risk classifications, and produce them on
request for a conformity assessment.

This module implements the smallest viable Article-12-conformant evidence
pack:

    bernstein audit export --article-12 \
        --since ISO --until ISO [--output PATH] [--risk-class high|limited|minimal]

The resulting bundle is:

* **HMAC-chained** - the full audit log slice for the period, with each
  entry's chain anchor preserved (verifiable by the existing
  ``AuditLog.verify`` API or the standalone verifier shipped here).
* **Deterministic** - the same input window produces a byte-identical
  bundle on every run (sorted JSONL, fixed key order, no timestamps in
  the bundle metadata that depend on wall-clock).
* **Retention-pinned** - bundle metadata records the ``retention_until``
  date matching Article 12(3): 10 years for high-risk systems, 6 months
  minimum otherwise.
* **Self-describing** - manifests for the input/output catalog (data
  governance evidence per Article 10 / Annex IV §2(d)) and a clause map
  showing which bundle artefact maps to which Article 12 sub-clause.

Deferred (not in this slice; tracked in the originating ticket):

* archival/storage layer (S3 Object Lock, immutable Postgres)
* Agent Card ledger (depends on ``a2a-v1-signed-agent-card``)
* full conformity-assessment paperwork (Article 43)
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import io
import json
import logging
import operator
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Article 12(3) - high-risk AI systems must keep logs for the lifetime
#: of the system *and* at least 10 years (Article 19(1)).  Bernstein
#: pins 10 years as the default cap; operators can opt to keep longer.
HIGH_RISK_RETENTION_YEARS: int = 10

#: Article 12(3) - minimum retention for any AI system covered by
#: Article 12 ("at least six months").
MINIMUM_RETENTION_DAYS: int = 183  # half a calendar year, leap-safe lower bound

#: Schema version emitted in the bundle manifest. Bump on any breaking
#: change to artefact ordering, file names, or hash inputs.
BUNDLE_SCHEMA_VERSION: str = "1.0.0"

RiskClass = Literal["high", "limited", "minimal"]

_GENESIS_HMAC = "0" * 64

#: Per-run audit chain location. Each orchestrator run writes its
#: HMAC-chained audit slice to ``<sdd>/runtime/audit/<run_id>.audit.jsonl``
#: in addition to the calendar-rotated daily logs at ``<sdd>/audit/``.
#: ``assemble_from_run`` reads this file directly so the bundle is anchored
#: to one run rather than a wall-clock window.
RUN_AUDIT_DIR_NAME: str = "runtime/audit"
RUN_AUDIT_FILE_SUFFIX: str = ".audit.jsonl"

#: Default location of the YAML clause map shipped with bernstein.
#: Overridable via the ``clause_map_path`` argument to
#: :func:`assemble_from_run`.
DEFAULT_CLAUSE_MAP_PATH: str = "config/eu_ai_act_clause_map.yaml"


@dataclass(frozen=True, slots=True)
class RetentionPin:
    """Article 12(3) retention metadata for an evidence bundle.

    Attributes:
        risk_class: EU AI Act risk classification driving retention.
        retention_days: Computed retention horizon in days.
        retention_until: ISO-8601 date by which deletion is forbidden.
        last_event_ts: ISO-8601 timestamp of the latest covered event.
    """

    risk_class: RiskClass
    retention_days: int
    retention_until: str
    last_event_ts: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this pin."""
        return {
            "risk_class": self.risk_class,
            "retention_days": self.retention_days,
            "retention_until": self.retention_until,
            "last_event_ts": self.last_event_ts,
        }


@dataclass(frozen=True, slots=True)
class Article12Bundle:
    """Result of an Article 12 evidence-pack export.

    Attributes:
        bundle_id: Stable hash over (since, until, risk_class) - the
            deterministic identifier auditors can quote when referring
            to a bundle.
        since: Inclusive lower bound of the export window (ISO-8601).
        until: Exclusive upper bound of the export window (ISO-8601).
        risk_class: Risk classification driving retention pin.
        event_count: Number of audit events in the bundle.
        chain_anchor: HMAC of the last event in the period (or the
            genesis sentinel when no events were captured).
        retention: Article 12(3) retention metadata.
        archive_path: On-disk path to the produced zip (``None`` when
            ``write=False``).
        sha256: SHA-256 of the produced zip's canonical contents.
    """

    bundle_id: str
    since: str
    until: str
    risk_class: RiskClass
    event_count: int
    chain_anchor: str
    retention: RetentionPin
    archive_path: Path | None
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this bundle's manifest."""
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_id": self.bundle_id,
            "since": self.since,
            "until": self.until,
            "risk_class": self.risk_class,
            "event_count": self.event_count,
            "chain_anchor": self.chain_anchor,
            "retention": self.retention.to_dict(),
            "sha256": self.sha256,
        }


# ---------------------------------------------------------------------------
# Retention enforcement
# ---------------------------------------------------------------------------


def compute_retention_pin(
    risk_class: RiskClass,
    last_event_ts: str,
) -> RetentionPin:
    """Compute the Article 12(3) retention horizon for a bundle.

    Args:
        risk_class: EU AI Act risk class for the covered system.
        last_event_ts: ISO-8601 timestamp of the last logged event.

    Returns:
        A :class:`RetentionPin` with the deletion horizon.

    Raises:
        ValueError: If ``last_event_ts`` is not parseable as ISO-8601.
    """
    last_dt = _parse_iso(last_event_ts)
    # 10 years (Article 19(1)) for high-risk; 6-month floor otherwise.
    # 365.25 average accounts for leap years.
    days = round(HIGH_RISK_RETENTION_YEARS * 365.25) if risk_class == "high" else MINIMUM_RETENTION_DAYS
    until = (last_dt + timedelta(days=days)).date().isoformat()
    return RetentionPin(
        risk_class=risk_class,
        retention_days=days,
        retention_until=until,
        last_event_ts=last_event_ts,
    )


def validate_retention(
    pin: RetentionPin,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Validate that a retention pin still satisfies Article 12(3).

    Args:
        pin: The retention pin attached to the bundle at export time.
        now: Override clock for tests.  Defaults to UTC ``now``.

    Returns:
        ``(ok, reason)``.  ``ok`` is True when the deletion horizon is
        in the future *and* the minimum retention floor is respected
        for the risk class; ``reason`` is empty when ``ok`` is True.
    """
    current = now or datetime.now(tz=UTC)
    last_dt = _parse_iso(pin.last_event_ts)
    elapsed = (current - last_dt).days

    floor = round(HIGH_RISK_RETENTION_YEARS * 365.25) if pin.risk_class == "high" else MINIMUM_RETENTION_DAYS
    if pin.retention_days < floor:
        return (
            False,
            f"retention_days={pin.retention_days} below Article 12(3) floor of {floor} for risk_class={pin.risk_class}",
        )

    horizon = _parse_iso(pin.retention_until + "T00:00:00+00:00")
    if current >= horizon:
        return (
            False,
            f"retention horizon {pin.retention_until} reached ({elapsed}d since last event)",
        )

    return True, ""


# ---------------------------------------------------------------------------
# Bundle assembler
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SourceEvent:
    """Internal representation of an audit event read from disk."""

    timestamp: str
    raw: dict[str, Any]


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string permissively, normalising trailing ``Z``."""
    cleaned = value.replace("Z", "+00:00") if value.endswith("Z") else value
    return datetime.fromisoformat(cleaned)


def _read_event_window(
    audit_dir: Path,
    since: str,
    until: str,
) -> list[_SourceEvent]:
    """Read audit events from JSONL files within ``[since, until)``.

    Args:
        audit_dir: Directory of daily ``YYYY-MM-DD.jsonl`` audit files.
        since: ISO-8601 lower bound (inclusive).
        until: ISO-8601 upper bound (exclusive).

    Returns:
        Events sorted by ``timestamp`` then by their original chain
        ``hmac`` to break ties deterministically.
    """
    events: list[_SourceEvent] = []
    if not audit_dir.is_dir():
        return events
    for path in sorted(audit_dir.glob("*.jsonl")):
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(entry.get("timestamp", ""))
            if not ts or ts < since or ts >= until:
                continue
            events.append(_SourceEvent(timestamp=ts, raw=entry))
    events.sort(key=lambda e: (e.timestamp, str(e.raw.get("hmac", ""))))
    return events


def _build_event_log(events: list[_SourceEvent]) -> bytes:
    """Serialise events as canonical JSONL (sorted keys, ``\\n`` newlines).

    Must match the on-disk byte layout produced by
    :class:`bernstein.core.security.audit.AuditLog` because
    ``AuditLog.verify`` re-canonicalises each line and demands
    byte-equality as anti-tamper evidence (see
    ``security/audit.py::_canonical_line_check``). The audit writer uses
    Python's default ``json.dumps`` separators (``', '`` / ``': '``);
    we match that here so a bundle replayed through ``AuditLog.verify``
    does not surface as ``non-canonical line bytes``.
    """
    buf = io.BytesIO()
    for ev in events:
        line = json.dumps(ev.raw, sort_keys=True)
        buf.write(line.encode("utf-8"))
        buf.write(b"\n")
    return buf.getvalue()


def _build_data_catalog(events: list[_SourceEvent]) -> bytes:
    """Build the input/output data-governance catalog (Article 12(1)(a)).

    Aggregates per-resource activity counts so an auditor can see what
    data classes were touched without scanning the raw log.
    """
    catalog: dict[str, dict[str, int]] = {}
    for ev in events:
        rtype = str(ev.raw.get("resource_type", "")) or "unknown"
        rid = str(ev.raw.get("resource_id", "")) or "unknown"
        bucket = catalog.setdefault(rtype, {})
        bucket[rid] = bucket.get(rid, 0) + 1
    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "resources": {rtype: dict(sorted(items.items())) for rtype, items in sorted(catalog.items())},
        "total_events": len(events),
    }
    return _bundle_json(payload)


def _build_clause_map() -> bytes:
    """Build the Article 12 conformance clause map.

    Maps each artefact in the bundle to the Article 12 sub-clause it
    addresses. Auditors use this to short-circuit their checklist.
    """
    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "regulation": "EU AI Act, Regulation (EU) 2024/1689",
        "article": 12,
        "mappings": [
            {
                "clause": "12(1)",
                "requirement": ("Automatic recording of events ('logs') over the lifetime of the system."),
                "artefact": "events.jsonl",
            },
            {
                "clause": "12(2)(a)",
                "requirement": (
                    "Identification of situations that may result in the AI "
                    "system presenting a risk within the meaning of Article "
                    "79(1) or in a substantial modification."
                ),
                "artefact": "events.jsonl (event_type, outcome fields)",
            },
            {
                "clause": "12(2)(b)",
                "requirement": "Facilitation of the post-market monitoring.",
                "artefact": "data_catalog.json",
            },
            {
                "clause": "12(2)(c)",
                "requirement": ("Monitoring of the operation of high-risk AI systems referred to in Article 26(5)."),
                "artefact": "events.jsonl + chain_anchor in manifest.json",
            },
            {
                "clause": "12(3)",
                "requirement": (
                    "Logs kept for at least 6 months unless otherwise "
                    "provided; 10 years for high-risk under Article 19(1)."
                ),
                "artefact": "manifest.json (retention block)",
            },
        ],
        "deferred": [
            "Agent Card ledger (depends on a2a-v1-signed-agent-card)",
            "Storage backends (S3 Object Lock, immutable Postgres)",
            "Article 43 conformity-assessment paperwork",
        ],
    }
    return _bundle_json(payload)


def _bundle_json(payload: dict[str, Any]) -> bytes:
    """Serialise a bundle file: indented, sorted keys, trailing newline.

    Presentation bytes a reviewer reads; the bundle's hashes are computed over
    the stored bytes. Not a signing canonicalization
    (:mod:`bernstein.core.security.canonical` owns that rule).
    """
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _bundle_id(since: str, until: str, risk_class: str) -> str:
    """Compute the deterministic bundle id."""
    seed = f"{since}|{until}|{risk_class}".encode()
    return hashlib.sha256(seed).hexdigest()[:32]


def build_article12_bundle(
    audit_dir: Path,
    since: str,
    until: str,
    *,
    risk_class: RiskClass = "limited",
    output_dir: Path | None = None,
    write: bool = True,
) -> Article12Bundle:
    """Assemble an Article 12 evidence pack for ``[since, until)``.

    Args:
        audit_dir: Directory containing HMAC-chained ``*.jsonl`` audit
            log files (typically ``.sdd/audit``).
        since: ISO-8601 inclusive lower bound.
        until: ISO-8601 exclusive upper bound.
        risk_class: EU AI Act risk classification driving retention.
        output_dir: Where to write the bundle zip.  Defaults to
            ``audit_dir.parent / 'evidence'`` (i.e. ``.sdd/evidence/``).
        write: If False, build the bundle in-memory only and skip the
            on-disk write - useful for ``--dry-run`` and tests.

    Returns:
        An :class:`Article12Bundle` describing the produced pack.
    """
    if since >= until:
        raise ValueError(f"since={since!r} must be < until={until!r}")

    events = _read_event_window(audit_dir, since, until)
    event_log = _build_event_log(events)
    data_catalog = _build_data_catalog(events)
    clause_map = _build_clause_map()

    last_event_ts = events[-1].timestamp if events else since
    chain_anchor = str(events[-1].raw.get("hmac", _GENESIS_HMAC)) if events else _GENESIS_HMAC
    retention = compute_retention_pin(risk_class, last_event_ts)

    artefact_hashes = {
        "events.jsonl": hashlib.sha256(event_log).hexdigest(),
        "data_catalog.json": hashlib.sha256(data_catalog).hexdigest(),
        "clause_map.json": hashlib.sha256(clause_map).hexdigest(),
    }
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": _bundle_id(since, until, risk_class),
        "since": since,
        "until": until,
        "risk_class": risk_class,
        "event_count": len(events),
        "chain_anchor": chain_anchor,
        "retention": retention.to_dict(),
        "artefacts": dict(sorted(artefact_hashes.items())),
    }
    manifest_bytes = _bundle_json(manifest)

    archive_bytes = _zip_artefacts(
        manifest_bytes=manifest_bytes,
        event_log=event_log,
        data_catalog=data_catalog,
        clause_map=clause_map,
    )
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    archive_path: Path | None = None
    if write:
        target_dir = output_dir or (audit_dir.parent / "evidence")
        target_dir.mkdir(parents=True, exist_ok=True)
        archive_path = target_dir / f"article12_{manifest['bundle_id']}.zip"
        archive_path.write_bytes(archive_bytes)

    return Article12Bundle(
        bundle_id=str(manifest["bundle_id"]),
        since=since,
        until=until,
        risk_class=risk_class,
        event_count=len(events),
        chain_anchor=chain_anchor,
        retention=retention,
        archive_path=archive_path,
        sha256=archive_sha256,
    )


def _zip_artefacts(
    *,
    manifest_bytes: bytes,
    event_log: bytes,
    data_catalog: bytes,
    clause_map: bytes,
) -> bytes:
    """Pack all artefacts into a deterministic zip.

    Determinism rules:

    * Fixed file order (sorted).
    * Fixed mtime (zip cannot store < 1980 so we use 1980-01-01).
    * Stored mode 0644.
    """
    fixed_dt = (1980, 1, 1, 0, 0, 0)
    files: tuple[tuple[str, bytes], ...] = tuple(
        sorted(
            (
                ("manifest.json", manifest_bytes),
                ("events.jsonl", event_log),
                ("data_catalog.json", data_catalog),
                ("clause_map.json", clause_map),
            ),
            key=operator.itemgetter(0),
        )
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files:
            info = zipfile.ZipInfo(filename=name, date_time=fixed_dt)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, payload)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Standalone verifier (used by the CLI; can also be invoked from a script)
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """Outcome of verifying an Article 12 bundle.

    Attributes:
        ok: True iff every check passed.
        errors: Per-check failure messages.
        manifest: The parsed manifest, when readable.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)


def verify_bundle(archive_path: Path, *, now: datetime | None = None) -> VerificationResult:
    """Verify a bundle's manifest hashes and retention pin.

    The check is intentionally narrow: it confirms (a) artefact hashes
    in the manifest match the on-disk content and (b) the retention pin
    still satisfies Article 12(3).  HMAC-chain verification of the
    embedded ``events.jsonl`` requires the original key and is delegated
    to :class:`bernstein.core.security.audit.AuditLog.verify` (or the
    forthcoming standalone verifier script).

    Args:
        archive_path: Path to the produced bundle zip.
        now: Override clock (for tests).

    Returns:
        A :class:`VerificationResult`.
    """
    errors: list[str] = []
    manifest: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(archive_path) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            for name, expected in manifest.get("artefacts", {}).items():
                got = hashlib.sha256(zf.read(name)).hexdigest()
                if got != expected:
                    errors.append(f"hash mismatch for {name}: {got!r} != {expected!r}")
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        errors.append(f"failed to read bundle: {exc}")
        return VerificationResult(ok=False, errors=errors, manifest=manifest)

    retention = manifest.get("retention") or {}
    if retention:
        pin = RetentionPin(
            risk_class=retention.get("risk_class", "limited"),
            retention_days=int(retention.get("retention_days", 0)),
            retention_until=str(retention.get("retention_until", "")),
            last_event_ts=str(retention.get("last_event_ts", "")),
        )
        ok, reason = validate_retention(pin, now=now)
        if not ok:
            errors.append(f"retention check failed: {reason}")
    else:
        errors.append("manifest missing retention block")

    return VerificationResult(ok=not errors, errors=errors, manifest=manifest)


# ---------------------------------------------------------------------------
# Run-scoped audit chain reader (assemble_from_run helpers)
# ---------------------------------------------------------------------------


class ChainBreakError(RuntimeError):
    """Raised when in-line HMAC chain verification fails for a run audit slice."""


@dataclass(frozen=True)
class _ChainEvent:
    """Internal: an entry pulled from a per-run audit chain file.

    Carries the full raw JSON entry plus the recomputed-vs-stored HMAC
    used by :func:`_verify_run_chain` so the caller can both detect a
    break *and* surface the exact line.
    """

    line_no: int
    timestamp: str
    raw: dict[str, Any]
    stored_hmac: str
    prev_hmac: str


def _verify_run_chain(
    run_audit_path: Path,
    *,
    key: bytes,
) -> list[_ChainEvent]:
    """Walk a per-run audit JSONL, verifying HMAC links eagerly.

    The runtime per-run file uses the same canonical HMAC payload as
    :class:`bernstein.core.security.audit.AuditLog` - ``HMAC(key,
    prev_hmac + canonical_json(entry_without_hmac))`` - so we can
    re-derive each event's MAC and refuse to proceed at the first break.

    Args:
        run_audit_path: Path to ``<run_id>.audit.jsonl``.
        key: Raw HMAC key bytes (loader's responsibility).

    Returns:
        Verified events, in file order.

    Raises:
        ChainBreakError: First verification failure (with line number).
        FileNotFoundError: When *run_audit_path* does not exist.
    """
    if not run_audit_path.is_file():
        raise FileNotFoundError(f"run audit file not found: {run_audit_path}")

    events: list[_ChainEvent] = []
    prev = _GENESIS_HMAC
    for line_no, raw_line in enumerate(run_audit_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChainBreakError(f"{run_audit_path.name}:{line_no}: invalid JSON - {exc}") from None

        if not isinstance(entry, dict):
            raise ChainBreakError(
                f"{run_audit_path.name}:{line_no}: expected object, got {type(entry).__name__}",
            )

        stored_hmac = str(entry.get("hmac", ""))
        # Compute expected HMAC over the stripped payload (without 'hmac' key).
        stripped = {k: v for k, v in entry.items() if k != "hmac"}
        recorded_prev = str(stripped.get("prev_hmac", ""))
        if recorded_prev != prev:
            raise ChainBreakError(
                f"{run_audit_path.name}:{line_no}: prev_hmac mismatch "
                f"(expected {prev[:16]}…, got {recorded_prev[:16]}…)",
            )

        payload = prev + json.dumps(stripped, sort_keys=True)
        expected = _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        if stored_hmac != expected:
            raise ChainBreakError(
                f"{run_audit_path.name}:{line_no}: HMAC mismatch (expected {expected[:16]}…, got {stored_hmac[:16]}…)",
            )

        events.append(
            _ChainEvent(
                line_no=line_no,
                timestamp=str(entry.get("timestamp", "")),
                raw=entry,
                stored_hmac=stored_hmac,
                prev_hmac=prev,
            ),
        )
        prev = stored_hmac

    return events


def _filter_events_by_window(
    events: Iterable[_ChainEvent],
    *,
    since: datetime,
    until: datetime,
) -> list[_SourceEvent]:
    """Project verified events into the ``[since, until)`` window."""
    out: list[_SourceEvent] = []
    for ev in events:
        if not ev.timestamp:
            continue
        try:
            ts_dt = _parse_iso(ev.timestamp)
        except (ValueError, TypeError):
            continue
        if ts_dt < since or ts_dt >= until:
            continue
        out.append(_SourceEvent(timestamp=ev.timestamp, raw=ev.raw))
    out.sort(key=lambda e: (e.timestamp, str(e.raw.get("hmac", ""))))
    return out


# ---------------------------------------------------------------------------
# Lineage / data-catalog cross-reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CatalogedArtifact:
    """Internal: lineage record projected into the data-catalog shape."""

    path: str
    sha256: str
    regulatory_class: str | None
    producer_agent_id: str
    producer_run_id: str
    producer_tick_id: str | None
    timestamp: float


def _walk_lineage_records(
    sdd_dir: Path,
    *,
    run_id: str,
    since: datetime,
    until: datetime,
) -> list[_CatalogedArtifact]:
    """Collect lineage records produced inside ``[since, until)`` for *run_id*.

    Reads from the same WAL the orchestrator writes via
    :class:`bernstein.core.persistence.lineage.LineageWriter`, so the
    catalog is grounded in the production hash chain - not a parallel
    file the agent would need to cooperate with separately.

    Args:
        sdd_dir: Project ``.sdd/`` directory (containing ``runtime/wal``).
        run_id: Restrict to records produced by this run.
        since: Inclusive lower bound (UTC).
        until: Exclusive upper bound (UTC).

    Returns:
        Stable-ordered list of catalog projections.
    """
    # Lazy import - keeps article12_bundle a leaf-importable module for
    # tooling that doesn't pull the persistence stack.
    try:
        from bernstein.core.persistence.lineage import LineageReader
    except ImportError:  # pragma: no cover - defensive, lineage ships alongside
        return []

    reader = LineageReader(sdd_dir)
    since_ts = since.timestamp()
    until_ts = until.timestamp()
    seen: set[tuple[str, str]] = set()
    catalog: list[_CatalogedArtifact] = []
    for record in reader.iter_records(run_id=run_id):
        # Records pre-date timestamping in v1; treat 0.0 as "always inside" so
        # we don't drop them silently.
        ts = float(record.timestamp or 0.0)
        if ts and (ts < since_ts or ts >= until_ts):
            continue
        out = record.output_artifact
        if not out.path:
            continue
        key = (out.path, out.sha256)
        if key in seen:
            continue
        seen.add(key)
        catalog.append(
            _CatalogedArtifact(
                path=out.path,
                sha256=out.sha256,
                regulatory_class=record.regulatory_class,
                producer_agent_id=record.producer.agent_id,
                producer_run_id=record.producer.run_id,
                producer_tick_id=record.producer.tick_id,
                timestamp=ts,
            ),
        )
    catalog.sort(key=lambda a: (a.path, a.sha256))
    return catalog


def _build_data_catalog_with_lineage(
    events: list[_SourceEvent],
    artefacts: list[_CatalogedArtifact],
) -> bytes:
    """Build the Article 12(1)(a) catalog enriched with lineage artefacts.

    Combines the per-resource activity counters from
    :func:`_build_data_catalog` with a flat list of lineage-tracked
    artefacts so an auditor can correlate event activity with the
    artefacts the run produced.
    """
    catalog: dict[str, dict[str, int]] = {}
    for ev in events:
        rtype = str(ev.raw.get("resource_type", "")) or "unknown"
        rid = str(ev.raw.get("resource_id", "")) or "unknown"
        bucket = catalog.setdefault(rtype, {})
        bucket[rid] = bucket.get(rid, 0) + 1

    artefact_payload = [
        {
            "path": a.path,
            "sha256": a.sha256,
            "regulatory_class": a.regulatory_class,
            "producer": {
                "agent_id": a.producer_agent_id,
                "run_id": a.producer_run_id,
                "tick_id": a.producer_tick_id,
            },
        }
        for a in artefacts
    ]

    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "resources": {rtype: dict(sorted(items.items())) for rtype, items in sorted(catalog.items())},
        "total_events": len(events),
        "lineage_artefacts": artefact_payload,
        "lineage_artefact_count": len(artefact_payload),
    }
    return _bundle_json(payload)


# ---------------------------------------------------------------------------
# Clause map loading
# ---------------------------------------------------------------------------


def _load_clause_map_from_yaml(path: Path) -> bytes:
    """Read the YAML clause-map config and serialise it canonically.

    The on-disk YAML is the source of truth for the operator: the bundle
    embeds the JSON projection so an auditor reading the bundle does not
    need a YAML parser.

    Args:
        path: Path to the YAML clause map.

    Returns:
        Canonical-JSON bytes ready for inclusion in the bundle.

    Raises:
        FileNotFoundError: When *path* does not exist.
        ValueError: When the YAML is structurally invalid for our schema.
    """
    if not path.is_file():
        raise FileNotFoundError(f"clause map config not found: {path}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - yaml ships in core deps
        raise ImportError(
            "PyYAML is required for assemble_from_run; install via the core dependency set",
        ) from exc

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"clause map at {path} is not a YAML mapping")
    if "mappings" not in parsed or not isinstance(parsed["mappings"], list):
        raise ValueError(f"clause map at {path} missing 'mappings' list")
    return _bundle_json(parsed)


# ---------------------------------------------------------------------------
# Public: assemble_from_run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunBundleResult:
    """Output of :func:`assemble_from_run` - the bundle plus catalog stats.

    Attributes:
        bundle: The :class:`Article12Bundle` describing the produced pack.
        run_id: The run id that anchored the bundle.
        chain_event_count: Total events read from the per-run chain file
            *before* window filtering - useful for sanity-checking that
            the window did not drop anything unexpectedly.
        catalog_artefact_count: Number of lineage artefacts cross-
            referenced into ``data_catalog.json``.
    """

    bundle: Article12Bundle
    run_id: str
    chain_event_count: int
    catalog_artefact_count: int


def _resolve_clause_map_path(
    workdir: Path,
    clause_map_path: Path | None,
) -> Path:
    """Pick the on-disk clause map file, falling back to the bundled default."""
    if clause_map_path is not None:
        return clause_map_path
    candidate = workdir / DEFAULT_CLAUSE_MAP_PATH
    if candidate.is_file():
        return candidate
    # Fall back to the package-local default shipped under repo root.
    package_default = Path(__file__).resolve().parents[4] / DEFAULT_CLAUSE_MAP_PATH
    return package_default


def assemble_from_run(
    run_id: str,
    since: datetime,
    until: datetime,
    *,
    sdd_dir: Path | None = None,
    workdir: Path | None = None,
    risk_class: RiskClass = "limited",
    audit_key: bytes | None = None,
    clause_map_path: Path | None = None,
    output_dir: Path | None = None,
    write: bool = True,
) -> RunBundleResult:
    """Assemble an Article 12 evidence pack from a real orchestrator run.

    Pipeline:

    1. Resolve the per-run audit chain at
       ``<sdd>/runtime/audit/<run_id>.audit.jsonl`` and verify every
       HMAC link in-place. A break aborts the bundle (no partial export).
    2. Filter the verified events to ``[since, until)``.
    3. Walk ``LineageReader`` for *run_id* and project each output
       artefact in-window into ``data_catalog.json`` with the producer's
       agent / tick ids and any ``regulatory_class``.
    4. Resolve the clause map from the YAML config (override-friendly).
    5. Emit a deterministic bundle with the standard manifest plus the
       lineage-enriched catalog.

    Args:
        run_id: The run identifier whose chain to bundle.
        since: Inclusive lower bound of the export window.
        until: Exclusive upper bound of the export window.
        sdd_dir: Override for the ``.sdd`` root (defaults to
            ``workdir / .sdd``).
        workdir: Project root; used to resolve default config paths.
            Defaults to ``Path.cwd()``.
        risk_class: EU AI Act risk classification driving retention.
        audit_key: HMAC key bytes for chain verification. When ``None``,
            uses :func:`load_or_create_audit_key` (matches the path the
            orchestrator's :class:`AuditLog` uses).
        clause_map_path: Optional override for the YAML clause-map file.
        output_dir: Where to write the bundle zip. Defaults to
            ``<sdd>/evidence``.
        write: When False, build everything in-memory and skip the disk
            write - useful for ``--dry-run`` and tests.

    Returns:
        :class:`RunBundleResult` carrying the bundle plus diagnostic
        counts so callers can assert lineage cross-ref worked.

    Raises:
        ChainBreakError: HMAC verification of the per-run chain failed.
        FileNotFoundError: Per-run audit file is missing.
        ValueError: ``since`` is not strictly less than ``until``.
    """
    if since >= until:
        raise ValueError(f"since={since.isoformat()} must be < until={until.isoformat()}")

    workdir = (workdir or Path.cwd()).resolve()
    resolved_sdd = (sdd_dir or workdir / ".sdd").resolve()
    run_audit_path = resolved_sdd / RUN_AUDIT_DIR_NAME / f"{run_id}{RUN_AUDIT_FILE_SUFFIX}"

    if audit_key is None:
        # Lazy-import to avoid pulling the audit-key environment touch on
        # callers that only build deterministic bundles from in-memory keys.
        from bernstein.core.security.audit import load_or_create_audit_key

        audit_key = load_or_create_audit_key()

    chain_events = _verify_run_chain(run_audit_path, key=audit_key)
    window_events = _filter_events_by_window(chain_events, since=since, until=until)

    last_event_ts = window_events[-1].timestamp if window_events else since.isoformat()
    chain_anchor = str(window_events[-1].raw.get("hmac", _GENESIS_HMAC)) if window_events else _GENESIS_HMAC
    retention = compute_retention_pin(risk_class, last_event_ts)

    lineage_artefacts = _walk_lineage_records(
        resolved_sdd,
        run_id=run_id,
        since=since,
        until=until,
    )

    event_log = _build_event_log(window_events)
    data_catalog = _build_data_catalog_with_lineage(window_events, lineage_artefacts)
    clause_map_file = _resolve_clause_map_path(workdir, clause_map_path)
    clause_map = _load_clause_map_from_yaml(clause_map_file)

    since_iso = since.isoformat()
    until_iso = until.isoformat()
    artefact_hashes = {
        "events.jsonl": hashlib.sha256(event_log).hexdigest(),
        "data_catalog.json": hashlib.sha256(data_catalog).hexdigest(),
        "clause_map.json": hashlib.sha256(clause_map).hexdigest(),
    }
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": _bundle_id(since_iso, until_iso, risk_class),
        "since": since_iso,
        "until": until_iso,
        "run_id": run_id,
        "risk_class": risk_class,
        "event_count": len(window_events),
        "chain_anchor": chain_anchor,
        "retention": retention.to_dict(),
        "artefacts": dict(sorted(artefact_hashes.items())),
        "lineage_artefact_count": len(lineage_artefacts),
        "clause_map_source": str(clause_map_file.relative_to(workdir))
        if clause_map_file.is_relative_to(workdir)
        else str(clause_map_file),
    }
    manifest_bytes = _bundle_json(manifest)

    archive_bytes = _zip_artefacts(
        manifest_bytes=manifest_bytes,
        event_log=event_log,
        data_catalog=data_catalog,
        clause_map=clause_map,
    )
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    archive_path: Path | None = None
    if write:
        target_dir = output_dir or (resolved_sdd / "evidence")
        target_dir.mkdir(parents=True, exist_ok=True)
        archive_path = target_dir / f"article12_run_{run_id}_{manifest['bundle_id']}.zip"
        archive_path.write_bytes(archive_bytes)

    bundle = Article12Bundle(
        bundle_id=str(manifest["bundle_id"]),
        since=since_iso,
        until=until_iso,
        risk_class=risk_class,
        event_count=len(window_events),
        chain_anchor=chain_anchor,
        retention=retention,
        archive_path=archive_path,
        sha256=archive_sha256,
    )
    logger.info(
        "Article 12 run bundle assembled (run_id=%s, events=%d, lineage=%d)",
        run_id,
        len(window_events),
        len(lineage_artefacts),
    )
    return RunBundleResult(
        bundle=bundle,
        run_id=run_id,
        chain_event_count=len(chain_events),
        catalog_artefact_count=len(lineage_artefacts),
    )


# ---------------------------------------------------------------------------
# Per-run audit emit helper (used by demo + integration tests)
# ---------------------------------------------------------------------------


def emit_run_audit_event(
    *,
    sdd_dir: Path,
    run_id: str,
    event_type: str,
    actor: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    audit_key: bytes | None = None,
) -> dict[str, Any]:
    """Append one HMAC-chained event to a per-run audit slice.

    The orchestrator's :class:`AuditLog` writes calendar-rotated daily
    files under ``<sdd>/audit/``. This helper writes to the additional
    per-run slice at ``<sdd>/runtime/audit/<run_id>.audit.jsonl`` using
    the same key + payload format, so :func:`assemble_from_run` can read
    a chain anchored to a run rather than a wall-clock window.

    Args:
        sdd_dir: Project ``.sdd`` root.
        run_id: Run identifier (validated for path safety).
        event_type: Audit event type label.
        actor: Originating actor.
        resource_type: Affected resource type.
        resource_id: Affected resource identifier.
        details: Optional structured payload.
        audit_key: HMAC key bytes. Falls back to the operator's keychain.

    Returns:
        The written entry (post-HMAC), useful for tests.

    Raises:
        ValueError: When *run_id* contains a path separator.
    """
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        raise ValueError(f"unsafe run_id: {run_id!r}")

    if audit_key is None:
        from bernstein.core.security.audit import load_or_create_audit_key

        audit_key = load_or_create_audit_key()

    target_dir = sdd_dir / RUN_AUDIT_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{run_id}{RUN_AUDIT_FILE_SUFFIX}"

    prev = _GENESIS_HMAC
    if target.is_file():
        for line in reversed(target.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(last, dict) and "hmac" in last:
                prev = str(last["hmac"])
                break

    ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    entry: dict[str, Any] = {
        "timestamp": ts,
        "event_type": event_type,
        "actor": actor,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": details or {},
        "prev_hmac": prev,
    }
    payload = prev + json.dumps(entry, sort_keys=True)
    entry["hmac"] = _hmac.new(audit_key, payload.encode(), hashlib.sha256).hexdigest()

    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "DEFAULT_CLAUSE_MAP_PATH",
    "HIGH_RISK_RETENTION_YEARS",
    "MINIMUM_RETENTION_DAYS",
    "RUN_AUDIT_DIR_NAME",
    "RUN_AUDIT_FILE_SUFFIX",
    "Article12Bundle",
    "ChainBreakError",
    "RetentionPin",
    "RunBundleResult",
    "VerificationResult",
    "assemble_from_run",
    "build_article12_bundle",
    "compute_retention_pin",
    "emit_run_audit_event",
    "validate_retention",
    "verify_bundle",
]
