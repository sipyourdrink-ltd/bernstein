"""Non-coding modality activities on the typed boundary (issue #2311).

The typed activity boundary in :mod:`bernstein.core.orchestration.activity` is
modality-agnostic on purpose, but it is only worth anything if a *non-coding*
agent modality runs under it end to end. This module ships two such modalities
as the proof, both producing an :class:`~bernstein.core.orchestration.activity.ActivityResult`
the deterministic scheduler dispatches and journals identically to a coding
spawn:

* :class:`ResearchActivity` -- a research agent that content-addresses every
  fetched page *at fetch time* (AC1). The bytes land in a content-addressed
  :class:`ContentStore`; the per-page content hash is the evidence, so a replay
  reattaches byte-identical pages from the store and refuses on any tamper.
* :class:`BrowserActivity` -- a browser / computer-use agent that records an
  observation hash per decision step (AC5): the screenshot / DOM snapshot the
  model saw before it acted. A replay reattaches the per-decision snapshots and
  compares their hashes.

Both modalities are thin: they gather content-addressed observations and hand a
built ``ActivityResult`` to :func:`~bernstein.core.orchestration.activity.dispatch_activity`.
The store is the shared substrate that makes the replay guarantee hold -- the
journal pins the content hashes, the store holds the bytes, and
:func:`replay_reattach` rejoins the two and re-verifies every hash.

Deferred modalities
-------------------
The **data** and **ops** modalities described in the epic (split
deterministic-plan vs side-effecting activity, signed input/output artifacts)
build on this same substrate: their side-effecting steps are
:class:`~bernstein.core.orchestration.activity.Observation` s over signed input
and output artifacts, dispatched through the identical boundary. They are
documented follow-ups so the core substrate plus a research and browser proof
land wired and tested first, rather than shipping four half-wired modalities.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.orchestration.activity import (
    ACTIVITY_RESULT_EVENT,
    ActivityKind,
    ActivityResult,
    Observation,
    TerminalState,
    evidence_set_hash,
)
from bernstein.core.replay.journal import load_events, verify_journal

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ActivityVerifyResult",
    "BrowserActivity",
    "ContentStore",
    "ResearchActivity",
    "StageVerdict",
    "replay_reattach",
    "verify_run_activities",
]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ContentStore:
    """A content-addressed byte store keyed by ``sha256:`` content hash.

    Fetched pages and per-decision snapshots land here at capture time. The key
    is the content hash, so the same bytes are stored once regardless of where
    they came from, and a replay retrieves the exact bytes by hash. The store is
    a directory of ``<hex>`` files under *root*; it never overwrites a key
    through :meth:`put` (identical bytes hash to the same key, so a re-put is a
    no-op), which is what makes the content-addressing guarantee hold.

    Args:
        root: Directory the content-addressed blobs live under.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, content_hash: str) -> Path:
        digest = content_hash.split(":", 1)[-1]
        return self._root / digest

    def put(self, content: bytes) -> str:
        """Store *content* and return its ``sha256:`` content hash.

        Idempotent: storing the same bytes twice returns the same key and does
        not rewrite the blob.
        """
        content_hash = "sha256:" + _sha256_hex(content)
        path = self._path_for(content_hash)
        if not path.exists():
            path.write_bytes(content)
        return content_hash

    def get(self, content_hash: str) -> bytes:
        """Return the bytes stored under *content_hash*.

        Raises:
            KeyError: When no blob is stored under the hash.
        """
        path = self._path_for(content_hash)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise KeyError(content_hash) from exc

    def force_put(self, content_hash: str, content: bytes) -> None:
        """Write *content* under an arbitrary *content_hash*, bypassing addressing.

        Test / adversarial hook only: a real caller never separates the bytes
        from their hash. Used to prove :func:`replay_reattach` detects a store
        whose bytes no longer match the pinned hash.
        """
        self._path_for(content_hash).write_bytes(content)


class _ObservationCollector:
    """Shared base: accumulate content-addressed observations, then finish.

    Both modalities gather observations at decision points and then build a
    single :class:`ActivityResult`. The base owns the accumulation and the
    ``finish`` boundary so each modality only declares its
    :class:`ActivityKind` and its capture verb.
    """

    kind: ActivityKind

    def __init__(self, *, store: ContentStore) -> None:
        self._store = store
        self._observations: list[Observation] = []

    def _capture(self, *, obs_kind: str, ref: str, content: bytes) -> Observation:
        """Content-address *content* at capture time and record the observation."""
        content_hash = self._store.put(content)
        obs = Observation(kind=obs_kind, ref=ref, content_hash=content_hash)
        self._observations.append(obs)
        return obs

    def observations(self) -> tuple[Observation, ...]:
        """Return the observations gathered so far, in capture order."""
        return tuple(self._observations)

    def finish(
        self,
        *,
        artifact: Any,
        terminal_state: TerminalState = TerminalState.COMPLETED,
        reason_code: str = "ok",
    ) -> ActivityResult:
        """Build the typed :class:`ActivityResult` for this activity.

        Args:
            artifact: The modality's opaque result payload.
            terminal_state: The typed terminal state.
            reason_code: A non-empty reason code.

        Returns:
            An :class:`ActivityResult` pinning the gathered evidence set.
        """
        return ActivityResult.build(
            kind=self.kind,
            artifact=artifact,
            observations=tuple(self._observations),
            terminal_state=terminal_state,
            reason_code=reason_code,
        )


class ResearchActivity(_ObservationCollector):
    """A research agent that content-addresses every fetched page (AC1).

    Each :meth:`fetch` hashes the page bytes at fetch time and stores them in the
    content-addressed store, so the fetched evidence is a forensic record. The
    built result's ``evidence_set_hash`` is a pure function of the fetched pages,
    so two runs that fetched the same corpus anchor the same evidence identity
    and a replay reattaches byte-identical pages.
    """

    kind = ActivityKind.RESEARCH

    def fetch(self, url: str, content: bytes) -> Observation:
        """Fetch a page: content-address its bytes at fetch time.

        Args:
            url: The page URL (provenance only -- not part of the content hash).
            content: The exact page bytes retrieved.

        Returns:
            The content-addressed :class:`Observation` for the page.
        """
        return self._capture(obs_kind="page", ref=url, content=content)


class BrowserActivity(_ObservationCollector):
    """A browser / computer-use agent recording one observation per decision (AC5).

    Each :meth:`observe` records the screenshot / DOM snapshot the model saw
    before a decision step, content-addressed at capture time. The observations
    are ordered by decision step, so a replay reattaches the per-decision
    snapshots and compares their hashes against the pinned evidence.
    """

    kind = ActivityKind.BROWSER

    def observe(self, *, step: str, snapshot: bytes) -> Observation:
        """Record the observation behind one decision step.

        Args:
            step: The decision-step label (provenance -- not part of the hash).
            snapshot: The screenshot / DOM snapshot bytes the model saw.

        Returns:
            The content-addressed :class:`Observation` for the decision step.
        """
        return self._capture(obs_kind="snapshot", ref=step, content=snapshot)


def replay_reattach(journal_path: Path, *, store: ContentStore, stage_id: str) -> list[bytes]:
    """Reattach the evidence bytes for an anchored activity from the store.

    Walks the run journal for the ``activity.result`` entry stamped *stage_id*,
    reads each observation's pinned content hash, retrieves the bytes from the
    content-addressed store, and re-verifies that the retrieved bytes still hash
    to the pinned hash. This is the replay side of AC1 / AC5: identical bytes are
    reattached in observation order, and any divergence (a tampered or wrong
    blob) is refused.

    Args:
        journal_path: Path to the run ``journal.jsonl``.
        store: The content-addressed store holding the evidence bytes.
        stage_id: The activity stage id to reattach.

    Returns:
        The reattached observation bytes, in the order they were captured.

    Raises:
        KeyError: When the stage is not found or a blob is missing from the store.
        ValueError: When a retrieved blob's recomputed hash does not match the
            pinned content hash (tamper / divergence).
    """
    observations = _observations_for_stage(journal_path, stage_id=stage_id)
    reattached: list[bytes] = []
    for obs in observations:
        content_hash = str(obs.get("content_hash", ""))
        content = store.get(content_hash)
        recomputed = "sha256:" + _sha256_hex(content)
        if recomputed != content_hash:
            raise ValueError(
                f"content hash mismatch reattaching {obs.get('ref')!r}: "
                f"pinned {content_hash!r}, recomputed {recomputed!r}"
            )
        reattached.append(content)
    return reattached


def _observations_for_stage(journal_path: Path, *, stage_id: str) -> Iterable[dict[str, Any]]:
    """Return the observation rows anchored for *stage_id* in the journal.

    Raises:
        KeyError: When no ``activity.result`` entry matches *stage_id*.
    """
    for row in load_events(journal_path):
        if row.get("event") == ACTIVITY_RESULT_EVENT and row.get("stage_id") == stage_id:
            obs = row.get("observations", [])
            return list(obs) if isinstance(obs, list) else []
    raise KeyError(f"no activity.result entry for stage {stage_id!r} in {journal_path}")


@dataclass(frozen=True, slots=True)
class StageVerdict:
    """Per-stage outcome of :func:`verify_run_activities`.

    Attributes:
        stage_id: The activity stage id.
        kind: The agent modality string.
        ok: Whether the stage's anchored evidence set recomputes and (when a
            store is available) its evidence bytes reattach and re-verify.
        evidence_reattached: Whether the evidence bytes were reattached from the
            content store and re-verified against their pinned hashes.
        reason: A short human-readable explanation on failure, else empty.
    """

    stage_id: str
    kind: str
    ok: bool
    evidence_reattached: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ActivityVerifyResult:
    """Outcome of verifying every activity anchored in a run journal.

    Attributes:
        run_id: The run whose journal was verified.
        found: Whether the run journal exists and holds at least one activity.
        ok: ``True`` only when the journal chain is intact and every stage
            verifies.
        chain_ok: Whether the journal's Merkle chain recomputes cleanly.
        stages: Per-stage verdicts in journal order.
        reason: A top-level explanation when the run is missing / empty.
    """

    run_id: str
    found: bool
    ok: bool
    chain_ok: bool
    stages: tuple[StageVerdict, ...] = ()
    reason: str = ""


def verify_run_activities(
    sdd_dir: Path,
    *,
    run_id: str,
    store: ContentStore | None = None,
) -> ActivityVerifyResult:
    """Verify every activity anchored in a run's canonical event journal (#2311).

    Walks ``<sdd_dir>/runs/<run_id>/journal.jsonl``. First confirms the journal's
    Merkle chain recomputes cleanly (a tampered anchored field breaks the chain
    at a precise step). Then, for each ``activity.result`` entry, recomputes the
    ``evidence_set_hash`` from the pinned observation hashes and compares it to
    the anchored value; when a content store is supplied it also reattaches each
    observation's bytes and re-verifies the content hash. Any divergence marks
    the stage -- and the run -- as failed.

    Args:
        sdd_dir: The project ``.sdd`` directory.
        run_id: The run to verify.
        store: Optional content-addressed store to reattach evidence bytes from.

    Returns:
        An :class:`ActivityVerifyResult`. ``found`` is ``False`` when no journal
        or no activity entry exists.
    """
    journal_path = sdd_dir / "runs" / run_id / "journal.jsonl"
    if not journal_path.exists():
        return ActivityVerifyResult(
            run_id=run_id,
            found=False,
            ok=False,
            chain_ok=False,
            reason=f"no run journal at {journal_path}",
        )

    chain = verify_journal(journal_path)
    rows = [r for r in load_events(journal_path) if r.get("event") == ACTIVITY_RESULT_EVENT]
    if not rows:
        return ActivityVerifyResult(
            run_id=run_id,
            found=False,
            ok=False,
            chain_ok=chain.ok,
            reason="run journal holds no activity.result entries",
        )

    verdicts: list[StageVerdict] = []
    for row in rows:
        verdicts.append(_verify_stage(row, store=store, chain_ok=chain.ok))

    ok = chain.ok and all(v.ok for v in verdicts)
    return ActivityVerifyResult(
        run_id=run_id,
        found=True,
        ok=ok,
        chain_ok=chain.ok,
        stages=tuple(verdicts),
    )


def _verify_stage(row: dict[str, Any], *, store: ContentStore | None, chain_ok: bool) -> StageVerdict:
    """Verify one anchored ``activity.result`` row."""
    stage_id = str(row.get("stage_id", ""))
    kind = str(row.get("kind", ""))
    raw_obs = row.get("observations", [])
    observations = list(raw_obs) if isinstance(raw_obs, list) else []
    rebuilt = [
        Observation(
            kind=str(o.get("kind", "")),
            ref=str(o.get("ref", "")),
            content_hash=str(o.get("content_hash", "")),
        )
        for o in observations
    ]

    recomputed = evidence_set_hash(rebuilt)
    anchored = str(row.get("evidence_set_hash", ""))
    if recomputed != anchored:
        return StageVerdict(
            stage_id=stage_id,
            kind=kind,
            ok=False,
            evidence_reattached=False,
            reason=f"evidence_set_hash mismatch: anchored {anchored!r}, recomputed {recomputed!r}",
        )

    if not chain_ok:
        return StageVerdict(
            stage_id=stage_id,
            kind=kind,
            ok=False,
            evidence_reattached=False,
            reason="journal Merkle chain diverges",
        )

    reattached = False
    if store is not None:
        for obs in rebuilt:
            try:
                content = store.get(obs.content_hash)
            except KeyError:
                return StageVerdict(
                    stage_id=stage_id,
                    kind=kind,
                    ok=False,
                    evidence_reattached=False,
                    reason=f"evidence bytes missing from store for {obs.ref!r}",
                )
            recomputed_hash = "sha256:" + _sha256_hex(content)
            if recomputed_hash != obs.content_hash:
                return StageVerdict(
                    stage_id=stage_id,
                    kind=kind,
                    ok=False,
                    evidence_reattached=False,
                    reason=f"content hash mismatch reattaching {obs.ref!r}",
                )
        reattached = bool(rebuilt)

    return StageVerdict(stage_id=stage_id, kind=kind, ok=True, evidence_reattached=reattached)
