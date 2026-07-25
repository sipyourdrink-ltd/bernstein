"""Per-artifact health as a deterministic projection (issue #2559, Phase 3).

Answering "is this output good and current" used to mean correlating four
surfaces by hand: open-tip analysis, evidence-bundle verification, spine chain
verification, and the cadence the output was supposed to refresh on. Nothing
rolled them up per artifact, so the answer lived in an operator's head and was
never the same twice.

This module is that roll-up, and it is a **pure function**. Everything that can
vary between two observers is an explicit argument:

* the state is read once by :func:`collect_artifact_state` and then never
  touched again;
* the evaluation instant is the caller-supplied ``at``, never the wall clock;
* the declared cadence is a caller-supplied number, never an ambient default.

That is what makes the CLI and the server route incapable of disagreeing: they
are not two implementations that happen to match, they are two callers of
:func:`artifact_health_json`, which is the only place the verdict is serialised.
Given the same ``.sdd`` state and the same ``at``, both emit byte-identical
bytes, and a third party can recompute either offline with no network.

Verdict
-------

``green``
    Every applicable leg passes: the artifact was produced, every spine entry
    carrying it verifies, exactly one set of bytes is current, the newest
    evidence bundle that references it verifies, and the last production is
    inside the declared cadence.

``amber``
    Nothing is broken but the artifact is out of date -- the cadence lapsed.

``red``
    An integrity or currency failure: a tampered or unlinked chain entry, or
    two different sets of bytes both claiming to be current.

A leg with nothing to say reports ``not_applicable`` and cannot hold a verdict
down: an artifact with no declared cadence is not "failing cadence", and one no
evidence bundle references is not "failing evidence". Absence of a signal is
never reported as a negative signal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.artifact_uri import ArtifactURIError, canonical_artifact_key
from bernstein.core.lineage.spine import LineageSpine, SpineStatus, verify_entry

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "AMBER",
    "GREEN",
    "LEG_FAIL",
    "LEG_NOT_APPLICABLE",
    "LEG_PASS",
    "LEG_STALE",
    "RED",
    "ArtifactHealth",
    "ArtifactProduction",
    "ArtifactState",
    "HealthLeg",
    "artifact_health_json",
    "artifact_log",
    "artifact_log_json",
    "collect_artifact_state",
    "compute_artifact_health",
    "list_artifact_keys",
]

GREEN = "green"
AMBER = "amber"
RED = "red"

LEG_PASS = "pass"
LEG_FAIL = "fail"
LEG_STALE = "stale"
LEG_NOT_APPLICABLE = "not_applicable"

#: Version stamped into the serialised verdict. A consumer pinning a verdict
#: document knows which projection produced it.
HEALTH_SCHEMA_VERSION = 1

_SPINE_LOG_NAME = "spine.jsonl"
_BUNDLES_SUBPATH = (".sdd", "evidence", "bundles")


@dataclass(frozen=True, slots=True)
class ArtifactProduction:
    """One recorded production of an artifact, as the chain saw it.

    This is the attribution record ``artifact log`` renders: which agent
    identity, running which model, produced these bytes, and whether the spine
    entry saying so still verifies.
    """

    run_id: str
    entry_hash: str
    content_hash: str
    actor: str
    model: str
    step_id: str
    timestamp: int
    verified: bool

    @property
    def order_key(self) -> tuple[int, str]:
        """Total order over productions: newest timestamp, then entry hash.

        The entry hash breaks ties so the ordering is total and identical on
        every host -- two verifiers never disagree about which production is the
        tip.
        """
        return (self.timestamp, self.entry_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "content_hash": self.content_hash,
            "entry_hash": self.entry_hash,
            "model": self.model,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "timestamp": self.timestamp,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class ArtifactState:
    """Everything read off disk for one artifact key, frozen before use.

    Collected once and then treated as immutable input, so the verdict is a
    function of a snapshot rather than of whatever the filesystem happened to
    hold at each individual read.
    """

    uri: str
    productions: tuple[ArtifactProduction, ...] = ()
    chain_errors: tuple[str, ...] = ()
    evidence_task_id: str = ""
    evidence_verified: bool | None = None
    evidence_detail: str = ""

    @property
    def ordered(self) -> tuple[ArtifactProduction, ...]:
        """Productions oldest-first in the total order."""
        return tuple(sorted(self.productions, key=lambda p: p.order_key))


@dataclass(frozen=True, slots=True)
class HealthLeg:
    """One named check inside the verdict."""

    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(frozen=True, slots=True)
class ArtifactHealth:
    """The rolled-up verdict for one artifact."""

    uri: str
    verdict: str
    legs: tuple[HealthLeg, ...]
    evaluated_at: int
    tip_entry_hash: str = ""
    tip_content_hash: str = ""
    tip_actor: str = ""
    tip_model: str = ""
    tip_run_id: str = ""
    last_produced_at: int | None = None
    production_count: int = 0
    schema_version: int = HEALTH_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical verdict document."""
        return {
            "evaluated_at": self.evaluated_at,
            "last_produced_at": self.last_produced_at,
            "legs": [leg.to_dict() for leg in self.legs],
            "production_count": self.production_count,
            "schema_version": self.schema_version,
            "tip": {
                "actor": self.tip_actor,
                "content_hash": self.tip_content_hash,
                "entry_hash": self.tip_entry_hash,
                "model": self.tip_model,
                "run_id": self.tip_run_id,
            },
            "uri": self.uri,
            "verdict": self.verdict,
        }


def _canonical_json(payload: dict[str, Any]) -> str:
    """Serialise a verdict document deterministically.

    Sorted keys and minimal separators: this single function is why two
    surfaces cannot produce different bytes for the same verdict.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# Reading state
# ---------------------------------------------------------------------------


def _run_ids(lineage_root: Path) -> list[str]:
    """Return run ids under ``lineage_root`` that carry a spine, sorted."""
    if not lineage_root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(lineage_root.iterdir()):
        if child.is_dir() and (child / _SPINE_LOG_NAME).is_file():
            out.append(child.name)
    return out


def list_artifact_keys(workdir: Path) -> dict[str, int]:
    """Return every artifact key the local spines carry, with production counts.

    Backs ``bernstein artifact list``. Keys are returned exactly as the chain
    records them; no canonicalisation is applied on read, because a key that a
    historical entry was hashed under must be reported as it was written.
    """
    lineage_root = workdir / ".sdd" / "lineage"
    counts: dict[str, int] = {}
    for run_id in _run_ids(lineage_root):
        try:
            spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=b"")
        except ValueError:
            logger.debug("artifact health: skipping unusable run dir %r", run_id)
            continue
        for entry in spine.iter_entries():
            counts[entry.artifact_path] = counts.get(entry.artifact_path, 0) + 1
    return counts


def _collect_evidence(
    workdir: Path,
    uri: str,
    *,
    hmac_key: bytes,
) -> tuple[str, bool | None, str]:
    """Return ``(task_id, verified, detail)`` for the newest bundle citing ``uri``.

    A bundle is considered to reference the artifact when its sealed output diff
    lists the key as declared-and-produced. That link is inside the bundle's
    *signed* binding, so an artifact cannot be associated with a bundle it was
    never sealed against.

    Returns ``("", None, "")`` when no bundle references the artifact, which the
    verdict reads as "not applicable" rather than as a failure.
    """
    from bernstein.core.evidence.bundle import EvidenceBundle, verify_evidence_bundle

    bundles_dir = workdir.joinpath(*_BUNDLES_SUBPATH)
    if not bundles_dir.is_dir():
        return "", None, ""

    newest_task = ""
    newest_ts = -1
    for path in sorted(bundles_dir.glob("*.json")):
        try:
            bundle = EvidenceBundle.from_bytes(path.read_bytes())
        except (OSError, ValueError, KeyError, TypeError):
            logger.debug("artifact health: malformed evidence bundle at %s", path)
            continue
        diff = bundle.output_diff
        if diff is None or uri not in diff.declared_and_produced:
            continue
        # Ties break on task id so the choice of "newest" is deterministic.
        if (bundle.timestamp, bundle.task_id) > (newest_ts, newest_task):
            newest_ts = bundle.timestamp
            newest_task = bundle.task_id

    if not newest_task:
        return "", None, ""

    try:
        result = verify_evidence_bundle(
            workdir=workdir,
            lineage_root=workdir / ".sdd" / "lineage",
            hmac_key=hmac_key,
            task_id=newest_task,
        )
    except Exception as exc:  # fail-open: an unverifiable bundle is a finding, not a crash
        logger.debug("artifact health: evidence verification raised for %s: %s", newest_task, exc)
        return newest_task, False, "evidence verification raised"
    return newest_task, bool(result.ok), "" if result.ok else str(getattr(result, "reason", "") or "bundle failed")


def collect_artifact_state(
    workdir: Path,
    uri: str,
    *,
    hmac_key: bytes,
    include_evidence: bool = True,
) -> ArtifactState:
    """Read every local fact about ``uri`` into one immutable snapshot.

    Walks each run spine under ``<workdir>/.sdd/lineage``, keeps the entries
    whose key equals ``uri``, recomputes each one's integrity verdict, and
    records a chain error for any run that carries the artifact but whose chain
    does not verify. A tampered entry belonging to a *different* artifact in the
    same run is deliberately not attributed here -- per-entry verification is
    what keeps one bad row from turning every artifact in the run red.

    Args:
        workdir: Project root containing ``.sdd``.
        uri: The artifact key, canonicalised before lookup.
        hmac_key: Audit-chain HMAC key the spines were tagged with.
        include_evidence: Read sealed evidence bundles. Off for callers that
            only need production attribution.

    Returns:
        The snapshot the verdict is computed from.
    """
    try:
        key = canonical_artifact_key(uri)
    except ArtifactURIError:
        # An unparseable key can still be looked up verbatim: the answer is
        # simply that nothing was ever produced under it.
        key = uri

    lineage_root = workdir / ".sdd" / "lineage"
    productions: list[ArtifactProduction] = []
    chain_errors: list[str] = []

    for run_id in _run_ids(lineage_root):
        try:
            spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
        except ValueError:
            logger.debug("artifact health: skipping unusable run dir %r", run_id)
            continue
        matched = [e for e in spine.iter_entries() if e.artifact_path == key]
        if not matched:
            continue
        for entry in matched:
            productions.append(
                ArtifactProduction(
                    run_id=run_id,
                    entry_hash=entry.entry_hash,
                    content_hash=entry.content_hash,
                    actor=entry.actor,
                    model=entry.model,
                    step_id=entry.step_id,
                    timestamp=entry.timestamp,
                    verified=verify_entry(entry, hmac_key),
                )
            )
        result = spine.verify()
        if result.status is SpineStatus.TAMPERED:
            chain_errors.append(f"run {run_id}: chain verification failed ({len(result.errors)} error(s))")

    evidence_task, evidence_verified, evidence_detail = (
        _collect_evidence(workdir, key, hmac_key=hmac_key) if include_evidence else ("", None, "")
    )

    return ArtifactState(
        uri=key,
        productions=tuple(productions),
        chain_errors=tuple(chain_errors),
        evidence_task_id=evidence_task,
        evidence_verified=evidence_verified,
        evidence_detail=evidence_detail,
    )


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def _tip_legs(state: ArtifactState) -> tuple[list[HealthLeg], ArtifactProduction | None]:
    """Return the production-, integrity- and tip-related legs plus the tip."""
    ordered = state.ordered
    if not ordered:
        return [
            HealthLeg(
                name="produced",
                status=LEG_FAIL,
                detail="no spine entry records this artifact; nothing has ever produced it",
            )
        ], None

    legs: list[HealthLeg] = [
        HealthLeg(name="produced", status=LEG_PASS, detail=f"{len(ordered)} production(s) recorded")
    ]

    unverified = [p.entry_hash for p in ordered if not p.verified]
    if unverified or state.chain_errors:
        detail_parts: list[str] = []
        if unverified:
            detail_parts.append("tampered entry: " + ", ".join(sorted(unverified)))
        detail_parts.extend(state.chain_errors)
        legs.append(HealthLeg(name="chain_integrity", status=LEG_FAIL, detail="; ".join(detail_parts)))
    else:
        legs.append(
            HealthLeg(
                name="chain_integrity",
                status=LEG_PASS,
                detail="every entry hash and HMAC tag recomputes",
            )
        )

    tip = ordered[-1]
    contenders = [p for p in ordered if p.timestamp == tip.timestamp]
    distinct = sorted({p.content_hash for p in contenders})
    if len(distinct) > 1:
        legs.append(
            HealthLeg(
                name="single_open_tip",
                status=LEG_FAIL,
                detail=(
                    f"{len(distinct)} distinct content hashes claim to be current at "
                    f"timestamp {tip.timestamp}: " + ", ".join(distinct)
                ),
            )
        )
    else:
        legs.append(HealthLeg(name="single_open_tip", status=LEG_PASS, detail=f"tip {tip.entry_hash}"))

    return legs, tip


def _evidence_leg(state: ArtifactState) -> HealthLeg:
    if state.evidence_verified is None:
        return HealthLeg(
            name="evidence",
            status=LEG_NOT_APPLICABLE,
            detail="no sealed evidence bundle declares this artifact",
        )
    if state.evidence_verified:
        return HealthLeg(name="evidence", status=LEG_PASS, detail=f"bundle for task {state.evidence_task_id} verifies")
    return HealthLeg(
        name="evidence",
        status=LEG_FAIL,
        detail=f"bundle for task {state.evidence_task_id} failed verification: {state.evidence_detail}",
    )


def _cadence_leg(tip: ArtifactProduction | None, *, at: int, cadence_seconds: int | None) -> HealthLeg:
    if cadence_seconds is None or cadence_seconds <= 0:
        return HealthLeg(name="cadence", status=LEG_NOT_APPLICABLE, detail="no cadence declared for this artifact")
    if tip is None:
        return HealthLeg(name="cadence", status=LEG_NOT_APPLICABLE, detail="never produced")
    age = at - tip.timestamp
    if age <= cadence_seconds:
        return HealthLeg(
            name="cadence", status=LEG_PASS, detail=f"last produced {age}s ago, cadence {cadence_seconds}s"
        )
    return HealthLeg(
        name="cadence",
        status=LEG_STALE,
        detail=f"last produced {age}s ago, past the declared cadence of {cadence_seconds}s",
    )


def compute_artifact_health(
    state: ArtifactState,
    *,
    at: int,
    cadence_seconds: int | None = None,
) -> ArtifactHealth:
    """Roll a collected state up into a verdict. Pure.

    Args:
        state: The snapshot from :func:`collect_artifact_state`.
        at: The evaluation instant, in the same unit the spine timestamps use.
            Explicit rather than read from the clock: a verdict that embedded
            "now" could never be byte-identical across two surfaces, which is
            the whole point of the projection.
        cadence_seconds: The declared refresh cadence, or ``None`` when the
            artifact declares none.

    Returns:
        The verdict. Depends only on its arguments.
    """
    legs, tip = _tip_legs(state)
    legs.append(_evidence_leg(state))
    legs.append(_cadence_leg(tip, at=at, cadence_seconds=cadence_seconds))

    statuses = {leg.status for leg in legs}
    if LEG_FAIL in statuses:
        verdict = RED
    elif LEG_STALE in statuses:
        verdict = AMBER
    else:
        verdict = GREEN

    return ArtifactHealth(
        uri=state.uri,
        verdict=verdict,
        legs=tuple(legs),
        evaluated_at=at,
        tip_entry_hash=tip.entry_hash if tip else "",
        tip_content_hash=tip.content_hash if tip else "",
        tip_actor=tip.actor if tip else "",
        tip_model=tip.model if tip else "",
        tip_run_id=tip.run_id if tip else "",
        last_produced_at=tip.timestamp if tip else None,
        production_count=len(state.productions),
    )


def artifact_health_json(
    workdir: Path,
    uri: str,
    *,
    hmac_key: bytes,
    at: int,
    cadence_seconds: int | None = None,
) -> str:
    """Return the canonical verdict JSON for ``uri``.

    **The** entry point. ``bernstein artifact health --json`` and the server
    route both call exactly this, so "the CLI and the dashboard cannot disagree"
    is a structural property rather than a pair of implementations kept in sync
    by discipline.
    """
    state = collect_artifact_state(workdir, uri, hmac_key=hmac_key)
    return _canonical_json(compute_artifact_health(state, at=at, cadence_seconds=cadence_seconds).to_dict())


# ---------------------------------------------------------------------------
# Attribution log
# ---------------------------------------------------------------------------


def artifact_log(
    workdir: Path,
    uri: str,
    *,
    hmac_key: bytes,
    limit: int = 0,
) -> tuple[ArtifactProduction, ...]:
    """Return productions of ``uri`` newest-first: who produced the current tip.

    Backs ``bernstein artifact log <uri>``. Each record carries the producing
    agent identity, the model it ran, the step it came from and the spine entry
    hash that proves it, plus whether that entry still verifies -- so the answer
    to "which agent identity, running which model, produced the current tip of
    this PR or this package" is read off the chain rather than reconstructed by
    walking run directories.

    Args:
        workdir: Project root containing ``.sdd``.
        uri: The artifact key.
        hmac_key: Audit-chain HMAC key.
        limit: Maximum records to return; ``0`` means all.
    """
    state = collect_artifact_state(workdir, uri, hmac_key=hmac_key, include_evidence=False)
    records = tuple(reversed(state.ordered))
    return records[:limit] if limit > 0 else records


def artifact_log_json(records: Iterable[ArtifactProduction], *, uri: str) -> str:
    """Serialise an attribution log canonically, for the CLI and the route."""
    return _canonical_json({"productions": [r.to_dict() for r in records], "uri": uri})
