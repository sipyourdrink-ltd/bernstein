"""Operator-declared per-goal SLA contract store (#2549).

The fleet-wide SLO tracker (:mod:`bernstein.core.observability.slo`) aggregates
three hardcoded targets across the whole fleet, so one degraded goal hides
inside a green aggregate. This module is the per-goal counterpart: an operator
attaches a declarative contract to a single recurring goal, task family, or
spend envelope, and the supervisor evaluates it against chain evidence.

A contract is a content-addressed document. Its canonical JSON is hashed into a
``contract_hash``; equal contract bodies land on the identical hash on any
machine (a verifier holding the same body recomputes it), and changing any
semantic field changes the hash. The ``id`` is derived from the hash so
reapplying ``sla add`` from configuration is idempotent.

Persistence mirrors :mod:`bernstein.core.planning.schedule_store`: one JSON file
per contract under ``<sdd_dir>/runtime/sla/<id>.json``, written atomically.

Axes (each declared only when its threshold is non-zero, so a contract carries
exactly the promises the operator made):

- ``max_run_duration_s``: a run of the subject must finish within N seconds.
- ``start_lateness_s``: the goal must start within N seconds of its fire instant.
- ``fire_frequency_s``: the goal must fire at least once every N seconds.
- ``artifact_freshness_s`` + ``artifact_path``: the artifact this goal maintains
  must have been re-derived within N seconds (checked purely from lineage-spine
  entries, offline, with no filesystem access to the artifact itself).
- ``spend_rate_usd_per_hour``: the subject's spend rate must stay under N.

Each contract carries its own error-budget policy (``budget_events`` plus
burn-rate escalation tiers), generalising the hardcoded fleet targets into
operator-defined policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Contract id format: 12 hex chars from the sha256 of the canonical body.
_SLA_ID_PREFIX = "sla_"
_SLA_ID_HEX_LEN = 12

#: Maximum length of a stored subject id / artifact path (defensive JSON cap).
_MAX_SUBJECT_LEN = 256
_MAX_ARTIFACT_LEN = 1024

#: Subject kinds a contract can bind to.
SUBJECT_SCHEDULE = "schedule"
SUBJECT_TASK_FAMILY = "task_family"
SUBJECT_ENVELOPE = "envelope"
_SUBJECT_TYPES = frozenset({SUBJECT_SCHEDULE, SUBJECT_TASK_FAMILY, SUBJECT_ENVELOPE})

SubjectType = Literal["schedule", "task_family", "envelope"]

#: The declarable axis names, in canonical order (used for stable rendering).
AXIS_DURATION = "max_run_duration"
AXIS_LATENESS = "start_lateness"
AXIS_FREQUENCY = "fire_frequency"
AXIS_FRESHNESS = "artifact_freshness"
AXIS_SPEND_RATE = "spend_rate"

_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,256}$")

#: A contract id is derived from the body digest, so the only well-formed shape
#: is the prefix plus lowercase hex. Every id that addresses a file on disk is
#: matched against this before it is joined onto the store directory.
_CONTRACT_ID_RE = re.compile(rf"^{re.escape(_SLA_ID_PREFIX)}[0-9a-f]{{{_SLA_ID_HEX_LEN}}}$")


class SLAContractError(ValueError):
    """Raised when a contract body fails validation."""


class SLAContractIdError(SLAContractError):
    """Raised when a contract id is not a well-formed derived id.

    Subclasses :class:`SLAContractError` so a caller that already handles
    contract validation failures keeps handling this one.
    """


def _single_line(value: object, *, limit: int = 256) -> str:
    """Return *value* as a single-line, control-character-free log token.

    Contract ids and store paths reach log records, and a log record is
    newline-delimited both for an operator reading the file and for any shipper
    parsing it. A value carrying CR or LF could therefore append a forged
    record after the real one. Line breaks are escaped first, every remaining
    non-printable character is hex-escaped, and the result is length-capped, so
    an untrusted value always occupies exactly one line of bounded width.
    """
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    text = "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in text)
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text


@dataclass(frozen=True)
class BurnTier:
    """One burn-rate escalation tier of a contract's error-budget policy.

    Attributes:
        name: Operator-facing tier label (e.g. ``"warn"``, ``"page"``).
        burn_rate: The burn-rate threshold at or above which this tier is
            selected. ``burn_rate`` is the multiple of the error budget the
            observed failures represent (``1.0`` = exactly the budget).
        action: The remediation-vocabulary hint recorded when this tier fires.
            One of the :class:`bernstein.core.observability.slo.ErrorBudgetAction`
            values.
    """

    name: str
    burn_rate: float
    action: str = "increase_review"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "burn_rate": self.burn_rate, "action": self.action}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BurnTier:
        return cls(
            name=str(raw.get("name", "")),
            burn_rate=float(raw.get("burn_rate", 0.0)),
            action=str(raw.get("action", "increase_review")),
        )


def _default_burn_tiers() -> tuple[BurnTier, ...]:
    """Return the default escalation ladder shared by contracts with none set.

    Mirrors the fleet SLO burndown status thresholds (yellow at >1.5x, red at
    >3x) so the per-goal ladder reads consistently with ``bernstein slo``.
    """
    return (
        BurnTier(name="warn", burn_rate=1.5, action="increase_review"),
        BurnTier(name="page", burn_rate=3.0, action="reduce_agents"),
    )


@dataclass(frozen=True)
class SLAContract:
    """An operator-declared per-goal SLA contract.

    Only the fields with non-zero thresholds are evaluated, so a contract
    declares exactly the axes the operator promised. ``id`` and ``created_at``
    are bookkeeping and are excluded from the content hash; every other field is
    semantic and folds into ``contract_hash``.
    """

    id: str
    subject_type: str
    subject_id: str
    max_run_duration_s: int = 0
    start_lateness_s: int = 0
    fire_frequency_s: int = 0
    artifact_freshness_s: int = 0
    artifact_path: str = ""
    spend_rate_usd_per_hour: float = 0.0
    budget_events: int = 3
    burn_tiers: tuple[BurnTier, ...] = field(default_factory=_default_burn_tiers)
    remediation_cost_usd: float = 0.0
    created_at: float = 0.0

    # -- content addressing -------------------------------------------------

    def canonical_body(self) -> dict[str, Any]:
        """Return the semantic body the ``contract_hash`` is taken over.

        Excludes ``id`` (derived from the hash) and ``created_at``
        (bookkeeping). Every other field is included, so changing any semantic
        field changes the hash.
        """
        return {
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "max_run_duration_s": self.max_run_duration_s,
            "start_lateness_s": self.start_lateness_s,
            "fire_frequency_s": self.fire_frequency_s,
            "artifact_freshness_s": self.artifact_freshness_s,
            "artifact_path": self.artifact_path,
            "spend_rate_usd_per_hour": self.spend_rate_usd_per_hour,
            "budget_events": self.budget_events,
            "burn_tiers": [t.to_dict() for t in self.burn_tiers],
            "remediation_cost_usd": self.remediation_cost_usd,
        }

    def canonical_bytes(self) -> bytes:
        """Return the canonical UTF-8 bytes the ``contract_hash`` is hashed from."""
        return json.dumps(self.canonical_body(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @property
    def contract_hash(self) -> str:
        """Return the sha256 hex digest of the canonical body."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def declared_axes(self) -> tuple[str, ...]:
        """Return the axis names this contract actually declares (non-zero)."""
        axes: list[str] = []
        if self.max_run_duration_s > 0:
            axes.append(AXIS_DURATION)
        if self.start_lateness_s > 0:
            axes.append(AXIS_LATENESS)
        if self.fire_frequency_s > 0:
            axes.append(AXIS_FREQUENCY)
        if self.artifact_freshness_s > 0:
            axes.append(AXIS_FRESHNESS)
        if self.spend_rate_usd_per_hour > 0:
            axes.append(AXIS_SPEND_RATE)
        return tuple(axes)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view including id, hash, and bookkeeping."""
        body = self.canonical_body()
        body["id"] = self.id
        body["contract_hash"] = self.contract_hash
        body["created_at"] = self.created_at
        return body


def compute_contract_id(body: dict[str, Any]) -> tuple[str, str]:
    """Return ``(contract_id, contract_hash)`` for a canonical contract body.

    Public so the store and a stand-alone verifier derive the id identically.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{_SLA_ID_PREFIX}{digest[:_SLA_ID_HEX_LEN]}", digest


def build_contract(
    *,
    subject_type: str,
    subject_id: str,
    max_run_duration_s: int = 0,
    start_lateness_s: int = 0,
    fire_frequency_s: int = 0,
    artifact_freshness_s: int = 0,
    artifact_path: str = "",
    spend_rate_usd_per_hour: float = 0.0,
    budget_events: int = 3,
    burn_tiers: tuple[BurnTier, ...] | None = None,
    remediation_cost_usd: float = 0.0,
    created_at: float = 0.0,
) -> SLAContract:
    """Validate and build an :class:`SLAContract` (id derived from its body).

    Raises:
        SLAContractError: On an unknown subject type, an empty / oversized
            subject, a freshness axis without an artifact path, or no declared
            axis at all.
    """
    if subject_type not in _SUBJECT_TYPES:
        raise SLAContractError(f"unknown subject_type {subject_type!r}; must be one of {sorted(_SUBJECT_TYPES)}")
    subject_id = subject_id.strip()
    if not subject_id:
        raise SLAContractError("subject_id must not be empty")
    if len(subject_id) > _MAX_SUBJECT_LEN or not _ID_RE.match(subject_id):
        raise SLAContractError(f"subject_id {subject_id!r} is invalid or too long")
    artifact_path = artifact_path.strip()
    if len(artifact_path) > _MAX_ARTIFACT_LEN:
        raise SLAContractError("artifact_path is too long")
    if artifact_freshness_s > 0 and not artifact_path:
        raise SLAContractError("artifact_freshness_s requires artifact_path")
    if budget_events < 0:
        raise SLAContractError("budget_events must be >= 0")
    for value, name in (
        (max_run_duration_s, "max_run_duration_s"),
        (start_lateness_s, "start_lateness_s"),
        (fire_frequency_s, "fire_frequency_s"),
        (artifact_freshness_s, "artifact_freshness_s"),
    ):
        if value < 0:
            raise SLAContractError(f"{name} must be >= 0")
    if spend_rate_usd_per_hour < 0 or remediation_cost_usd < 0:
        raise SLAContractError("cost fields must be >= 0")

    tiers = tuple(burn_tiers) if burn_tiers is not None else _default_burn_tiers()
    contract = SLAContract(
        id="",
        subject_type=subject_type,
        subject_id=subject_id,
        max_run_duration_s=int(max_run_duration_s),
        start_lateness_s=int(start_lateness_s),
        fire_frequency_s=int(fire_frequency_s),
        artifact_freshness_s=int(artifact_freshness_s),
        artifact_path=artifact_path,
        spend_rate_usd_per_hour=float(spend_rate_usd_per_hour),
        budget_events=int(budget_events),
        burn_tiers=tiers,
        remediation_cost_usd=float(remediation_cost_usd),
        created_at=created_at,
    )
    if not contract.declared_axes():
        raise SLAContractError("contract must declare at least one axis with a non-zero threshold")
    contract_id, _ = compute_contract_id(contract.canonical_body())
    # Rebuild with the derived id (dataclass is frozen).
    return SLAContract(
        id=contract_id,
        subject_type=contract.subject_type,
        subject_id=contract.subject_id,
        max_run_duration_s=contract.max_run_duration_s,
        start_lateness_s=contract.start_lateness_s,
        fire_frequency_s=contract.fire_frequency_s,
        artifact_freshness_s=contract.artifact_freshness_s,
        artifact_path=contract.artifact_path,
        spend_rate_usd_per_hour=contract.spend_rate_usd_per_hour,
        budget_events=contract.budget_events,
        burn_tiers=contract.burn_tiers,
        remediation_cost_usd=contract.remediation_cost_usd,
        created_at=contract.created_at,
    )


def contract_from_dict(raw: dict[str, Any]) -> SLAContract:
    """Rebuild an :class:`SLAContract` from its persisted / embedded dict view.

    Derives the id and hash from the body, so a tampered ``id`` or
    ``contract_hash`` field cannot survive a round-trip (the caller can compare
    the rebuilt hash against the stored one to detect it).
    """
    tiers_raw: Any = raw.get("burn_tiers", [])
    tiers = (
        tuple(
            BurnTier.from_dict(cast("dict[str, Any]", t)) for t in cast("list[Any]", tiers_raw) if isinstance(t, dict)
        )
        if isinstance(tiers_raw, list)
        else _default_burn_tiers()
    )
    return build_contract(
        subject_type=str(raw.get("subject_type", "")),
        subject_id=str(raw.get("subject_id", "")),
        max_run_duration_s=int(raw.get("max_run_duration_s", 0)),
        start_lateness_s=int(raw.get("start_lateness_s", 0)),
        fire_frequency_s=int(raw.get("fire_frequency_s", 0)),
        artifact_freshness_s=int(raw.get("artifact_freshness_s", 0)),
        artifact_path=str(raw.get("artifact_path", "")),
        spend_rate_usd_per_hour=float(raw.get("spend_rate_usd_per_hour", 0.0)),
        budget_events=int(raw.get("budget_events", 3)),
        burn_tiers=tiers,
        remediation_cost_usd=float(raw.get("remediation_cost_usd", 0.0)),
        created_at=float(raw.get("created_at", 0.0)),
    )


class SLAStore:
    """File-backed store for operator-declared SLA contracts.

    One JSON file per contract under ``<sdd_dir>/runtime/sla/``. ``add`` is
    idempotent: re-adding an identical contract body returns the existing
    contract unchanged (equal bodies share the derived id).
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._dir = sdd_dir / "runtime" / "sla"
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def sdd_dir(self) -> Path:
        return self._sdd_dir

    def _path_for(self, contract_id: str) -> Path:
        """Return the store path for ``contract_id``, refusing anything else.

        Contract ids arrive from operator input and from request paths, and
        both ``get`` and ``remove`` turn one into a filesystem operation, so an
        unchecked id would read or unlink a file anywhere the process can
        reach. Two independent checks stand between the id and the filesystem:
        the id must match the derived-id shape (which admits no separator, dot
        segment, or control character), and the resolved path must still be
        inside the resolved store directory.

        Raises:
            SLAContractIdError: If the id is not a well-formed derived id, or
                if it resolves outside the store directory.
        """
        if not _CONTRACT_ID_RE.match(contract_id):
            raise SLAContractIdError(f"invalid SLA contract id '{_single_line(contract_id)}'")
        base = self._dir.resolve()
        path = (base / f"{contract_id}.json").resolve()
        if not path.is_relative_to(base):
            raise SLAContractIdError(f"SLA contract id '{_single_line(contract_id)}' escapes the contract store")
        return path

    def add(self, contract: SLAContract, *, now: float | None = None) -> SLAContract:
        """Persist a contract; idempotent by derived id."""
        existing = self.get(contract.id)
        if existing is not None:
            return existing
        created = contract.created_at or (float(now) if now is not None else time.time())
        stored = contract_from_dict({**contract.canonical_body(), "created_at": created})
        self._write(stored)
        logger.info("Registered SLA contract %s (subject=%s:%s)", stored.id, stored.subject_type, stored.subject_id)
        return stored

    def get(self, contract_id: str) -> SLAContract | None:
        path = self._path_for(contract_id)
        if not path.exists():
            return None
        return _load_contract(path)

    def list(self) -> list[SLAContract]:
        out: list[SLAContract] = []
        for path in sorted(self._dir.glob("*.json")):
            contract = _load_contract(path)
            if contract is not None:
                out.append(contract)
        out.sort(key=lambda c: c.id)
        return out

    def for_subject(self, subject_type: str, subject_id: str) -> list[SLAContract]:
        """Return every contract bound to ``(subject_type, subject_id)``."""
        return [c for c in self.list() if c.subject_type == subject_type and c.subject_id == subject_id]

    def remove(self, contract_id: str) -> bool:
        path = self._path_for(contract_id)
        if not path.exists():
            return False
        path.unlink()
        logger.info("Removed SLA contract %s", contract_id)
        return True

    def _write(self, contract: SLAContract) -> None:
        path = self._path_for(contract.id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = contract.to_dict()
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2))
        tmp.replace(path)


def _load_contract(path: Path) -> SLAContract | None:
    try:
        raw: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load SLA contract %s: %s", _single_line(path), _single_line(exc))
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    try:
        return contract_from_dict(data)
    except SLAContractError as exc:
        logger.warning("Malformed SLA contract %s: %s", _single_line(path), _single_line(exc))
        return None


# Re-export the frozen asdict helper name so callers can round-trip if needed.
def contract_to_json(contract: SLAContract) -> dict[str, Any]:
    """Return the full JSON view (alias of :meth:`SLAContract.to_dict`)."""
    return contract.to_dict()


__all__ = [
    "AXIS_DURATION",
    "AXIS_FREQUENCY",
    "AXIS_FRESHNESS",
    "AXIS_LATENESS",
    "AXIS_SPEND_RATE",
    "SUBJECT_ENVELOPE",
    "SUBJECT_SCHEDULE",
    "SUBJECT_TASK_FAMILY",
    "BurnTier",
    "SLAContract",
    "SLAContractError",
    "SLAContractIdError",
    "SLAStore",
    "build_contract",
    "compute_contract_id",
    "contract_from_dict",
    "contract_to_json",
]
