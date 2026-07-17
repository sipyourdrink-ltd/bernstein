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

Data and ops modalities
------------------------
Unlike a read-only research fetch or a browser observation, a **data** or **ops**
activity changes the world, so it carries two extra guarantees the epic calls
for (:class:`DataActivity` / :class:`OpsActivity`):

* a **deterministic-plan vs side-effecting split** -- the plan
  (:class:`DataOpsPlan`) is a byte-identical projection of the signed inputs,
  derived *before* any output is recorded, so a replay recomputes the same plan
  hash from the same inputs. The runner refuses a side effect before a plan and
  refuses a new input after one, so the plan is provably the projection the
  effects were computed from; and
* **signed input/output artifacts** (:class:`SignedArtifact`) -- every input and
  output is content-addressed *and* Ed25519-signed with the install key, so a
  verifier confirms offline both which exact bytes crossed the boundary and that
  this install produced them.

Both modalities still return an
:class:`~bernstein.core.orchestration.activity.ActivityResult` the deterministic
scheduler dispatches and journals identically to research/browser: the signed
:class:`DataOpsReceipt` is the ``artifact`` (its hash anchored as
``artifact_hash`` and its canonical bytes stored content-addressed so an offline
verifier reattaches it), and the signed input/output bytes are the
content-addressed observations anchored via ``evidence_set_hash``. The receipt is
the primary artifact: strip the store and the journal and it stops verifying, not
merely stops logging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
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
from bernstein.core.orchestration.research_report import (
    ClaimVerdict,
    ResearchReport,
    ResearchReportVerdict,
    verify_research_report,
)
from bernstein.core.replay.journal import load_events, verify_journal
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ActivityVerifyResult",
    "BrowserActivity",
    "ContentStore",
    "DataActivity",
    "DataOpsPhaseError",
    "DataOpsPlan",
    "DataOpsReceipt",
    "DataOpsVerdict",
    "OpsActivity",
    "ResearchActivity",
    "SignedArtifact",
    "StageVerdict",
    "replay_reattach",
    "verify_data_ops_receipt",
    "verify_run_activities",
]

#: A bare sha256 hex digest (the addressable component of a content hash). Used
#: to realpath-contain a content-store lookup keyed on a hash read from the
#: journal, so a tampered hash can never escape the store root.
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8).

    Matches the canonicalisation the activity boundary hashes with, so the
    content hash of a receipt's canonical bytes equals its anchored
    ``artifact_hash``.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


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


# ---------------------------------------------------------------------------
# data / ops modalities: deterministic-plan split + signed input/output artifacts
# ---------------------------------------------------------------------------


class DataOpsPhaseError(RuntimeError):
    """A data/ops activity violated the deterministic-plan vs side-effect split.

    Raised when a caller records a side-effecting output before a deterministic
    plan is derived, adds an input after the plan is fixed, or finishes before a
    plan exists. The split is what makes the plan a provable projection of the
    inputs it was computed from.
    """


@dataclass(frozen=True, slots=True)
class SignedArtifact:
    """A content-addressed, Ed25519-signed input or output artifact.

    The signature covers the canonical ``{role, ref, content_hash}`` binding, so
    a verifier confirms offline both which exact bytes crossed the boundary and
    that the install key produced them. The bytes themselves live in the shared
    :class:`ContentStore`, keyed by ``content_hash``.

    Attributes:
        role: ``input`` or ``output``.
        ref: Provenance reference (dataset path, target, result label).
        content_hash: The ``sha256:`` hash of the artifact bytes.
        signature: Base64url detached Ed25519 signature over the binding.
    """

    role: str
    ref: str
    content_hash: str
    signature: str

    def signing_payload(self) -> bytes:
        """Return the canonical bytes the signature covers."""
        return _canonical_bytes({"role": self.role, "ref": self.ref, "content_hash": self.content_hash})

    def to_dict(self) -> dict[str, str]:
        """Return the JSON projection stored on the receipt."""
        return {"role": self.role, "ref": self.ref, "content_hash": self.content_hash, "signature": self.signature}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> SignedArtifact:
        """Rebuild a signed artifact from its receipt projection."""
        return cls(
            role=str(row.get("role", "")),
            ref=str(row.get("ref", "")),
            content_hash=str(row.get("content_hash", "")),
            signature=str(row.get("signature", "")),
        )


@dataclass(frozen=True, slots=True)
class DataOpsPlan:
    """The deterministic plan a data/ops activity derives before any side effect.

    The plan is a pure projection of the signed inputs and the declared steps:
    ``plan_hash`` is a function of the sorted, de-duplicated input content hashes
    and the ordered steps only. Two operators with the same inputs and steps
    derive the byte-identical ``plan_hash``, whatever order the inputs arrived
    in, so a replay recomputes and re-verifies it.

    Attributes:
        input_hashes: Sorted, de-duplicated input content hashes.
        steps: The ordered intended side effects (declarative, not executed).
        plan_hash: The ``sha256:`` hash over ``{input_hashes, steps}``.
    """

    input_hashes: tuple[str, ...]
    steps: tuple[str, ...]
    plan_hash: str

    @classmethod
    def derive_from_hashes(cls, *, input_hashes: Sequence[str], steps: Sequence[str]) -> DataOpsPlan:
        """Derive a plan from input content hashes and declared steps."""
        canonical_inputs = tuple(sorted(set(input_hashes)))
        canonical_steps = tuple(steps)
        plan_hash = "sha256:" + _sha256_hex(
            _canonical_bytes({"input_hashes": list(canonical_inputs), "steps": list(canonical_steps)})
        )
        return cls(input_hashes=canonical_inputs, steps=canonical_steps, plan_hash=plan_hash)

    @classmethod
    def derive(cls, *, inputs: Sequence[SignedArtifact], steps: Sequence[str]) -> DataOpsPlan:
        """Derive a plan from the signed input artifacts and declared steps."""
        return cls.derive_from_hashes(input_hashes=[a.content_hash for a in inputs], steps=steps)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON projection stored on the receipt."""
        return {
            "input_hashes": list(self.input_hashes),
            "steps": list(self.steps),
            "plan_hash": self.plan_hash,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> DataOpsPlan:
        """Rebuild a plan from its receipt projection (input hashes re-canonicalised)."""
        raw_inputs = row.get("input_hashes", [])
        raw_steps = row.get("steps", [])
        input_hashes = tuple(sorted({str(h) for h in raw_inputs})) if isinstance(raw_inputs, list) else ()
        steps = tuple(str(s) for s in raw_steps) if isinstance(raw_steps, list) else ()
        return cls(input_hashes=input_hashes, steps=steps, plan_hash=str(row.get("plan_hash", "")))


@dataclass(frozen=True, slots=True)
class DataOpsReceipt:
    """The signed result record a data/ops activity produces (the artifact).

    The receipt is the primary artifact of a data/ops boundary crossing: it binds
    the deterministic :class:`DataOpsPlan`, the signed input and output artifacts,
    and the install public key that signed them into one record. Its canonical
    bytes hash to the activity's ``artifact_hash`` (anchored in the journal) and
    are stored content-addressed so :func:`verify_run_activities` reattaches and
    re-verifies it offline.

    Attributes:
        kind: The modality string (``data`` or ``ops``).
        plan: The deterministic plan the side effects were computed from.
        inputs: The signed input artifacts.
        outputs: The signed output artifacts.
        signer_public_key_pem: The install public key the artifacts verify under.
    """

    kind: str
    plan: DataOpsPlan
    inputs: tuple[SignedArtifact, ...]
    outputs: tuple[SignedArtifact, ...]
    signer_public_key_pem: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON artifact projection (hashed as ``artifact_hash``)."""
        return {
            "kind": self.kind,
            "plan": self.plan.to_dict(),
            "inputs": [a.to_dict() for a in self.inputs],
            "outputs": [a.to_dict() for a in self.outputs],
            "signer_public_key_pem": self.signer_public_key_pem,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> DataOpsReceipt:
        """Rebuild a receipt from its artifact projection."""
        raw_inputs = row.get("inputs", [])
        raw_outputs = row.get("outputs", [])
        return cls(
            kind=str(row.get("kind", "")),
            plan=DataOpsPlan.from_dict(row.get("plan", {}) if isinstance(row.get("plan"), dict) else {}),
            inputs=tuple(SignedArtifact.from_dict(a) for a in raw_inputs) if isinstance(raw_inputs, list) else (),
            outputs=tuple(SignedArtifact.from_dict(a) for a in raw_outputs) if isinstance(raw_outputs, list) else (),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
        )


@dataclass(frozen=True, slots=True)
class DataOpsVerdict:
    """Outcome of :func:`verify_data_ops_receipt`.

    Attributes:
        ok: True only when the plan recomputes, every signature verifies, and
            (when a store is supplied) every artifact's bytes reattach.
        plan_ok: Whether ``plan_hash`` recomputes from the signed inputs + steps.
        signatures_ok: Whether every input/output signature verifies.
        evidence_reattached: Whether the artifact bytes reattached from the store
            and re-hashed to their pinned content hashes.
        reason: A short explanation on failure, else empty.
    """

    ok: bool
    plan_ok: bool
    signatures_ok: bool
    evidence_reattached: bool
    reason: str = ""


def verify_data_ops_receipt(receipt: DataOpsReceipt, *, store: ContentStore | None = None) -> DataOpsVerdict:
    """Verify a data/ops signed receipt offline (determinism + signatures).

    Recomputes the deterministic plan from the signed inputs and declared steps
    and compares it to the anchored ``plan_hash`` (a tampered step list or a
    swapped input diverges); verifies every input and output Ed25519 signature
    against the receipt's signer public key; and, when a content store is
    supplied, reattaches each artifact's bytes and re-hashes them to their pinned
    content hash.

    Args:
        receipt: The receipt to verify.
        store: Optional content store to reattach artifact bytes from.

    Returns:
        A :class:`DataOpsVerdict`. ``ok`` requires plan, signatures, and (when a
        store is supplied) reattachment to all hold.
    """
    recomputed = DataOpsPlan.derive_from_hashes(
        input_hashes=[a.content_hash for a in receipt.inputs], steps=receipt.plan.steps
    )
    plan_ok = recomputed.plan_hash == receipt.plan.plan_hash and recomputed.input_hashes == receipt.plan.input_hashes
    if not plan_ok:
        return DataOpsVerdict(
            ok=False,
            plan_ok=False,
            signatures_ok=False,
            evidence_reattached=False,
            reason=f"plan_hash mismatch: anchored {receipt.plan.plan_hash!r}, recomputed {recomputed.plan_hash!r}",
        )

    for artifact in (*receipt.inputs, *receipt.outputs):
        outcome = verify_payload(
            artifact.signing_payload(),
            artifact.signature,
            receipt.signer_public_key_pem or None,
            allow_unverified=True,
        )
        if not outcome.verified:
            return DataOpsVerdict(
                ok=False,
                plan_ok=True,
                signatures_ok=False,
                evidence_reattached=False,
                reason=f"signature does not verify for {artifact.role} {artifact.ref!r}: {outcome.reason}",
            )

    reattached = False
    if store is not None:
        for artifact in (*receipt.inputs, *receipt.outputs):
            try:
                content = store.get(artifact.content_hash)
            except KeyError:
                return DataOpsVerdict(
                    ok=False,
                    plan_ok=True,
                    signatures_ok=True,
                    evidence_reattached=False,
                    reason=f"artifact bytes missing from store for {artifact.ref!r}",
                )
            if "sha256:" + _sha256_hex(content) != artifact.content_hash:
                return DataOpsVerdict(
                    ok=False,
                    plan_ok=True,
                    signatures_ok=True,
                    evidence_reattached=False,
                    reason=f"content hash mismatch reattaching {artifact.ref!r}",
                )
        reattached = True

    return DataOpsVerdict(ok=True, plan_ok=True, signatures_ok=True, evidence_reattached=reattached)


class _SignedArtifactActivity(_ObservationCollector):
    """Shared base for the data/ops modalities: signed I/O + plan/effect split.

    A subclass only declares its :class:`ActivityKind`. The base enforces the
    deterministic-plan vs side-effecting split (inputs, then one plan, then
    outputs), signs every artifact with the install key, content-addresses each
    as an observation so it journals identically to research/browser, and builds
    a :class:`DataOpsReceipt` as the result artifact.
    """

    def __init__(self, *, store: ContentStore, private_key_pem: str, public_key_pem: str) -> None:
        super().__init__(store=store)
        # Keys are held verbatim: they must be byte-identical to what signs /
        # verifies, so they are never stripped or reformatted.
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem
        self._inputs: list[SignedArtifact] = []
        self._outputs: list[SignedArtifact] = []
        self._plan: DataOpsPlan | None = None

    def _signed(self, *, role: str, obs: Observation) -> SignedArtifact:
        payload = _canonical_bytes({"role": role, "ref": obs.ref, "content_hash": obs.content_hash})
        signature = sign_payload(payload, self._private_key_pem)
        return SignedArtifact(role=role, ref=obs.ref, content_hash=obs.content_hash, signature=signature)

    def add_input(self, *, ref: str, content: bytes) -> SignedArtifact:
        """Record a signed input artifact (the plan-phase, before any effect).

        Raises:
            DataOpsPhaseError: If called after the plan is derived.
        """
        if self._plan is not None:
            raise DataOpsPhaseError("inputs are frozen once the deterministic plan is derived")
        obs = self._capture(obs_kind="input", ref=ref, content=content)
        artifact = self._signed(role="input", obs=obs)
        self._inputs.append(artifact)
        return artifact

    def plan(self, steps: Sequence[str]) -> DataOpsPlan:
        """Derive the deterministic plan from the signed inputs and *steps*.

        Raises:
            DataOpsPhaseError: If a plan was already derived.
        """
        if self._plan is not None:
            raise DataOpsPhaseError("a deterministic plan was already derived")
        self._plan = DataOpsPlan.derive(inputs=self._inputs, steps=steps)
        return self._plan

    def add_output(self, *, ref: str, content: bytes) -> SignedArtifact:
        """Record a signed output artifact (the side-effecting phase).

        Raises:
            DataOpsPhaseError: If called before the deterministic plan exists.
        """
        if self._plan is None:
            raise DataOpsPhaseError("cannot record a side effect before a deterministic plan")
        obs = self._capture(obs_kind="output", ref=ref, content=content)
        artifact = self._signed(role="output", obs=obs)
        self._outputs.append(artifact)
        return artifact

    def receipt(self) -> DataOpsReceipt:
        """Build the signed receipt for this activity.

        Raises:
            DataOpsPhaseError: If no plan has been derived.
        """
        if self._plan is None:
            raise DataOpsPhaseError("no deterministic plan derived")
        return DataOpsReceipt(
            kind=self.kind.value,
            plan=self._plan,
            inputs=tuple(self._inputs),
            outputs=tuple(self._outputs),
            signer_public_key_pem=self._public_key_pem,
        )

    def finish(
        self,
        *,
        terminal_state: TerminalState = TerminalState.COMPLETED,
        reason_code: str = "ok",
    ) -> ActivityResult:
        """Build the typed :class:`ActivityResult` carrying the signed receipt.

        The receipt's canonical bytes are stored content-addressed under the
        activity's ``artifact_hash`` so an offline verifier reattaches and
        re-verifies the receipt from the run's content store alone.

        Raises:
            DataOpsPhaseError: If no plan has been derived.
        """
        artifact = self.receipt().to_dict()
        self._store.put(_canonical_bytes(artifact))
        return ActivityResult.build(
            kind=self.kind,
            artifact=artifact,
            observations=self.observations(),
            terminal_state=terminal_state,
            reason_code=reason_code,
        )


class DataActivity(_SignedArtifactActivity):
    """A data agent: deterministic transform plan over signed input/output data.

    The signed inputs are the source datasets, the plan is the deterministic
    transform derived from them, and the signed outputs are the produced
    datasets. The plan is a byte-identical projection of the inputs, so a replay
    recomputes it and a verifier confirms the transform acted on the plan the
    inputs imply.
    """

    kind = ActivityKind.DATA


class OpsActivity(_SignedArtifactActivity):
    """An ops agent: deterministic change plan over signed input/output targets.

    The signed inputs are the target descriptors, the plan is the deterministic
    set of intended changes, and the signed outputs are the applied results. The
    plan/effect split guarantees no change is recorded before the plan the change
    was derived from, so a postmortem can prove which plan a side effect executed.
    """

    kind = ActivityKind.OPS


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
        signed_receipt_verified: Whether a data/ops signed receipt was reattached
            from the store and its plan + input/output signatures re-verified.
        claim_verdicts: Per-claim citation verdicts for a research stage
            (empty for other modalities), in the report's claim order.
        reason: A short human-readable explanation on failure, else empty.
    """

    stage_id: str
    kind: str
    ok: bool
    evidence_reattached: bool
    signed_receipt_verified: bool = False
    claim_verdicts: tuple[ClaimVerdict, ...] = ()
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

    verdicts: list[StageVerdict] = [_verify_stage(row, store=store, chain_ok=chain.ok) for row in rows]

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

    # Research citation lineage (#2524): resolve every claim's citation against
    # the store first, so a tampered *cited* page fails naming the claim and the
    # mismatched hash rather than as an anonymous observation-reattach failure.
    claim_verdicts: tuple[ClaimVerdict, ...] = ()
    if store is not None and kind == ActivityKind.RESEARCH.value:
        research = _verify_research_stage(row, store=store)
        if research is not None:
            claim_verdicts = research.claims
            if not research.ok:
                reason = research.reason or next(
                    (c.reason for c in research.claims if not c.ok),
                    "research report verification failed",
                )
                return StageVerdict(
                    stage_id=stage_id,
                    kind=kind,
                    ok=False,
                    evidence_reattached=False,
                    claim_verdicts=claim_verdicts,
                    reason=reason,
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
                    claim_verdicts=claim_verdicts,
                    reason=f"evidence bytes missing from store for {obs.ref!r}",
                )
            recomputed_hash = "sha256:" + _sha256_hex(content)
            if recomputed_hash != obs.content_hash:
                return StageVerdict(
                    stage_id=stage_id,
                    kind=kind,
                    ok=False,
                    evidence_reattached=False,
                    claim_verdicts=claim_verdicts,
                    reason=f"content hash mismatch reattaching {obs.ref!r}",
                )
        reattached = bool(rebuilt)

    receipt_verified = False
    if store is not None and kind in {ActivityKind.DATA.value, ActivityKind.OPS.value}:
        verdict = _verify_data_ops_stage(row, store=store)
        if verdict is not None:
            if not verdict.ok:
                return StageVerdict(
                    stage_id=stage_id,
                    kind=kind,
                    ok=False,
                    evidence_reattached=reattached,
                    reason=verdict.reason,
                )
            receipt_verified = True

    return StageVerdict(
        stage_id=stage_id,
        kind=kind,
        ok=True,
        evidence_reattached=reattached,
        signed_receipt_verified=receipt_verified,
        claim_verdicts=claim_verdicts,
    )


def _verify_data_ops_stage(row: dict[str, Any], *, store: ContentStore) -> DataOpsVerdict | None:
    """Reattach and re-verify a data/ops signed receipt for one anchored row.

    The receipt's canonical bytes were stored content-addressed under the
    stage's ``artifact_hash``. This reattaches them by that hash, confirms the
    reattached bytes still hash to the anchored ``artifact_hash`` (tamper), and
    re-verifies the plan projection and every input/output signature. Returns
    ``None`` when no receipt is stored (a legacy or non-receipt data/ops row),
    so the generic evidence check stands alone.
    """
    artifact_hash = str(row.get("artifact_hash", ""))
    digest = artifact_hash.split(":", 1)[-1]
    # Realpath-contain the store lookup: the hash comes from the journal, so
    # only a bare sha256 hex digest may address a blob (no path separators).
    if not _SHA256_HEX.match(digest):
        return DataOpsVerdict(
            ok=False,
            plan_ok=False,
            signatures_ok=False,
            evidence_reattached=False,
            reason=f"malformed artifact_hash for data/ops stage: {artifact_hash!r}",
        )
    try:
        receipt_bytes = store.get(artifact_hash)
    except KeyError:
        return None
    if "sha256:" + _sha256_hex(receipt_bytes) != artifact_hash:
        return DataOpsVerdict(
            ok=False,
            plan_ok=False,
            signatures_ok=False,
            evidence_reattached=False,
            reason=f"receipt bytes do not match anchored artifact_hash {artifact_hash!r}",
        )
    try:
        receipt = DataOpsReceipt.from_dict(json.loads(receipt_bytes))
    except (ValueError, TypeError) as exc:
        return DataOpsVerdict(
            ok=False,
            plan_ok=False,
            signatures_ok=False,
            evidence_reattached=False,
            reason=f"stored receipt is not valid JSON: {type(exc).__name__}",
        )
    return verify_data_ops_receipt(receipt, store=store)


def _verify_research_stage(row: dict[str, Any], *, store: ContentStore) -> ResearchReportVerdict | None:
    """Reattach and re-resolve a research report's citation lineage (#2524).

    The report's canonical bytes were stored content-addressed under the stage's
    ``artifact_hash``. This reattaches them by that hash, confirms they still hash
    to the anchored ``artifact_hash`` (tamper of the report itself), parses the
    report, and resolves every claim's citation offline against the store --
    naming the claim and the mismatched hash on any altered source page. Returns
    ``None`` when no report is stored (a legacy or non-report research row), so
    the generic evidence check stands alone.
    """
    artifact_hash = str(row.get("artifact_hash", ""))
    digest = artifact_hash.split(":", 1)[-1]
    # Realpath-contain the store lookup: the hash comes from the journal, so only
    # a bare sha256 hex digest may address a blob (no path separators).
    if not _SHA256_HEX.match(digest):
        return ResearchReportVerdict(
            ok=False,
            claims=(),
            reason=f"malformed artifact_hash for research stage: {artifact_hash!r}",
        )
    try:
        report_bytes = store.get(artifact_hash)
    except KeyError:
        return None
    if "sha256:" + _sha256_hex(report_bytes) != artifact_hash:
        return ResearchReportVerdict(
            ok=False,
            claims=(),
            reason=f"report bytes do not match anchored artifact_hash {artifact_hash!r}",
        )
    try:
        report = ResearchReport.from_dict(json.loads(report_bytes))
    except (ValueError, TypeError) as exc:
        return ResearchReportVerdict(
            ok=False,
            claims=(),
            reason=f"stored report is not valid JSON: {type(exc).__name__}",
        )
    # A stored report with no claims is not a valid research artifact; surface it
    # as one refused claim so the failure is not silently empty.
    verdict = verify_research_report(report, store=store)
    if not report.claims and not verdict.claims:
        return ResearchReportVerdict(
            ok=False,
            claims=(ClaimVerdict(claim_id="", ok=False, citations_checked=0, reason="report holds no claims"),),
            reason=verdict.reason or "report holds no claims",
        )
    return verdict
