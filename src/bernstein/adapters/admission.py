"""Receipt-gated adapter admission with signed refusal receipts (#2610).

Promotes adapter resolution from name-based to proof-based. ``get_adapter``
historically mapped a string key to a class and handed back a live adapter:
an adapter whose conformance verdict was ``skip`` -- because it shipped no
contract, or because its binary was missing, or because the ``--help`` probe
was inconclusive -- spawned exactly like one that had passed every check.
A skip is not a pass, and it must not read as permission.

This module makes the admission decision an artefact. Every resolution of an
adapter in the spawn path derives, locally and deterministically, the evidence
the decision rests on -- the installed binary's version, the pinned contract's
content hash, the capability profile's content hash, the golden-transcript
replay outcome, and the nightly canary's attestation for that version -- folds
it into a content-addressed receipt, and anchors that receipt in the HMAC audit
chain.

Both paths are first class. A green admission seals an ``admit`` receipt naming
the capabilities the adapter is granted. A skipped, stale, or failing adapter
seals a *refusal* receipt naming the reason, the capabilities it is explicitly
denied, and the operator-visible remediation that would clear it. The refusal
is not the absence of a record; it is the record. An operator handed a chain
slice can prove which adapters held production authority during a window and
why every other one did not -- a question a log line that says "skipped"
cannot answer.

Killer shape: the refusal *is* the receipt. Strip the audit chain and the
deterministic replay fingerprint and this degrades to a conformance check with
a log line; with them, an admission is an offline-verifiable proof that the
adapter which ran was conformant against the exact binary version, and a
refusal is an equally verifiable proof that an unverified adapter never
received task context.

Lever: the replay fingerprint is a deterministic projection of
``(contract bytes, binary version, golden-transcript replay output)``, so two
operators on the same binary version derive byte-identical bytes and any drift
surfaces as a named hash divergence rather than as an unexplained behaviour
change. The receipt is meaningful only against that projection and the
HMAC-chained log it is anchored in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.security.path_containment import (
    PathContainmentError,
    PathTooLongError,
    contained_path,
    slug_identifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from bernstein.adapters.conformance import TranscriptResult

logger = logging.getLogger(__name__)

__all__ = [
    "ADMISSION_EXEMPT",
    "ADMISSION_SCHEMA_VERSION",
    "ADMISSION_TTL_SECONDS",
    "CANARY_GREEN",
    "CANARY_RED",
    "CANARY_STALE",
    "CANARY_UNKNOWN",
    "GATE_RECEIPT_KIND",
    "POLICY_ENFORCE",
    "POLICY_OFF",
    "POLICY_WARN",
    "REASON_CANARY_RED",
    "REASON_CONFORMANCE_FAIL",
    "REASON_CONFORMANCE_SKIP",
    "REASON_FINGERPRINT_MISMATCH",
    "REASON_NO_CONTRACT",
    "REASON_NO_RECEIPT",
    "REASON_NO_TRANSCRIPT",
    "REASON_RECEIPT_STALE",
    "REASON_RECEIPT_TAMPERED",
    "REASON_REPLAY_DIVERGED",
    "REASON_STORED_REFUSAL",
    "RECEIPT_KIND",
    "REMEDIATION",
    "VERDICT_ADMIT",
    "VERDICT_REFUSE",
    "AdapterAdmissionReceipt",
    "AdapterAdmissionRefusal",
    "AdmissionDecision",
    "AdmissionEvidence",
    "AdmissionGate",
    "anchor_admission_receipt_in_lineage",
    "audit_admission_no_unproven_spawn",
    "build_admission_receipt",
    "capability_split",
    "conformance_run_id",
    "evaluate_admission",
    "gate_decision",
    "gather_admission_evidence",
    "golden_transcripts_dir",
    "load_admission_receipt",
    "policy_from_env",
    "preflight_admission",
    "receipt_is_stale",
    "receipt_sha256",
    "replay_fingerprint",
    "seal_admission_receipt",
    "verify_admission_receipt",
    "write_admission_receipt",
]

#: Version stamped into every receipt preimage. Bump only on a wire-format
#: change so receipts sealed under an older shape stay verifiable.
ADMISSION_SCHEMA_VERSION = 1

#: ``kind`` discriminator on the admission receipt the gate consults. Sealed
#: by a conformance run (``bernstein adapters verify --seal``).
RECEIPT_KIND = "adapter.admission_receipt"

#: ``kind`` discriminator on the receipt the gate itself seals for one
#: resolution attempt. Distinct from :data:`RECEIPT_KIND` so a gate decision
#: can never be mistaken for the conformance evidence it was derived against.
GATE_RECEIPT_KIND = "adapter.admission_gate_receipt"

#: The adapter presented conformant evidence and is granted spawn authority.
VERDICT_ADMIT = "admit"

#: The adapter is skipped, stale, or failing: spawn authority is withheld and
#: this receipt is the proof of why.
VERDICT_REFUSE = "refuse"

# -- refusal reasons --------------------------------------------------------

#: No contract YAML pins the adapter's invocation surface.
REASON_NO_CONTRACT = "no_contract"

#: No golden transcript replays the adapter's spawn path.
REASON_NO_TRANSCRIPT = "no_transcript"

#: The in-process conformance probe returned ``skip`` -- inconclusive, not
#: passing. This is the case the module exists for: a skip is not permission.
REASON_CONFORMANCE_SKIP = "conformance_skip"

#: The in-process conformance probe returned ``fail``: the installed binary no
#: longer advertises the surface the adapter depends on.
REASON_CONFORMANCE_FAIL = "conformance_fail"

#: A golden transcript replayed against the adapter and diverged.
REASON_REPLAY_DIVERGED = "replay_diverged"

#: The nightly canary's attestation for the installed version is red.
REASON_CANARY_RED = "canary_red"

#: Live evidence is green but no sealed admission receipt exists on disk.
REASON_NO_RECEIPT = "no_receipt"

#: The sealed receipt is older than its admission TTL.
REASON_RECEIPT_STALE = "receipt_stale"

#: The sealed receipt's body no longer hashes to its recorded identity.
REASON_RECEIPT_TAMPERED = "receipt_tampered"

#: The sealed receipt pins a different replay fingerprint than the one derived
#: now: the binary, the contract, or the transcript moved under the receipt.
REASON_FINGERPRINT_MISMATCH = "fingerprint_mismatch"

#: The sealed receipt itself records a refusal.
REASON_STORED_REFUSAL = "stored_refusal"

#: Operator-visible remediation per refusal reason. Every refusal names a
#: concrete next action, so a withheld adapter is an actionable finding rather
#: than a dead end. ``{adapter}`` is substituted at receipt-build time.
REMEDIATION: dict[str, str] = {
    REASON_NO_CONTRACT: (
        "Add tests/contract/contracts/{adapter}.yaml pinning the flags and "
        "subcommands the adapter passes, then re-run "
        "`bernstein adapters verify {adapter} --seal`."
    ),
    REASON_NO_TRANSCRIPT: (
        "Add tests/golden/{adapter}_adapter.yaml replaying the adapter's spawn "
        "path under a mocked subprocess, then re-run "
        "`bernstein adapters verify {adapter} --seal`."
    ),
    REASON_CONFORMANCE_SKIP: (
        "Install the adapter's binary and confirm `bernstein adapters check "
        "{adapter}` reports conformance=ok, then re-run "
        "`bernstein adapters verify {adapter} --seal`."
    ),
    REASON_CONFORMANCE_FAIL: (
        "The installed binary no longer advertises the contract's required "
        "surface. Reconcile tests/contract/contracts/{adapter}.yaml with "
        "`bernstein adapters check {adapter}`, then re-seal."
    ),
    REASON_REPLAY_DIVERGED: (
        "The golden transcript for {adapter} no longer replays clean. Run the "
        "conformance suite and reconcile tests/golden/{adapter}_adapter.yaml "
        "with the adapter's spawn path."
    ),
    REASON_CANARY_RED: (
        "The nightly canary attestation for the installed {adapter} version is "
        "red. Pin a known-good upstream version or fix the contract drift the "
        "canary reported, then re-seal."
    ),
    REASON_NO_RECEIPT: (
        "No sealed admission receipt for {adapter}. Run "
        "`bernstein adapters verify {adapter} --seal` to derive and anchor one."
    ),
    REASON_RECEIPT_STALE: (
        "The admission receipt for {adapter} is past its TTL. Re-run "
        "`bernstein adapters verify {adapter} --seal` to refresh it."
    ),
    REASON_RECEIPT_TAMPERED: (
        "The admission receipt for {adapter} does not hash to its recorded "
        "identity. Discard it and re-run "
        "`bernstein adapters verify {adapter} --seal`."
    ),
    REASON_FINGERPRINT_MISMATCH: (
        "The installed {adapter} binary, its contract, or its golden "
        "transcript changed after the receipt was sealed. Re-run "
        "`bernstein adapters verify {adapter} --seal` against the current "
        "surface."
    ),
    REASON_STORED_REFUSAL: (
        "The sealed admission receipt for {adapter} records a refusal. Clear "
        "the underlying conformance finding, then re-seal."
    ),
}

#: How long a sealed admission receipt stays fresh. One nightly-canary cycle:
#: past it, the attestation the receipt rests on is no longer evidence that
#: the installed surface still conforms.
ADMISSION_TTL_SECONDS = 24 * 60 * 60

# -- canary attestation states ---------------------------------------------

#: A last-green row attests the installed version.
CANARY_GREEN = "green"

#: A last-green row exists but attests a different version than the installed
#: one, so it is not evidence about what is on PATH now.
CANARY_STALE = "stale"

#: The canary recorded a failure for this adapter.
CANARY_RED = "red"

#: No canary attestation exists for this adapter at all.
CANARY_UNKNOWN = "unknown"

# -- enforcement policy -----------------------------------------------------

#: Refuse an adapter that cannot present a fresh, matching admission receipt.
POLICY_ENFORCE = "enforce"

#: Seal and anchor the refusal receipt but let the spawn proceed. The default:
#: the gate becomes observable before it becomes blocking, so an operator sees
#: exactly which adapters would be refused before flipping to enforce.
POLICY_WARN = "warn"

#: Disable the gate entirely (offline work, air-gapped hosts).
POLICY_OFF = "off"

_POLICY_ENV = "BERNSTEIN_ADAPTER_ADMISSION_POLICY"
_ENFORCE_TOKENS = frozenset({"enforce", "block", "strict"})
_OFF_TOKENS = frozenset({"off", "0", "false", "disabled", "none"})

#: Registry names the gate never withholds. ``mock`` is the test-only stub:
#: it does not wrap a pinned upstream surface so it has no conformance evidence
#: to present. This is the explicit dev/offline escape hatch -- the gate must
#: never block work against ``mock``.
ADMISSION_EXEMPT: frozenset[str] = frozenset({"mock"})

#: Env override for the golden-transcript directory, for an operator pinning a
#: tree of their own ahead of both layouts below.
_GOLDEN_DIR_ENV = "BERNSTEIN_ADAPTER_GOLDEN_DIR"

#: Golden transcripts as laid out in a development checkout.
_GOLDEN_DIR_DEFAULT = Path(__file__).parents[3] / "tests" / "golden"

#: Golden transcripts as shipped in the wheel. Force-included into the package
#: tree at build time (see ``[tool.hatch.build.targets.wheel.force-include]``
#: in ``pyproject.toml``) so a pip install can replay the spawn path instead of
#: refusing every adapter with :data:`REASON_NO_TRANSCRIPT` (issue #3547).
_GOLDEN_DIR_PACKAGED = Path(__file__).resolve().parents[1] / "_default_templates" / "adapter_golden"


class AdapterAdmissionRefusal(RuntimeError):
    """Raised when an adapter is refused admission under the enforce policy.

    Carries the sealed refusal receipt and its content hash so the caller
    surfaces the proof artefact, not just an error string. A plain
    :class:`RuntimeError` subclass -- deliberately *not* a
    :class:`~bernstein.adapters.base.SpawnError` -- so the spawner's
    per-provider failover loop never swallows it into an alternate-provider
    retry, mirroring :class:`~bernstein.adapters.security_floor.
    AdapterSecurityFloorRefusal`. An unverified adapter is a hard stop.
    """

    def __init__(self, message: str, *, receipt: dict[str, Any], receipt_sha256: str) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.receipt_sha256 = receipt_sha256


# ---------------------------------------------------------------------------
# Canonical JSON helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def policy_from_env(env: dict[str, str] | None = None) -> str:
    """Resolve the admission policy from the environment.

    Warn-by-default: the gate records every decision from the first run, and an
    operator opts in to blocking with
    ``BERNSTEIN_ADAPTER_ADMISSION_POLICY=enforce`` once the receipts for their
    adapter set are sealed. ``=off`` disables the gate.
    """
    source = env if env is not None else os.environ
    raw = (source.get(_POLICY_ENV) or "").strip().lower()
    if raw in _ENFORCE_TOKENS:
        return POLICY_ENFORCE
    if raw in _OFF_TOKENS:
        return POLICY_OFF
    return POLICY_WARN


def golden_transcripts_dir(explicit: Path | None = None) -> Path | None:
    """Resolve the golden-transcript directory, or ``None`` when absent.

    Order: an explicit argument, then :data:`_GOLDEN_DIR_ENV`, then the
    development-checkout layout, then the copy bundled in the wheel. ``None``
    means no transcript tree is reachable at all, which surfaces as a
    :data:`REASON_NO_TRANSCRIPT` refusal rather than as silent permission.
    """
    if explicit is not None:
        return explicit if explicit.exists() else None
    env_dir = (os.environ.get(_GOLDEN_DIR_ENV) or "").strip()
    if env_dir:
        candidate = Path(env_dir)
        return candidate if candidate.exists() else None
    if _GOLDEN_DIR_DEFAULT.exists():
        return _GOLDEN_DIR_DEFAULT
    return _GOLDEN_DIR_PACKAGED if _GOLDEN_DIR_PACKAGED.exists() else None


# ---------------------------------------------------------------------------
# Deterministic fingerprints
# ---------------------------------------------------------------------------


def replay_fingerprint(
    adapter: str,
    *,
    contract_hash: str,
    installed_version: str | None,
    results: Sequence[TranscriptResult],
) -> str:
    """Content hash over one adapter's golden-transcript replay output.

    The preimage folds the pinned contract's content hash, the installed
    binary version, and the per-step pass/fail shape of every transcript that
    targets this adapter -- and nothing else. Free-text step messages are
    deliberately excluded: they can embed a temporary working directory, which
    would make the fingerprint host-dependent and destroy the property the
    fingerprint exists for.

    Determinism: two operators replaying the same transcripts against the same
    binary version derive byte-identical bytes and therefore an identical
    fingerprint. Changing one contract token, or the binary version, or a
    transcript's outcome changes it -- so drift is a named hash divergence
    rather than an unexplained behaviour change.

    Args:
        adapter: Adapter registry key.
        contract_hash: SHA-256 of the contract YAML bytes, or ``""``.
        installed_version: Probed upstream version, or ``None``.
        results: Replay results for the transcripts targeting this adapter.

    Returns:
        The fingerprint, prefixed ``sha256:``.
    """
    payload = {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "adapter": adapter,
        "contract_hash": contract_hash,
        "installed_version": installed_version or "",
        "results": [
            {
                "transcript_name": result.transcript_name,
                "adapter_class": result.adapter_class,
                "passed": result.passed,
                "steps": [{"step_index": step.step_index, "passed": step.passed} for step in result.step_results],
            }
            for result in sorted(results, key=lambda r: r.transcript_name)
        ],
    }
    return "sha256:" + _sha256_hex(_canonical_bytes(payload))


def conformance_run_id(
    adapter: str,
    *,
    contract_hash: str,
    profile_hash: str,
    fingerprint: str,
    conformance_verdict: str,
) -> str:
    """Deterministic identifier for the conformance run behind a receipt.

    A pure function of the evidence, not a random id: two runs that observed
    the same surface name the same run, so "which conformance run admitted
    this adapter" is answerable offline from the receipt alone.
    """
    return _sha256_hex(
        _canonical_bytes(
            {
                "schema_version": ADMISSION_SCHEMA_VERSION,
                "adapter": adapter,
                "contract_hash": contract_hash,
                "profile_hash": profile_hash,
                "replay_fingerprint": fingerprint,
                "conformance_verdict": conformance_verdict,
            }
        )
    )[:32]


def _probe_hash(binary: str, binary_path: str | None, installed_version: str | None) -> str:
    """Content hash binding the probed binary identity into the receipt."""
    return "sha256:" + _sha256_hex(
        _canonical_bytes(
            {
                "binary": binary,
                "binary_path": binary_path or "",
                "installed_version": installed_version or "",
            }
        )
    )


# ---------------------------------------------------------------------------
# Capability split
# ---------------------------------------------------------------------------


def capability_split(adapter: str, *, admitted: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(allowed, forbidden)`` capability axes for a decision.

    On admission the split is the adapter's own declaration: the boolean
    capability axes its profile sets are allowed, the rest are forbidden. An
    adapter with no profile declares nothing, so it is granted nothing beyond
    a plain spawn.

    On refusal every axis is forbidden and none is allowed, including the
    ``spawn`` pseudo-axis. That is the whole point of writing the negative
    path down: a refusal receipt states positively that no authority was
    granted, so an absent admission can never be read as an implicit one.

    Args:
        adapter: Adapter registry key.
        admitted: Whether the decision grants spawn authority.

    Returns:
        ``(allowed, forbidden)``, each sorted for a deterministic receipt.
    """
    from bernstein.adapters.capability_profile import BOOLEAN_CAPABILITIES, PROFILES

    axes = ("spawn", *BOOLEAN_CAPABILITIES)
    if not admitted:
        return (), tuple(sorted(axes))

    profile = PROFILES.get(adapter)
    allowed = {"spawn"}
    if profile is not None:
        allowed.update(axis for axis in BOOLEAN_CAPABILITIES if bool(profile.capability_value(axis)))
    forbidden = set(axes) - allowed
    return tuple(sorted(allowed)), tuple(sorted(forbidden))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionEvidence:
    """The locally re-derivable inputs one admission decision rests on.

    Every field is either read off disk (contract bytes, transcripts, the
    packaged last-green projection) or probed from the host (binary path and
    version), so a second operator with the same tree and the same installed
    binary reconstructs the identical evidence -- and therefore the identical
    fingerprint.

    Attributes:
        adapter: Adapter registry key.
        binary: Binary name the contract pins.
        binary_path: Absolute path on ``PATH``, or ``None`` when missing.
        installed_version: Captured ``<binary> --version`` first line, or
            ``None``.
        contract_hash: SHA-256 of the contract YAML bytes, ``""`` when absent.
        profile_hash: Content address of the capability profile, ``""`` when
            the adapter declares none.
        conformance_verdict: ``ok`` / ``fail`` / ``skip`` from the in-process
            probe.
        conformance_detail: Human-readable reason; empty on ``ok``.
        transcript_names: Golden transcripts that target this adapter.
        replay_passed: Whether every transcript replayed clean; ``None`` when
            no transcript targets the adapter.
        canary_verdict: :data:`CANARY_GREEN` / :data:`CANARY_STALE` /
            :data:`CANARY_RED` / :data:`CANARY_UNKNOWN`.
        replay_fingerprint: Deterministic projection of the evidence.
    """

    adapter: str
    binary: str
    binary_path: str | None
    installed_version: str | None
    contract_hash: str
    profile_hash: str
    conformance_verdict: str
    conformance_detail: str
    transcript_names: tuple[str, ...] = ()
    replay_passed: bool | None = None
    canary_verdict: str = CANARY_UNKNOWN
    replay_fingerprint: str = ""

    @property
    def probe_hash(self) -> str:
        """Content hash binding the probed binary identity."""
        return _probe_hash(self.binary, self.binary_path, self.installed_version)


def _transcripts_for(adapter: str, golden_dir: Path) -> list[Any]:
    """Golden transcripts whose adapter class resolves to ``adapter``."""
    from bernstein.adapters.conformance import (  # pyright: ignore[reportPrivateUsage]
        _load_adapter,
        load_golden_transcripts,
    )
    from bernstein.adapters.registry import registry_name_for

    selected: list[Any] = []
    for transcript in load_golden_transcripts(golden_dir):
        try:
            instance = _load_adapter(transcript.adapter_class, transcript.ctor_kwargs or None)
        except Exception as exc:
            logger.debug("admission: transcript %s not instantiable (%s)", transcript.name, type(exc).__name__)
            continue
        if registry_name_for(instance) == adapter:
            selected.append(transcript)
    return selected


def _canary_verdict_for(adapter: str, installed_version: str | None, last_green_path: Path | None) -> str:
    """Read the nightly canary's attestation for the installed version.

    PEP 440-equivalent spellings such as 1.0 and 1.0.0 intentionally identify
    the same attested release.
    """
    from packaging.version import InvalidVersion, Version

    from .base import VERSION_TOKEN_RE
    from .canary import load_last_green

    entries = load_last_green(last_green_path)
    entry = entries.get(adapter)
    if entry is None:
        return CANARY_UNKNOWN
    if installed_version is None:
        return CANARY_STALE

    match = VERSION_TOKEN_RE.search(installed_version)
    if not entry.version or match is None:
        return CANARY_STALE

    probed_version = match.group(0)
    try:
        versions_match = Version(entry.version) == Version(probed_version)
    except InvalidVersion:
        versions_match = entry.version == probed_version
    return CANARY_GREEN if versions_match else CANARY_STALE


def gather_admission_evidence(
    adapter: str,
    *,
    contracts_dir: Path | None = None,
    golden_dir: Path | None = None,
    last_green_path: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    version_probe: Callable[[str], str | None] | None = None,
    canary_verdict: str | None = None,
) -> AdmissionEvidence:
    """Derive every input one admission decision rests on.

    Side effects are limited to ``shutil.which``, a single
    ``<binary> --version`` subprocess, and golden-transcript replay under
    mocked subprocesses -- the same surface ``bernstein adapters check``
    already touches. No agent is spawned.

    Args:
        adapter: Adapter registry key.
        contracts_dir: Override for the packaged contracts directory.
        golden_dir: Override for the golden-transcript directory.
        last_green_path: Override for the packaged last-green projection.
        which: ``shutil.which``-shaped resolver; tests inject stubs.
        version_probe: ``--version`` probe; tests inject stubs.
        canary_verdict: Pre-computed canary attestation; when omitted it is
            read from the last-green projection.

    Returns:
        The populated :class:`AdmissionEvidence`, fingerprint included.
    """
    import tempfile

    from bernstein.adapters.capability_profile import profile_hash_for
    from bernstein.adapters.report import (
        _binary_for_adapter,  # pyright: ignore[reportPrivateUsage]
        _capture_version,  # pyright: ignore[reportPrivateUsage]
        _contract_hash,  # pyright: ignore[reportPrivateUsage]
        check_adapter_in_process,
    )

    resolver = which if which is not None else shutil.which
    probe = version_probe if version_probe is not None else _capture_version

    binary = _binary_for_adapter(adapter)
    binary_path = resolver(binary) if binary else None
    installed_version = probe(binary) if binary_path else None

    contract_hash = _contract_hash(adapter, contracts_dir=contracts_dir)
    payload = check_adapter_in_process(adapter, binary_resolved=binary_path, contracts_dir=contracts_dir)

    resolved_golden = golden_transcripts_dir(golden_dir)
    transcripts = _transcripts_for(adapter, resolved_golden) if resolved_golden is not None else []
    results: list[TranscriptResult] = []
    if transcripts:
        from bernstein.adapters.conformance import ConformanceHarness

        harness = ConformanceHarness()
        with tempfile.TemporaryDirectory() as tmp:
            results = [harness.replay_transcript(t, workdir=Path(tmp)) for t in transcripts]

    replay_passed = all(r.passed for r in results) if results else None
    fingerprint = replay_fingerprint(
        adapter,
        contract_hash=contract_hash,
        installed_version=installed_version,
        results=results,
    )

    verdict = (
        canary_verdict
        if canary_verdict is not None
        else _canary_verdict_for(adapter, installed_version, last_green_path)
    )

    return AdmissionEvidence(
        adapter=adapter,
        binary=binary,
        binary_path=binary_path,
        installed_version=installed_version,
        contract_hash=contract_hash,
        profile_hash=profile_hash_for(adapter) or "",
        conformance_verdict=str(payload.verdict),
        conformance_detail=str(payload.detail or ""),
        transcript_names=tuple(sorted(t.name for t in transcripts)),
        replay_passed=replay_passed,
        canary_verdict=verdict,
        replay_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionDecision:
    """The admission verdict for one adapter, plus the evidence behind it.

    Attributes:
        evidence: The inputs the verdict is a pure function of.
        verdict: :data:`VERDICT_ADMIT` or :data:`VERDICT_REFUSE`.
        reason: Empty on admission; a refusal-reason constant otherwise.
        ttl_seconds: How long a sealed receipt for this decision stays fresh.
        allowed_capabilities: Axes this decision grants.
        forbidden_capabilities: Axes this decision explicitly withholds.
    """

    evidence: AdmissionEvidence
    verdict: str
    reason: str = ""
    ttl_seconds: int = ADMISSION_TTL_SECONDS
    allowed_capabilities: tuple[str, ...] = ()
    forbidden_capabilities: tuple[str, ...] = ()

    @property
    def admitted(self) -> bool:
        """True when the decision grants spawn authority."""
        return self.verdict == VERDICT_ADMIT

    @property
    def remediation(self) -> str:
        """Operator-visible next action; empty on admission."""
        if self.admitted or not self.reason:
            return ""
        template = REMEDIATION.get(self.reason, "Re-run `bernstein adapters verify {adapter} --seal`.")
        return template.format(adapter=self.evidence.adapter)


def _decision(evidence: AdmissionEvidence, verdict: str, reason: str, ttl_seconds: int) -> AdmissionDecision:
    allowed, forbidden = capability_split(evidence.adapter, admitted=verdict == VERDICT_ADMIT)
    return AdmissionDecision(
        evidence=evidence,
        verdict=verdict,
        reason=reason,
        ttl_seconds=ttl_seconds,
        allowed_capabilities=allowed,
        forbidden_capabilities=forbidden,
    )


def evaluate_admission(
    evidence: AdmissionEvidence,
    *,
    ttl_seconds: int = ADMISSION_TTL_SECONDS,
) -> AdmissionDecision:
    """Decide admit / refuse from evidence alone. Pure and deterministic.

    The order of checks is the order an operator would investigate in: is the
    surface pinned at all, does the pinned surface still hold, does the spawn
    path still replay, and does the nightly matrix agree. The first failing
    check names the reason, so a refusal points at one actionable thing rather
    than at a list.

    A ``skip`` conformance verdict refuses. That is the behaviour change this
    module exists for: an inconclusive probe is not evidence of conformance,
    and treating it as one is how "unverified" quietly became "trusted".
    """
    if not evidence.contract_hash:
        return _decision(evidence, VERDICT_REFUSE, REASON_NO_CONTRACT, ttl_seconds)
    if evidence.conformance_verdict == "fail":
        return _decision(evidence, VERDICT_REFUSE, REASON_CONFORMANCE_FAIL, ttl_seconds)
    if evidence.conformance_verdict != "ok":
        return _decision(evidence, VERDICT_REFUSE, REASON_CONFORMANCE_SKIP, ttl_seconds)
    if evidence.replay_passed is None:
        return _decision(evidence, VERDICT_REFUSE, REASON_NO_TRANSCRIPT, ttl_seconds)
    if not evidence.replay_passed:
        return _decision(evidence, VERDICT_REFUSE, REASON_REPLAY_DIVERGED, ttl_seconds)
    if evidence.canary_verdict == CANARY_RED:
        return _decision(evidence, VERDICT_REFUSE, REASON_CANARY_RED, ttl_seconds)
    return _decision(evidence, VERDICT_ADMIT, "", ttl_seconds)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterAdmissionReceipt:
    """Typed view over a sealed admission receipt body.

    The canonical artefact on disk and in the chain is the mapping returned by
    :func:`build_admission_receipt`; this class is the ergonomic reader for it,
    so callers do not index string keys by hand.
    """

    body: dict[str, Any] = field(default_factory=dict)

    @property
    def adapter(self) -> str:
        """Adapter registry key the receipt covers."""
        return str(self.body.get("adapter") or "")

    @property
    def verdict(self) -> str:
        """:data:`VERDICT_ADMIT` or :data:`VERDICT_REFUSE`."""
        return str(self.body.get("verdict") or "")

    @property
    def admitted(self) -> bool:
        """True when the receipt grants spawn authority."""
        return self.verdict == VERDICT_ADMIT

    @property
    def reason(self) -> str:
        """Refusal reason; empty on an admission receipt."""
        return str(self.body.get("reason") or "")

    @property
    def replay_fingerprint(self) -> str:
        """The deterministic fingerprint the receipt pins."""
        return str(self.body.get("replay_fingerprint") or "")

    @property
    def generated_at(self) -> str:
        """ISO-8601 timestamp stamped into the preimage."""
        return str(self.body.get("generated_at") or "")

    @property
    def ttl_seconds(self) -> int:
        """Freshness window in seconds."""
        try:
            return int(self.body.get("admission_ttl_seconds", ADMISSION_TTL_SECONDS))
        except (TypeError, ValueError):
            return ADMISSION_TTL_SECONDS

    @property
    def sha256(self) -> str:
        """Content hash (identity) of the receipt body."""
        return receipt_sha256(self.body)


def build_admission_receipt(
    decision: AdmissionDecision,
    *,
    generated_at: str,
    kind: str = RECEIPT_KIND,
) -> dict[str, Any]:
    """Bind a decision into a canonical receipt mapping.

    The field set is deliberately symmetric across admit and refuse: a refusal
    carries the same binary identity, contract version, conformance run id and
    capability split an admission does, plus the reason and the remediation.
    A verifier reading a refusal knows exactly as much about what happened as
    one reading an admission -- which is what stops a withheld adapter from
    looking like an unexamined one.

    Determinism: a pure function of the decision and the caller-supplied
    timestamp, so two operators deriving the same evidence at the same
    timestamp produce byte-identical receipts and therefore the same
    :func:`receipt_sha256` identity.
    """
    evidence = decision.evidence
    return {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "kind": kind,
        "adapter": evidence.adapter,
        "binary": evidence.binary,
        "binary_path": evidence.binary_path,
        "installed_version": evidence.installed_version,
        "probe_hash": evidence.probe_hash,
        "contract_hash": evidence.contract_hash,
        "profile_hash": evidence.profile_hash,
        "replay_fingerprint": evidence.replay_fingerprint,
        "conformance_run_id": conformance_run_id(
            evidence.adapter,
            contract_hash=evidence.contract_hash,
            profile_hash=evidence.profile_hash,
            fingerprint=evidence.replay_fingerprint,
            conformance_verdict=evidence.conformance_verdict,
        ),
        "conformance_verdict": evidence.conformance_verdict,
        "conformance_detail": evidence.conformance_detail,
        "transcripts": list(evidence.transcript_names),
        "canary_verdict": evidence.canary_verdict,
        "verdict": decision.verdict,
        "reason": decision.reason,
        "allowed_capabilities": list(decision.allowed_capabilities),
        "forbidden_capabilities": list(decision.forbidden_capabilities),
        "admission_ttl_seconds": decision.ttl_seconds,
        "remediation": decision.remediation,
        "generated_at": generated_at,
    }


def receipt_sha256(receipt: dict[str, Any]) -> str:
    """Content hash (identity) of a receipt's canonical bytes."""
    return _sha256_hex(_canonical_bytes(receipt))


def verify_admission_receipt(doc: dict[str, Any]) -> bool:
    """Check a written receipt document against its embedded content hash.

    A mutated receipt body no longer hashes to the recorded identity, so
    tampering is detected offline with no key material required. The HMAC
    audit-chain mirror additionally binds the hash into the chain, and the
    optional lineage anchor signs it.
    """
    receipt = doc.get("receipt")
    recorded = doc.get("receipt_sha256")
    if not isinstance(receipt, dict) or not isinstance(recorded, str):
        return False
    return receipt_sha256(receipt) == recorded


def _parse_generated_at(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def receipt_is_stale(receipt: dict[str, Any], *, now: datetime) -> bool:
    """Return whether a sealed receipt is past its admission TTL.

    Time is injected so the decision is deterministic and testable. An
    unparseable timestamp is treated as stale (fail-visible), mirroring
    :func:`bernstein.adapters.canary.last_green_is_stale`.
    """
    view = AdapterAdmissionReceipt(receipt)
    generated = _parse_generated_at(view.generated_at)
    if generated is None:
        return True
    return now > generated + timedelta(seconds=view.ttl_seconds)


def write_admission_receipt(base_dir: Path, receipt: dict[str, Any]) -> Path:
    """Write a receipt under its content-addressed name.

    The filename embeds the adapter name and the receipt hash prefix. The
    adapter component is validated against a strict allow-pattern and the
    resolved path is containment-checked against ``base_dir``, so a hostile
    adapter string can never escape the receipts directory.

    Raises:
        ValueError: The adapter name fails validation, or the resolved path
            escapes ``base_dir``.
    """
    adapter = str(receipt.get("adapter") or "")
    adapter_key = slug_identifier(adapter, label="adapter name")
    sha = receipt_sha256(receipt)
    base_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{adapter_key}-{sha[:16]}"
    try:
        candidate = contained_path(base_dir, f"{stem}.json", label="receipt path")
        # The temporary sibling is opened *before* the rename, so it needs the
        # same proof as the final name. A pre-placed symlink at the .tmp path
        # otherwise captures the write and lands the receipt body outside the
        # receipts directory, then the rename seals the link into place.
        tmp = contained_path(base_dir, f"{stem}.json.tmp", label="receipt temp path")
    except PathTooLongError as exc:
        raise ValueError(f"receipt filename is too long for this filesystem: {stem}.json") from exc
    except PathContainmentError as exc:
        raise ValueError("receipt path escapes the receipts directory") from exc
    doc = {"receipt": receipt, "receipt_sha256": sha}
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(candidate)
    return candidate


def load_admission_receipt(base_dir: Path, adapter: str) -> tuple[dict[str, Any] | None, str]:
    """Load the newest sealed admission receipt for ``adapter``.

    Only receipts whose ``kind`` is :data:`RECEIPT_KIND` are considered, so a
    gate-decision receipt can never be mistaken for the conformance evidence
    the gate is supposed to check against.

    Args:
        base_dir: Directory sealed receipts are written to.
        adapter: Adapter registry key.

    Returns:
        ``(receipt, problem)``. ``problem`` is ``""`` when a well-formed
        receipt was found, :data:`REASON_NO_RECEIPT` when none exists, or
        :data:`REASON_RECEIPT_TAMPERED` when every candidate failed its
        content-hash check.
    """
    try:
        adapter_key = slug_identifier(adapter, label="adapter name")
    except PathContainmentError:
        return None, REASON_NO_RECEIPT
    if not base_dir.exists():
        return None, REASON_NO_RECEIPT

    candidates: list[dict[str, Any]] = []
    tampered = False
    for path in sorted(base_dir.glob(f"{adapter_key}-*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        body = doc.get("receipt")
        if not isinstance(body, dict) or body.get("kind") != RECEIPT_KIND:
            continue
        # Match on the unmodified adapter name, not the slug: the slug is a
        # filename concern only, the body keeps the operator-visible name.
        if str(body.get("adapter") or "").lower() != adapter.lower():
            continue
        if not verify_admission_receipt(doc):
            tampered = True
            continue
        candidates.append(body)

    if not candidates:
        return None, (REASON_RECEIPT_TAMPERED if tampered else REASON_NO_RECEIPT)
    candidates.sort(key=lambda body: str(body.get("generated_at") or ""))
    return candidates[-1], ""


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def _anchor_in_chain(chain: Any, receipt: dict[str, Any], sha: str) -> None:
    """Mirror one receipt into the HMAC audit chain. Never raises."""
    try:
        from bernstein.core.security.audit_chain import record_adapter_admission_receipt

        view = AdapterAdmissionReceipt(receipt)
        record_adapter_admission_receipt(
            chain=chain,
            adapter=view.adapter,
            binary=str(receipt.get("binary") or ""),
            installed_version=receipt.get("installed_version"),
            contract_hash=str(receipt.get("contract_hash") or ""),
            replay_fingerprint=view.replay_fingerprint,
            conformance_run_id=str(receipt.get("conformance_run_id") or ""),
            verdict=view.verdict,
            reason=view.reason,
            allowed_capabilities=list(receipt.get("allowed_capabilities") or []),
            forbidden_capabilities=list(receipt.get("forbidden_capabilities") or []),
            receipt_sha256=sha,
            kind=str(receipt.get("kind") or RECEIPT_KIND),
        )
    except Exception as exc:  # the mirror must never mask the admission decision
        logger.warning(
            "Could not mirror %s for %s: %s",
            receipt.get("kind"),
            receipt.get("adapter"),
            type(exc).__name__,
        )


def anchor_admission_receipt_in_lineage(
    receipt: dict[str, Any],
    *,
    store: Any,
    operator_hmac_key: bytes,
    agent_id: str,
    agent_card: Any,
    private_key_pem: str,
    ts_ns: int | None = None,
) -> str:
    """Seal one admission receipt as a signed lineage entry. Returns its hash.

    Anchors the receipt the way every other Bernstein receipt subsystem does
    -- through :func:`bernstein.core.lineage.signed_write.seal_write` -- so the
    receipt carries an Ed25519 detached-JWS signature and an operator-HMAC
    envelope on top of its content address. The artefact path is derived from
    the adapter key and the fingerprint, so successive receipts for one
    adapter chain to each other and a replaced receipt is a visible fork
    rather than an untraceable overwrite.

    ``span_id`` is repurposed to carry the receipt's content hash so the
    identity is signed along with the rest of the entry; the seal path does
    not consume ``span_id`` for telemetry, mirroring the datasource query
    receipt.

    Args:
        receipt: The canonical receipt mapping.
        store: A ``LineageStore`` accepting the entry.
        operator_hmac_key: Operator HMAC key for the envelope.
        agent_id: Bernstein agent slug recording the entry.
        agent_card: Agent Card carrying the verifying public key.
        private_key_pem: PEM-encoded Ed25519 private key.
        ts_ns: Optional deterministic entry timestamp.

    Returns:
        The lineage entry hash.
    """
    from bernstein.core.lineage.signed_write import seal_write

    view = AdapterAdmissionReceipt(receipt)
    sha = view.sha256
    canonical = _canonical_bytes(receipt)
    return seal_write(
        store,
        operator_hmac_key,
        artefact_path=f".sdd/adapters/admission/{view.adapter}.json",
        new_content=canonical,
        agent_id=agent_id,
        agent_card=agent_card,
        private_key_pem=private_key_pem,
        tool_call_id=f"adapter-admission:{view.adapter}",
        span_id=sha,
        artefact_kind="report",
        ts_ns=ts_ns,
    )


def seal_admission_receipt(
    decision: AdmissionDecision,
    *,
    receipts_dir: Path,
    generated_at: str,
    chain: Any | None = None,
) -> tuple[dict[str, Any], str, Path]:
    """Seal, write and anchor the admission receipt for a conformance run.

    This is the positive-and-negative emit path: a passing conformance run
    seals an ``admit`` receipt, a skipped or failing one seals a ``refuse``
    receipt, and both are written and anchored. The gate later reads whichever
    one is on disk, so the record the operator can inspect is exactly the
    record the gate acts on.

    Returns:
        ``(receipt, receipt_sha256, path)``.
    """
    receipt = build_admission_receipt(decision, generated_at=generated_at, kind=RECEIPT_KIND)
    sha = receipt_sha256(receipt)
    path = write_admission_receipt(receipts_dir, receipt)
    if chain is not None:
        _anchor_in_chain(chain, receipt, sha)
    return receipt, sha, path


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionGate:
    """Receipt-gated admission check for one resolution attempt.

    Constructed by the spawn path and handed to
    :func:`bernstein.adapters.registry.get_adapter`, so an adapter resolves
    only when it can present a fresh, matching admission receipt. Passing no
    gate leaves resolution unchanged, which keeps the enumeration surfaces
    (``bernstein adapters list``, the conformance report, doctor) free to
    inspect adapters they would never be allowed to spawn.

    Attributes:
        receipts_dir: Where sealed admission receipts live.
        chain: An ``AuditChainStore`` the gate decision is anchored into, or
            ``None`` to skip anchoring.
        policy: :data:`POLICY_WARN` (default), :data:`POLICY_ENFORCE`, or
            :data:`POLICY_OFF`.
        contracts_dir: Override for the packaged contracts directory.
        golden_dir: Override for the golden-transcript directory.
        last_green_path: Override for the packaged last-green projection.
        decisions_dir: When set, each gate decision is also written there as a
            content-addressed file for offline forensics.
        now: Injected clock for deterministic staleness checks.
        which: ``shutil.which``-shaped resolver; tests inject stubs.
        version_probe: ``--version`` probe; tests inject stubs.
        canary_verdict: Pre-computed canary attestation; when ``None`` it is
            read from the packaged last-green projection.
    """

    receipts_dir: Path
    chain: Any | None = None
    policy: str = POLICY_WARN
    contracts_dir: Path | None = None
    golden_dir: Path | None = None
    last_green_path: Path | None = None
    decisions_dir: Path | None = None
    now: datetime | None = None
    which: Callable[[str], str | None] | None = None
    version_probe: Callable[[str], str | None] | None = None
    canary_verdict: str | None = None

    def admit(self, adapter: str) -> AdmissionDecision | None:
        """Check admission for ``adapter``; refuse when it cannot be proved.

        Returns ``None`` for an exempt adapter or a disabled gate (nothing to
        prove, nothing sealed). Otherwise returns the decision, having sealed
        and anchored its receipt.

        Raises:
            AdapterAdmissionRefusal: Under :data:`POLICY_ENFORCE`, when the
                adapter cannot present a fresh, matching admission receipt.
        """
        return preflight_admission(
            adapter=adapter,
            receipts_dir=self.receipts_dir,
            chain=self.chain,
            policy=self.policy,
            contracts_dir=self.contracts_dir,
            golden_dir=self.golden_dir,
            last_green_path=self.last_green_path,
            decisions_dir=self.decisions_dir,
            now=self.now,
            which=self.which,
            version_probe=self.version_probe,
            canary_verdict=self.canary_verdict,
        )


def gate_decision(
    evidence: AdmissionEvidence,
    stored: dict[str, Any] | None,
    problem: str,
    *,
    now: datetime,
    ttl_seconds: int,
) -> AdmissionDecision:
    """Fold live evidence and the sealed receipt into the gate's verdict."""
    live = evaluate_admission(evidence, ttl_seconds=ttl_seconds)
    if not live.admitted:
        return live
    if stored is None:
        return _decision(evidence, VERDICT_REFUSE, problem or REASON_NO_RECEIPT, ttl_seconds)
    view = AdapterAdmissionReceipt(stored)
    if not view.admitted:
        return _decision(evidence, VERDICT_REFUSE, REASON_STORED_REFUSAL, ttl_seconds)
    if receipt_is_stale(stored, now=now):
        return _decision(evidence, VERDICT_REFUSE, REASON_RECEIPT_STALE, ttl_seconds)
    if view.replay_fingerprint != evidence.replay_fingerprint:
        return _decision(evidence, VERDICT_REFUSE, REASON_FINGERPRINT_MISMATCH, ttl_seconds)
    return live


def preflight_admission(
    *,
    adapter: str,
    receipts_dir: Path,
    chain: Any | None = None,
    policy: str = POLICY_WARN,
    contracts_dir: Path | None = None,
    golden_dir: Path | None = None,
    last_green_path: Path | None = None,
    decisions_dir: Path | None = None,
    now: datetime | None = None,
    generated_at: str | None = None,
    which: Callable[[str], str | None] | None = None,
    version_probe: Callable[[str], str | None] | None = None,
    canary_verdict: str | None = None,
    ttl_seconds: int = ADMISSION_TTL_SECONDS,
) -> AdmissionDecision | None:
    """Derive the evidence, check the sealed receipt, seal + anchor, enforce.

    The sequence is deliberately "derive first, then compare": the gate never
    trusts the stored receipt on its own. It re-derives the fingerprint from
    the tree and the installed binary and admits only when the sealed receipt
    pins that same fingerprint and is still inside its TTL. Removing the
    receipt therefore makes the adapter un-spawnable rather than merely
    unlogged, and a binary that moved under a still-valid receipt is caught as
    a fingerprint mismatch instead of riding a stale attestation.

    Every outcome -- admit and refuse alike -- is sealed as a gate receipt and
    anchored in the HMAC chain, so a contiguous chain slice proves offline
    which adapters held spawn authority during a window and, for each one that
    did not, why.

    Args:
        adapter: Adapter registry key.
        receipts_dir: Where sealed admission receipts are read from.
        chain: An ``AuditChainStore`` accepting the gate receipt, or ``None``.
        policy: :data:`POLICY_WARN` / :data:`POLICY_ENFORCE` /
            :data:`POLICY_OFF`.
        contracts_dir: Override for the packaged contracts directory.
        golden_dir: Override for the golden-transcript directory.
        last_green_path: Override for the packaged last-green projection.
        decisions_dir: When set, the gate receipt is also written there.
        now: Injected clock for staleness; defaults to wall-clock UTC.
        generated_at: Timestamp stamped into the receipt preimage; defaults to
            ``now`` in ISO-8601.
        which: ``shutil.which``-shaped resolver; tests inject stubs.
        version_probe: ``--version`` probe; tests inject stubs.
        canary_verdict: Pre-computed canary attestation.
        ttl_seconds: Freshness window for a sealed receipt.

    Returns:
        The gate's :class:`AdmissionDecision`, or ``None`` when the adapter is
        exempt, the gate is disabled, or ``adapter`` is not an adapter key.

    Raises:
        AdapterAdmissionRefusal: Under :data:`POLICY_ENFORCE`, when admission
            cannot be proved.
    """
    if policy == POLICY_OFF or adapter in ADMISSION_EXEMPT:
        return None

    # Every field of the receipt is serialised into the audit chain, so a
    # non-string adapter key would poison the anchor with an unserialisable
    # value rather than producing a decision. Only a test double reaches here
    # with one (a spawner constructed around a stubbed adapter), and there is
    # no contract, transcript, or receipt filename such a key could address -
    # so there is nothing to gate and nothing to record.
    if not isinstance(adapter, str) or not adapter:
        logger.debug("admission: skipping gate for non-string adapter key %r", type(adapter).__name__)
        return None

    clock = now if now is not None else datetime.now(UTC)
    stamp = generated_at if generated_at is not None else clock.isoformat()

    evidence = gather_admission_evidence(
        adapter,
        contracts_dir=contracts_dir,
        golden_dir=golden_dir,
        last_green_path=last_green_path,
        which=which,
        version_probe=version_probe,
        canary_verdict=canary_verdict,
    )
    stored, problem = load_admission_receipt(receipts_dir, adapter)
    decision = gate_decision(evidence, stored, problem, now=clock, ttl_seconds=ttl_seconds)

    receipt = build_admission_receipt(decision, generated_at=stamp, kind=GATE_RECEIPT_KIND)
    sha = receipt_sha256(receipt)

    if decisions_dir is not None:
        try:
            write_admission_receipt(decisions_dir, receipt)
        except (OSError, ValueError) as exc:
            logger.warning("Could not write admission gate receipt for %s: %s", adapter, exc)

    if chain is not None:
        _anchor_in_chain(chain, receipt, sha)

    if decision.admitted:
        return decision

    message = (
        f"Adapter {adapter!r} was refused admission ({decision.reason}); "
        f"receipt sha256:{sha}. Forbidden: {', '.join(decision.forbidden_capabilities) or '<none>'}. "
        f"{decision.remediation}"
    )
    if policy == POLICY_ENFORCE:
        raise AdapterAdmissionRefusal(message, receipt=receipt, receipt_sha256=sha)
    logger.warning(
        "%s Set %s=enforce to make this a hard refusal.",
        message,
        _POLICY_ENV,
    )
    return decision


# ---------------------------------------------------------------------------
# Offline audit of a chain slice
# ---------------------------------------------------------------------------


def audit_admission_no_unproven_spawn(events: Iterable[Any]) -> tuple[bool, list[str]]:
    """Prove, over a slice of gate receipt events, that no unproven adapter ran.

    Each event may be an :class:`~bernstein.core.security.audit.AuditEvent`
    (its ``details`` mapping is read) or the details mapping directly. A
    violation is any gate decision that refused admission -- under the warn
    policy such a decision still permitted the spawn, so the slice names it
    explicitly rather than leaving the permission implicit.

    Chain continuity is the complementary check:
    ``AuditChainStore.verify`` detects a truncated or tampered chain, so a
    missing window is detectable rather than silently exculpatory.

    Returns:
        ``(ok, violations)`` where ``violations`` names each refused adapter
        and the reason it was refused.
    """
    violations: list[str] = []
    for event in events:
        details = getattr(event, "details", event) or {}
        if not isinstance(details, dict):
            continue
        if details.get("kind") != GATE_RECEIPT_KIND:
            continue
        if details.get("verdict") == VERDICT_REFUSE:
            violations.append(
                f"{details.get('adapter')} refused admission ({details.get('reason')}) "
                f"at fingerprint {details.get('replay_fingerprint')}"
            )
    return (not violations, violations)
