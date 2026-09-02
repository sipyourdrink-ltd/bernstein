"""Model drift probe: signed, chain-anchored observations (issue #5041).

A model that was admitted six months ago and behaves differently today is
indistinguishable, in our records, from one that behaves the same. The
lineage receipt (:class:`~bernstein.core.lineage.ModelRef`) records what the
provider *said* it served; it does not record whether that thing still
behaves like it did.

This module produces the missing record. It does not add a dashboard or a
threshold: the artefact is a :class:`DriftObservation` — *on this date, this
model, on this fixed suite, produced these results; here is the baseline it
was compared against, and here is the delta* — signed off the install
identity and anchored in the HMAC-chained audit log, so the series is ordered
and an edit after the fact is visible.

Nothing here is new eval, metric or signing machinery. The suite, the runner
and the bundle come from :mod:`bernstein.eval.bench`; the baseline comes from
:mod:`bernstein.eval.baseline`; the signature is the same detached Ed25519
JWS the reliability receipt uses; the chain write is the ordinary
``log_with_prev_digest`` path.

Declared sampling
-----------------
A probe may run a subset of the suite. When it does, the observation carries
``cases_declared``, ``cases_ran``, a non-empty ``sampling_reason`` and a
``coverage`` label, and the verifier refuses an observation whose label
disagrees with the cases it names. An unlabelled sample is how a claim that
we watch our models becomes false without anyone noticing, so a sample that
cannot say what it skipped is not emitted at all.

Comparison scope
----------------
The comparison is computed over exactly the cases that ran, against the
baseline's per-case components restricted to the same ids. A baseline that
carries no component for a case that ran yields
:attr:`ComparisonStatus.INCOMPARABLE` and no delta: a subset's mean measured
against a whole suite's mean is a number with no meaning, and emitting it
would be the same silent-sample failure in a different field.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac as hmac_mod
import json
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.core.lineage import ModelRef
from bernstein.core.security.audit_chain import record_model_drift_observation
from bernstein.eval.baseline import EvalBaseline
from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.runner import BenchRunner
from bernstein.eval.bench.suite import BenchSuite

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.eval.bench.runner import ReplayAdapter

__all__ = [
    "COVERAGE_FULL",
    "COVERAGE_PARTIAL",
    "CaseDelta",
    "ComparisonStatus",
    "DriftComparison",
    "DriftObservation",
    "DriftObservationVerifier",
    "DriftProbe",
    "DriftSignerProtocol",
    "DriftVerificationResult",
    "DriftVerificationStatus",
    "InstallIdentityDriftSigner",
    "StubDriftSigner",
    "baseline_fingerprint",
    "compare_to_baseline",
    "record_observation",
]

#: JWS ``typ`` binding install-identity signatures to this artefact, so a
#: signature minted for another surface cannot be replayed as a drift
#: observation signature.
_DRIFT_JWS_TYP = "bernstein-model-drift-observation+jws"

#: Every case the suite declares was run.
COVERAGE_FULL = "full"
#: A subset ran; ``sampling_reason`` says which subset and why.
COVERAGE_PARTIAL = "partial"


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _model_ref_dict(ref: ModelRef) -> dict[str, Any]:
    """Canonical dict for a :class:`ModelRef`, dropping unset optionals.

    Mirrors the drop rule :mod:`bernstein.core.lineage.entry` applies to the
    same fields, so the model reference hashed into an observation is the one
    the lineage receipt canonicalises.
    """
    body = dataclasses.asdict(ref)
    return {k: v for k, v in body.items() if v is not None}


def _model_ref_from_dict(raw: Mapping[str, Any]) -> ModelRef:
    return ModelRef(
        provider=str(raw["provider"]),
        model_requested=str(raw["model_requested"]),
        model_reported=raw.get("model_reported"),
        version=raw.get("version"),
        routing_decision_hash=str(raw.get("routing_decision_hash", "")),
    )


def baseline_fingerprint(baseline: EvalBaseline) -> str:
    """Return the content hash of *baseline*.

    The baseline itself is not a signed artefact; naming it by content hash
    inside a signed observation is what makes "which baseline was this
    compared against" answerable after the fact.
    """
    return hashlib.sha256(_canonical_bytes(baseline.to_dict())).hexdigest()


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class ComparisonStatus(Enum):
    """Whether a delta could honestly be computed."""

    COMPARABLE = "comparable"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True)
class CaseDelta:
    """One case's movement against its baseline score."""

    case_id: str
    baseline_score: float
    observed_score: float
    delta: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "baseline_score": self.baseline_score,
            "observed_score": self.observed_score,
            "delta": self.delta,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CaseDelta:
        return cls(
            case_id=str(raw["case_id"]),
            baseline_score=float(raw["baseline_score"]),
            observed_score=float(raw["observed_score"]),
            delta=float(raw["delta"]),
        )


@dataclass(frozen=True)
class DriftComparison:
    """The deterministic delta between an observed run and a baseline."""

    baseline_hash: str
    status: ComparisonStatus
    case_deltas: tuple[CaseDelta, ...] = ()
    #: Mean observed score minus mean baseline score over the cases that ran.
    #: ``None`` when the comparison is :attr:`ComparisonStatus.INCOMPARABLE`.
    aggregate_delta: float | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_hash": self.baseline_hash,
            "status": self.status.value,
            "case_deltas": [cd.to_dict() for cd in self.case_deltas],
            "aggregate_delta": self.aggregate_delta,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DriftComparison:
        aggregate = raw.get("aggregate_delta")
        return cls(
            baseline_hash=str(raw["baseline_hash"]),
            status=ComparisonStatus(raw["status"]),
            case_deltas=tuple(CaseDelta.from_dict(cd) for cd in raw.get("case_deltas", [])),
            aggregate_delta=None if aggregate is None else float(aggregate),
            detail=str(raw.get("detail", "")),
        )


def compare_to_baseline(bundle: SubmissionBundle, baseline: EvalBaseline) -> DriftComparison:
    """Compare *bundle*'s per-case scores against *baseline*, deterministically.

    Only the cases present in *bundle* participate: the bundle is the record
    of what ran. Cases are visited in bundle order (which is suite order), so
    two callers holding the same bundle and the same baseline produce
    byte-identical comparisons.
    """
    fingerprint = baseline_fingerprint(baseline)
    missing = [tr.task_id for tr in bundle.task_results if tr.task_id not in baseline.components]
    if missing:
        return DriftComparison(
            baseline_hash=fingerprint,
            status=ComparisonStatus.INCOMPARABLE,
            detail=(
                "Baseline carries no per-case score for "
                + ", ".join(sorted(missing))
                + "; a subset mean measured against a whole-suite mean is not a delta."
            ),
        )
    if not bundle.task_results:
        return DriftComparison(
            baseline_hash=fingerprint,
            status=ComparisonStatus.INCOMPARABLE,
            detail="No cases ran; there is nothing to compare.",
        )

    case_deltas = tuple(
        CaseDelta(
            case_id=tr.task_id,
            baseline_score=float(baseline.components[tr.task_id]),
            observed_score=float(tr.score),
            delta=float(tr.score) - float(baseline.components[tr.task_id]),
        )
        for tr in bundle.task_results
    )
    count = len(case_deltas)
    observed_mean = sum(cd.observed_score for cd in case_deltas) / count
    baseline_mean = sum(cd.baseline_score for cd in case_deltas) / count
    return DriftComparison(
        baseline_hash=fingerprint,
        status=ComparisonStatus.COMPARABLE,
        case_deltas=case_deltas,
        aggregate_delta=observed_mean - baseline_mean,
    )


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass
class DriftObservation:
    """One dated, signed statement about how a model behaved on a fixed suite.

    The observation embeds the run bundle and the baseline snapshot, so a
    holder of the file alone can recompute the delta and the observation hash
    without our database, our suite files or the emitting machine.
    """

    model_ref: ModelRef
    suite_hash: str
    suite_version: str
    #: Every case the suite declares, in suite order.
    cases_declared: tuple[str, ...]
    #: The cases this probe actually ran, in suite order.
    cases_ran: tuple[str, ...]
    #: Why the probe ran a subset. Empty exactly when coverage is full.
    sampling_reason: str
    coverage: str
    baseline: EvalBaseline
    bundle: SubmissionBundle
    comparison: DriftComparison
    observed_at: float = field(default_factory=time.time)
    #: The chain head this observation was anchored onto, filled by
    #: :func:`record_observation` before signing.
    prev_chain_digest: str = ""
    signature: str = ""
    signer_fingerprint: str = ""

    _observation_hash: str | None = field(default=None, init=False, repr=False, compare=False)

    # -- content hash ---------------------------------------------------

    def observation_hash(self) -> str:
        """Hash over everything except the signature fields."""
        if self._observation_hash is None:
            self._observation_hash = hashlib.sha256(_canonical_bytes(self._hashed_body())).hexdigest()
        return self._observation_hash

    def _hashed_body(self) -> dict[str, Any]:
        return {
            "model_ref": _model_ref_dict(self.model_ref),
            "suite_hash": self.suite_hash,
            "suite_version": self.suite_version,
            "cases_declared": list(self.cases_declared),
            "cases_ran": list(self.cases_ran),
            "sampling_reason": self.sampling_reason,
            "coverage": self.coverage,
            "baseline": self.baseline.to_dict(),
            "bundle": self.bundle.to_dict(),
            "comparison": self.comparison.to_dict(),
            "observed_at": self.observed_at,
            "prev_chain_digest": self.prev_chain_digest,
        }

    @property
    def is_partial(self) -> bool:
        """True when fewer cases ran than the suite declares."""
        return self.cases_ran != self.cases_declared

    # -- serialisation --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        body = self._hashed_body()
        body["observation_hash"] = self.observation_hash()
        body["signature"] = self.signature
        body["signer_fingerprint"] = self.signer_fingerprint
        return body

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DriftObservation:
        observation = cls(
            model_ref=_model_ref_from_dict(raw["model_ref"]),
            suite_hash=str(raw["suite_hash"]),
            suite_version=str(raw["suite_version"]),
            cases_declared=tuple(str(c) for c in raw["cases_declared"]),
            cases_ran=tuple(str(c) for c in raw["cases_ran"]),
            sampling_reason=str(raw["sampling_reason"]),
            coverage=str(raw["coverage"]),
            baseline=EvalBaseline.from_dict(dict(raw["baseline"])),
            bundle=SubmissionBundle.from_dict(raw["bundle"]),
            comparison=DriftComparison.from_dict(raw["comparison"]),
            observed_at=float(raw["observed_at"]),
            prev_chain_digest=str(raw.get("prev_chain_digest", "")),
            signature=str(raw.get("signature", "")),
            signer_fingerprint=str(raw.get("signer_fingerprint", "")),
        )
        stored = str(raw.get("observation_hash", ""))
        if stored and observation.observation_hash() != stored:
            raise ValueError(
                f"Drift observation hash mismatch: stored {stored!r} "
                f"!= recomputed {observation.observation_hash()!r}. "
                "The observation file has been modified since it was emitted."
            )
        return observation

    @classmethod
    def load(cls, path: Path) -> DriftObservation:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Signing (mirrors bench/reliability: stub for tests, install identity in prod)
# ---------------------------------------------------------------------------


class DriftSignerProtocol(Protocol):
    """Anything that can sign a drift observation."""

    def sign(self, observation: DriftObservation) -> DriftObservation:
        """Return a *new* observation with the signature fields populated."""
        ...


class StubDriftSigner:
    """Deterministic stub: HMAC-SHA256 over the observation hash.

    Never use in production — the key is public and provides no security.
    Mirrors ``bench.signer.StubSigner``.
    """

    _TEST_KEY = b"bernstein-model-drift-stub-signer-v1"

    @classmethod
    def fingerprint(cls) -> str:
        return hashlib.sha256(cls._TEST_KEY).hexdigest()[:16] + "-stub"

    @classmethod
    def expected_signature(cls, observation: DriftObservation) -> str:
        raw = hmac_mod.new(cls._TEST_KEY, observation.observation_hash().encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def sign(self, observation: DriftObservation) -> DriftObservation:
        return replace(
            observation,
            signature=self.expected_signature(observation),
            signer_fingerprint=self.fingerprint(),
        )


class InstallIdentityDriftSigner:
    """Detached Ed25519 JWS over the observation hash, keyed by the install identity.

    Explicit key material can be injected for hermetic tests; without it the
    install keystore is used. Signing fails loudly when no key material is
    available — an observation is never silently downgraded to the stub key.
    """

    def __init__(
        self,
        private_key_pem: bytes | None = None,
        public_key_pem: bytes | None = None,
    ) -> None:
        if (private_key_pem is None) != (public_key_pem is None):
            raise ValueError("Provide both private_key_pem and public_key_pem, or neither.")
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem

    def _key_material(self) -> tuple[bytes, bytes]:
        if self._private_key_pem is not None and self._public_key_pem is not None:
            return self._private_key_pem, self._public_key_pem
        from bernstein.core.identity.http_signing import default_keystore

        return default_keystore().load_or_generate()

    def fingerprint(self) -> str:
        """The install-identity keyid this signer stamps into observations."""
        from bernstein.core.identity.http_signing import install_identity_keyid

        _, public_pem = self._key_material()
        return install_identity_keyid(public_pem)

    def public_key_pem(self) -> bytes:
        """SPKI PEM of the verifying key (for building a trusted-key map)."""
        _, public_pem = self._key_material()
        return public_pem

    def sign(self, observation: DriftObservation) -> DriftObservation:
        from bernstein.core.identity.http_signing import install_identity_keyid
        from bernstein.core.security.agent_card_signer import sign_detached_jws_over_canonical

        private_pem, public_pem = self._key_material()
        kid = install_identity_keyid(public_pem)
        signature = sign_detached_jws_over_canonical(
            observation.observation_hash().encode(),
            private_pem,
            typ=_DRIFT_JWS_TYP,
            kid=kid,
        )
        return replace(observation, signature=signature, signer_fingerprint=kid)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


@dataclass
class DriftProbe:
    """Run a fixed, versioned suite against an admitted model.

    The probe is on-demand: it holds no schedule and enforces no bound. Its
    only output is a :class:`DriftObservation`.
    """

    suite: BenchSuite
    adapter: ReplayAdapter
    model_ref: ModelRef
    scheduler_config: dict[str, Any] = field(default_factory=dict[str, Any])
    #: Injectable clock so an observation is reproducible in tests.
    observed_at: float | None = None

    def run(
        self,
        *,
        baseline: EvalBaseline,
        case_ids: Sequence[str] | None = None,
        sampling_reason: str = "",
    ) -> DriftObservation:
        """Probe the model and return an unsigned observation.

        Args:
            baseline: The baseline this run is compared against.
            case_ids: Optional subset of suite case ids to run. ``None`` runs
                the whole suite.
            sampling_reason: Why the subset was chosen. Required for a partial
                run and refused for a full one, so the field never reads as
                decoration.

        Raises:
            ValueError: When *case_ids* names a case the suite does not
                declare, when it is empty, or when the sampling reason
                disagrees with the coverage the run actually achieves.
        """
        declared = tuple(task.id for task in self.suite.tasks)
        selected = self._select(declared, case_ids)
        coverage = COVERAGE_FULL if selected == declared else COVERAGE_PARTIAL
        reason = sampling_reason.strip()
        if coverage == COVERAGE_PARTIAL and not reason:
            raise ValueError(
                "A partial suite run must state a sampling_reason: an unlabelled "
                "sample reads as full coverage, which is the failure this probe exists to prevent."
            )
        if coverage == COVERAGE_FULL and reason:
            raise ValueError("sampling_reason is only meaningful for a partial run; the whole suite ran.")

        # One clock reading stamps both the bundle and the observation: two
        # readings let an observation claim a time its own bundle disagrees
        # with, and pinning the reading is what makes an observation
        # reproducible byte-for-byte from the same inputs.
        observed_at = time.time() if self.observed_at is None else self.observed_at

        # Run only the selected cases, then restate the *declared* suite hash
        # on the bundle: the subset is a subset of a known suite, not a suite
        # of its own, and ``cases_ran`` is what says which part of it ran.
        ran_suite = BenchSuite(
            version=self.suite.version,
            tasks=[task for task in self.suite.tasks if task.id in set(selected)],
        )
        bundle = replace(
            BenchRunner(suite=ran_suite, adapter=self.adapter, scheduler_config=self.scheduler_config).run(),
            suite_hash=self.suite.suite_hash,
            submitted_at=observed_at,
        )

        return DriftObservation(
            model_ref=self.model_ref,
            suite_hash=self.suite.suite_hash,
            suite_version=self.suite.version,
            cases_declared=declared,
            cases_ran=selected,
            sampling_reason=reason,
            coverage=coverage,
            baseline=baseline,
            bundle=bundle,
            comparison=compare_to_baseline(bundle, baseline),
            observed_at=observed_at,
        )

    def _select(self, declared: tuple[str, ...], case_ids: Sequence[str] | None) -> tuple[str, ...]:
        if case_ids is None:
            return declared
        requested = list(case_ids)
        if not requested:
            raise ValueError("case_ids must name at least one case, or be omitted to run the whole suite.")
        unknown = sorted(set(requested) - set(declared))
        if unknown:
            raise ValueError(f"case ids not in suite {self.suite.version!r}: {', '.join(unknown)}")
        chosen = set(requested)
        return tuple(case_id for case_id in declared if case_id in chosen)


def record_observation(
    chain: AuditChainStore,
    observation: DriftObservation,
    signer: DriftSignerProtocol,
    *,
    actor: str = "eval",
) -> tuple[DriftObservation, AuditEvent]:
    """Anchor *observation* in the chain and return the signed copy plus the event.

    The chain head is read and the record appended inside one chain
    transaction, so the head the signature commits to is the head the record
    is actually chained onto. Reading and appending as two steps would let a
    concurrent writer land in between, leaving a signed observation asserting
    a chain position it does not occupy.
    """
    with chain.chain_transaction():
        anchored = replace(observation, prev_chain_digest=chain.resync_head())
        signed = signer.sign(anchored)
        event = record_model_drift_observation(
            chain,
            observation_hash=signed.observation_hash(),
            model_provider=signed.model_ref.provider,
            model_requested=signed.model_ref.model_requested,
            model_reported=signed.model_ref.model_reported or "",
            suite_hash=signed.suite_hash,
            suite_version=signed.suite_version,
            baseline_hash=signed.comparison.baseline_hash,
            comparison_status=signed.comparison.status.value,
            aggregate_delta=signed.comparison.aggregate_delta,
            coverage=signed.coverage,
            cases_declared_count=len(signed.cases_declared),
            cases_ran_count=len(signed.cases_ran),
            sampling_reason=signed.sampling_reason,
            signer_fingerprint=signed.signer_fingerprint,
            actor=actor,
        )
    return signed, event


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------


class DriftVerificationStatus(Enum):
    MATCH = "MATCH"
    UNSIGNED = "UNSIGNED"
    COVERAGE_UNDECLARED = "COVERAGE_UNDECLARED"
    HASH_MISMATCH = "HASH_MISMATCH"
    DIVERGED = "DIVERGED"


@dataclass
class DriftVerificationResult:
    observation_hash: str
    status: DriftVerificationStatus
    detail: str = ""
    recomputed_comparison: DriftComparison | None = None

    @property
    def passed(self) -> bool:
        return self.status is DriftVerificationStatus.MATCH

    def report(self) -> str:
        lines = [
            f"observation : {self.observation_hash}",
            f"status      : {self.status.value}",
        ]
        if self.detail:
            lines.append(f"detail      : {self.detail}")
        return "\n".join(lines)


class DriftObservationVerifier:
    """Recompute an observation from the observation alone.

    No suite file, no baseline file, no database and no adapter: the
    observation embeds the bundle and the baseline it was compared against,
    which is what makes a disputed observation recomputable by anyone holding
    it. Re-deriving each case's *verdict* from its receipt is
    :class:`~bernstein.eval.bench.verifier.BenchVerifier`'s job and needs the
    suite; what is checked here is that the sealed delta and the declared
    coverage follow from the bundle the observation carries.
    """

    def __init__(self, trusted_keys: Mapping[str, bytes] | None = None) -> None:
        self._trusted_keys: dict[str, bytes] = dict(trusted_keys or {})

    def verify(self, observation: DriftObservation) -> DriftVerificationResult:
        signature_problem = self._check_signature(observation)
        if signature_problem:
            return self._result(observation, DriftVerificationStatus.UNSIGNED, signature_problem)

        coverage_problem = self._check_coverage(observation)
        if coverage_problem:
            return self._result(observation, DriftVerificationStatus.COVERAGE_UNDECLARED, coverage_problem)

        receipt_problem = self._check_receipts(observation)
        if receipt_problem:
            return self._result(observation, DriftVerificationStatus.HASH_MISMATCH, receipt_problem)

        recomputed = compare_to_baseline(observation.bundle, observation.baseline)
        if recomputed.to_dict() != observation.comparison.to_dict():
            return self._result(
                observation,
                DriftVerificationStatus.DIVERGED,
                self._first_comparison_divergence(observation.comparison, recomputed),
                recomputed=recomputed,
            )
        return self._result(observation, DriftVerificationStatus.MATCH, "", recomputed=recomputed)

    # -- checks ---------------------------------------------------------

    def _check_signature(self, observation: DriftObservation) -> str:
        if not observation.signature or not observation.signer_fingerprint:
            return "Observation is unsigned (signature or signer_fingerprint is empty)."
        if observation.signer_fingerprint == StubDriftSigner.fingerprint():
            expected = StubDriftSigner.expected_signature(observation)
            if not hmac_mod.compare_digest(observation.signature, expected):
                return "Stub signature does not verify against the observation hash."
            return ""
        public_pem = self._trusted_keys.get(observation.signer_fingerprint)
        if public_pem is None:
            return (
                f"Signer fingerprint {observation.signer_fingerprint!r} does not resolve "
                "to a trusted public key; an unverifiable signature is treated as unsigned."
            )
        from bernstein.core.security.agent_card_signer import verify_detached_jws_over_canonical

        if not verify_detached_jws_over_canonical(
            observation.observation_hash().encode(),
            observation.signature,
            public_pem,
            expected_typ=_DRIFT_JWS_TYP,
        ):
            return "Install-identity signature does not verify against the trusted public key."
        return ""

    @staticmethod
    def _check_coverage(observation: DriftObservation) -> str:
        declared = observation.cases_declared
        ran = observation.cases_ran
        if not ran:
            return "Observation records no cases as run."
        if len(set(ran)) != len(ran):
            return "cases_ran lists a case more than once."
        unknown = sorted(set(ran) - set(declared))
        if unknown:
            return f"cases_ran names cases the suite does not declare: {', '.join(unknown)}."
        if tuple(case for case in declared if case in set(ran)) != ran:
            return "cases_ran is not the declared suite order restricted to the cases that ran."

        expected_coverage = COVERAGE_FULL if ran == declared else COVERAGE_PARTIAL
        if observation.coverage != expected_coverage:
            return (
                f"Observation is labelled {observation.coverage!r} but ran {len(ran)} of "
                f"{len(declared)} declared cases, which is {expected_coverage!r}."
            )
        if expected_coverage == COVERAGE_PARTIAL and not observation.sampling_reason:
            return "A partial run carries no sampling_reason, so the sample reads as full coverage."
        if expected_coverage == COVERAGE_FULL and observation.sampling_reason:
            return "A full run carries a sampling_reason, which claims a sampling that did not happen."

        ran_in_bundle = tuple(tr.task_id for tr in observation.bundle.task_results)
        if ran_in_bundle != ran:
            return f"Bundle records cases {list(ran_in_bundle)} but the observation claims {list(ran)}."
        return ""

    @staticmethod
    def _check_receipts(observation: DriftObservation) -> str:
        for result in observation.bundle.task_results:
            if not result.receipt:
                return f"Case {result.task_id!r} carries no receipt; its score has no replay substrate."
            live = hashlib.sha256(_canonical_bytes(result.receipt)).hexdigest()
            if result.stored_receipt_hash != live:
                return (
                    f"Case {result.task_id!r} receipt hash mismatch: stored "
                    f"{result.stored_receipt_hash!r} != recomputed {live!r}."
                )
        return ""

    @staticmethod
    def _first_comparison_divergence(sealed: DriftComparison, recomputed: DriftComparison) -> str:
        if sealed.status is not recomputed.status:
            return f"Sealed comparison status {sealed.status.value!r} != recomputed {recomputed.status.value!r}."
        if sealed.baseline_hash != recomputed.baseline_hash:
            return f"Sealed baseline_hash {sealed.baseline_hash!r} != recomputed {recomputed.baseline_hash!r}."
        sealed_by_case = {cd.case_id: cd for cd in sealed.case_deltas}
        for case in recomputed.case_deltas:
            stored = sealed_by_case.get(case.case_id)
            if stored is None:
                return f"Case {case.case_id!r} is missing from the sealed comparison."
            if stored.to_dict() != case.to_dict():
                return (
                    f"Case {case.case_id!r} sealed delta {stored.delta} does not follow "
                    f"from the bundle, which recomputes to {case.delta}."
                )
        return (
            f"Sealed aggregate delta {sealed.aggregate_delta} != recomputed {recomputed.aggregate_delta}."
            if sealed.aggregate_delta != recomputed.aggregate_delta
            else "Sealed comparison does not equal the comparison recomputed from the bundle."
        )

    @staticmethod
    def _result(
        observation: DriftObservation,
        status: DriftVerificationStatus,
        detail: str,
        *,
        recomputed: DriftComparison | None = None,
    ) -> DriftVerificationResult:
        return DriftVerificationResult(
            observation_hash=observation.observation_hash(),
            status=status,
            detail=detail,
            recomputed_comparison=recomputed,
        )
