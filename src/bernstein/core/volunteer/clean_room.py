"""Clean-room gate re-verification, independent of the run being checked (#3871).

Gates that passed in the agent's own workspace prove little: the workspace was
shaped by the agent, and a gate command can pass because of something the
agent's run left lying around rather than because of what the patch itself
contains. Before a bundle is submitted, its gates are re-run somewhere that
shares nothing with the run being checked: a fresh, detached worktree at the
attested base commit, the attested patch applied on top, the manifest's gates
executed again, and the outcome compared against what the bundle attests.

That independence is the entire point, and it rules out the obvious
implementation. Importing the run's own gate-execution code and calling it
against a fresh worktree would pass every test below while proving nothing:
the same code, the same environment assumptions, the same latent tolerance
for a workspace side effect would sit on both sides of a comparison whose
purpose is that the two sides are independent. This module re-derives the
gate list from the manifest as checked out in the fresh worktree
(:func:`~bernstein.core.volunteer.manifest.load_manifest_from_repo`) and
re-executes each command through
:func:`~bernstein.core.volunteer.wall_clock.run_under_wall_clock` -- the same
primitive the original run used, because that primitive is just "run this
argv under a wall clock", not a decision about whether the run passes.

:func:`verify_in_clean_room` runs :func:`~bernstein.core.security.result_receipt_bundle.verify_result_bundle`
first. A clean-room re-run against a bundle that is not even internally
consistent -- a bad signature, a tampered patch, a gate log that does not
hash to its own attested digest -- is wasted work and a confusing error to
debug later; there is nothing to re-verify until the bundle verifies itself.

Exit code vs. log digest
-------------------------

A gate log embeds real output, and real output is rarely byte-stable.
``tests/unit/security/test_result_receipt_bundle.py``'s own fixture gate log
is ``"42 passed in 3.1s\\n"`` -- pytest's summary line carries wall-clock
duration, so a second, honest run of the identical suite produces a
different log every time even when nothing about the code changed. A
byte-for-byte log comparison alone would flag nearly every real pytest-based
project as diverged on every run.

This module keeps the two kinds of disagreement distinct rather than folding
them into one verdict:

* An **exit-code divergence** means the gate's outcome itself changed --
  the bundle is wrong about whether the run passed. This is the severe case:
  :attr:`GateDivergence.exit_code_diverged`, surfaced on
  :attr:`CleanRoomResult.outcome_divergences`.
* A **log-only divergence** (exit code agrees, log digest does not) means
  the gate agreed on pass/fail both times but produced different output
  text -- ordinary nondeterminism, not a lie about the result. Surfaced
  separately, on :attr:`CleanRoomResult.log_only_divergences`.

Both still block :attr:`CleanRoomResult.passed` by default: swallowing a
log-only divergence into a silent pass is exactly what
``test_gate_nondeterminism_surfaces_as_divergence_not_a_silent_pass`` exists
to catch. What changes is which field a caller reads to tell "this project's
gates are a little noisy" from "this bundle is lying about whether tests
passed" -- the two describe different problems for a maintainer, but the
gate-level "did this reproduce" question (:attr:`GateDivergence.diverged`)
answers yes-or-no for either.

The verification receipt
-------------------------

:class:`CleanRoomVerificationReceipt` is the third file in this program with
the same DSSE-wrap-a-frozen-dataclass shape as
:mod:`bernstein.core.security.result_receipt_bundle` and
:mod:`bernstein.core.volunteer.consent` -- by now clearly this program's
house style for a signed, chainable, offline-verifiable fact, not a
coincidence worth reinventing a fourth way. It reuses
:class:`~bernstein.core.security.result_receipt_bundle.ChainLink` rather than
defining its own, and carries ``bundle_digest`` so a receipt links a specific
bundle to a specific outcome, per this issue's own acceptance criterion.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.git.git_basic import run_git
from bernstein.core.sandbox.snapshot import SnapshotError, resume_worktree_snapshot
from bernstein.core.security.audit_dsse import (
    DSSE_PAYLOAD_TYPE,
    Envelope,
    Signature,
    Statement,
    Subject,
    keyid_from_public_key,
    load_envelope,
    pae,
    parse_envelope,
    verify_envelope,
    write_envelope,
)
from bernstein.core.security.result_receipt_bundle import (
    GENESIS_ANCHOR,
    ChainLink,
    verify_result_bundle,
)
from bernstein.core.volunteer.manifest import VolunteerManifestError, load_manifest_from_repo
from bernstein.core.volunteer.wall_clock import run_under_wall_clock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

#: Predicate type for a clean-room verification receipt. Distinct from every
#: other envelope kind this program signs, so a verifier cannot confuse them.
CLEAN_ROOM_VERIFY_PREDICATE_TYPE: str = "https://bernstein.run/attestations/clean-room-verify/v1"

#: Verification receipt schema version, bumped when the field set changes.
CLEAN_ROOM_VERIFY_SCHEMA_VERSION: str = "1.0.0"

#: Default path for a persisted clean-room verification receipt, mirroring
#: :data:`~bernstein.core.volunteer.consent.DEFAULT_CONSENT_PATH`'s convention.
DEFAULT_CLEAN_ROOM_RECEIPT_PATH: str = ".sdd/runtime/volunteer/clean-room-verify.json"

# --------------------------------------------------------------------------- #
# Refusal reasons -- stable codes, mirroring VolunteerRefusal's vocabulary
# --------------------------------------------------------------------------- #

#: The bundle itself failed :func:`~bernstein.core.security.result_receipt_bundle.verify_result_bundle`
#: -- nothing below it can be trusted, so no clean-room re-run was attempted.
REASON_BUNDLE_INVALID = "bundle_invalid"

#: The attested base commit could not be checked out from the local repo
#: (:class:`~bernstein.core.sandbox.snapshot.SnapshotError`).
REASON_BASE_COMMIT_UNAVAILABLE = "base_commit_unavailable"

#: The attested patch does not apply cleanly onto a fresh checkout of the
#: attested base commit.
REASON_PATCH_CONFLICT = "patch_conflict"

#: The clean-room checkout's own ``.bernstein/volunteer.json`` could not be
#: loaded after the patch was applied (missing, unreadable, or invalid).
REASON_MANIFEST_UNREADABLE = "manifest_unreadable"

#: Every stable refusal code this module can produce.
CLEAN_ROOM_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        REASON_BUNDLE_INVALID,
        REASON_BASE_COMMIT_UNAVAILABLE,
        REASON_PATCH_CONFLICT,
        REASON_MANIFEST_UNREADABLE,
    }
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sort_recursive(value: Any) -> Any:
    """Reorder dict keys at every depth so canonical JSON is byte-stable."""
    if isinstance(value, dict):
        return {k: _sort_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_recursive(v) for v in value]
    return value


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON: recursively sorted keys, compact separators, UTF-8."""
    return json.dumps(_sort_recursive(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode(raw: bytes) -> str:
    """Gate output as text, never as an exception (mirrors ``task_finish._decode``)."""
    return raw.decode("utf-8", errors="replace")


def _utc_second() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Clean-room result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GateDivergence:
    """One gate's clean-room re-run, compared against what the bundle attests.

    Attributes:
        command: The gate command, as the manifest renders it.
        attested_exit_code: What the bundle claims the gate returned.
        actual_exit_code: What the clean-room re-run actually returned.
        attested_log_sha256: The bundle's attested digest of the gate's log.
        actual_log_sha256: The clean-room re-run's actual log digest.
    """

    command: str
    attested_exit_code: int
    actual_exit_code: int
    attested_log_sha256: str
    actual_log_sha256: str

    @property
    def exit_code_diverged(self) -> bool:
        """The gate's pass/fail outcome itself changed between runs.

        The severe case: a bundle attesting a passing exit code whose
        clean-room re-run fails (or vice versa) is not "a little noisy", it
        is wrong about whether the work happened.
        """
        return self.attested_exit_code != self.actual_exit_code

    @property
    def diverged(self) -> bool:
        """Either the exit code or the log digest disagrees with the bundle.

        Deliberately broad: a caller who only wants to know "did this gate
        reproduce" reads this one field, and it answers yes for a log-only
        mismatch too -- see the module docstring's exit-code-vs-log-digest
        note for why that mismatch is not silently dropped, and
        :class:`CleanRoomResult`'s ``outcome_divergences`` /
        ``log_only_divergences`` split for where the severity distinction
        actually lives.
        """
        return self.exit_code_diverged or self.attested_log_sha256 != self.actual_log_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "attested_exit_code": self.attested_exit_code,
            "actual_exit_code": self.actual_exit_code,
            "attested_log_sha256": self.attested_log_sha256,
            "actual_log_sha256": self.actual_log_sha256,
            "exit_code_diverged": self.exit_code_diverged,
            "diverged": self.diverged,
        }


@dataclass(frozen=True, slots=True)
class CleanRoomResult:
    """Outcome of re-running a bundle's gates in an independent worktree.

    Attributes:
        bundle_digest: The bundle being verified, so a result is attributable
            without re-reading the envelope.
        patch_applied: Whether the attested patch applied cleanly onto the
            attested base commit. ``False`` means gate re-execution never
            started: gates cannot mean anything against a worktree that never
            reached the state the bundle claims.
        divergences: One entry per gate the clean-room checkout's manifest
            declares, comparing the re-run against the bundle's attested
            :class:`~bernstein.core.security.result_receipt_bundle.GateResult`.
            Empty when verification stopped before gates ran.
        refusal_reason: A stable code (see the ``REASON_*`` module constants)
            naming why verification stopped before gates ran, or ``None``
            when it reached the gate phase.
        refusal_detail: One human-readable sentence for ``refusal_reason``.
    """

    bundle_digest: str
    patch_applied: bool
    divergences: tuple[GateDivergence, ...] = ()
    refusal_reason: str | None = None
    refusal_detail: str | None = None

    @property
    def outcome_divergences(self) -> tuple[GateDivergence, ...]:
        """Gates whose pass/fail outcome itself changed -- the severe case."""
        return tuple(d for d in self.divergences if d.exit_code_diverged)

    @property
    def log_only_divergences(self) -> tuple[GateDivergence, ...]:
        """Gates that agreed on pass/fail but produced different log text."""
        return tuple(d for d in self.divergences if d.diverged and not d.exit_code_diverged)

    @property
    def passed(self) -> bool:
        """A clean-room re-run passes only by reaching gates and reproducing every one.

        Any divergence blocks by default, including a log-only one -- see the
        module docstring. The distinction between "the outcome changed" and
        "only the text changed" is visible on ``outcome_divergences`` /
        ``log_only_divergences``, not encoded as a difference in whether this
        property blocks.
        """
        return self.refusal_reason is None and self.patch_applied and not any(d.diverged for d in self.divergences)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_digest": self.bundle_digest,
            "passed": self.passed,
            "patch_applied": self.patch_applied,
            "refusal_reason": self.refusal_reason,
            "refusal_detail": self.refusal_detail,
            "divergences": [d.to_dict() for d in self.divergences],
            "outcome_divergent_gates": [d.command for d in self.outcome_divergences],
            "log_only_divergent_gates": [d.command for d in self.log_only_divergences],
        }


def verify_in_clean_room(
    bundle_envelope: Envelope,
    *,
    repo_root: Path,
    public_key: Ed25519PublicKey,
    env: Mapping[str, str] | None = None,
    gate_budget_seconds: float | None = None,
) -> CleanRoomResult:
    """Re-run a result bundle's gates in a fresh, detached worktree and compare.

    Runs, in order:

    1. :func:`~bernstein.core.security.result_receipt_bundle.verify_result_bundle`
       against *public_key*. Anything short of ``ok`` stops here --
       :data:`REASON_BUNDLE_INVALID`.
    2. :func:`~bernstein.core.sandbox.snapshot.resume_worktree_snapshot` checks
       the attested base commit out into a fresh detached worktree under a
       scratch directory this function owns and removes when it returns,
       success or failure alike -- a verification tool that leaves worktrees
       behind on a donor's disk every time it runs is its own kind of leak.
    3. The attested patch is applied with ``git apply``. A patch that does
       not apply cleanly is :data:`REASON_PATCH_CONFLICT`, reported with a
       structured reason rather than a raw ``git apply`` stderr dump (the
       stderr is still included, as context appended to that reason).
    4. The clean-room checkout's own manifest is reloaded (not the bundle's
       claims about it) and every gate it declares is re-executed via
       :func:`~bernstein.core.volunteer.wall_clock.run_under_wall_clock`,
       sharing one wall-clock budget the same way
       :func:`~bernstein.core.volunteer.task_finish._run_gates` shares one
       across the original run -- unlike that function, every gate runs
       regardless of an earlier one diverging, because a verification report
       naming only the first mismatch is less useful than one naming all of
       them.

    Args:
        bundle_envelope: The signed bundle to verify, typically from
            :func:`~bernstein.core.security.result_receipt_bundle.load_bundle`.
        repo_root: A local git repository that already holds the attested
            base commit. :func:`~bernstein.core.sandbox.snapshot.resume_worktree_snapshot`
            does not fetch.
        public_key: The key the bundle's signature is checked against.
        env: Complete environment for every gate. ``None`` is passed straight
            through to :func:`run_under_wall_clock`, which inherits the
            current process's environment in that case -- the same default
            that function already documents. A caller wanting hardened
            containment for the re-run passes an explicit environment (for
            example, :func:`~bernstein.core.volunteer.sandbox_profile.sandbox_env`
            for a real sandbox profile); this function does not select or
            build one itself.
        gate_budget_seconds: Wall clock for the whole gate phase, shared
            across gates. ``None`` takes the clean-room manifest's own
            ``max_wall_clock_minutes``.

    Returns:
        A :class:`CleanRoomResult` describing what happened. Never raises for
        an ordinary verification failure -- those come back as ``refusal_*``
        fields or ``GateDivergence`` entries, not exceptions.
    """
    verification = verify_result_bundle(bundle_envelope, public_key)
    bundle_digest = verification.digest
    if not verification.ok:
        return CleanRoomResult(
            bundle_digest=bundle_digest,
            patch_applied=False,
            refusal_reason=REASON_BUNDLE_INVALID,
            refusal_detail=(
                "the bundle failed baseline verification, before any clean-room re-run was attempted: "
                + "; ".join(str(e) for e in verification.errors)
            ),
        )

    bundle_dict = verification.bundle
    task = bundle_dict.get("task", {})
    base_commit = str(task.get("commit_sha", "")) if isinstance(task, dict) else ""
    patch = str(bundle_dict.get("patch", ""))
    attested_gates = bundle_dict.get("gates", [])
    if not isinstance(attested_gates, list):
        attested_gates = []

    scratch_root = Path(tempfile.mkdtemp(prefix="bernstein-clean-room-"))
    dest = scratch_root / "worktree"
    try:
        try:
            resume_worktree_snapshot(repo_root, base_commit, dest)
        except SnapshotError as exc:
            return CleanRoomResult(
                bundle_digest=bundle_digest,
                patch_applied=False,
                refusal_reason=REASON_BASE_COMMIT_UNAVAILABLE,
                refusal_detail=f"could not check out attested base commit {base_commit!r}: {exc}",
            )

        if patch.strip():
            apply_result = run_git(["apply", "--whitespace=nowarn"], dest, input_data=patch)
            if not apply_result.ok:
                return CleanRoomResult(
                    bundle_digest=bundle_digest,
                    patch_applied=False,
                    refusal_reason=REASON_PATCH_CONFLICT,
                    refusal_detail=(
                        f"the attested patch does not apply cleanly to base commit {base_commit!r} "
                        f"(patch conflict): {apply_result.stderr.strip()}"
                    ),
                )

        try:
            manifest = load_manifest_from_repo(dest)
        except (FileNotFoundError, OSError, VolunteerManifestError) as exc:
            return CleanRoomResult(
                bundle_digest=bundle_digest,
                patch_applied=True,
                refusal_reason=REASON_MANIFEST_UNREADABLE,
                refusal_detail=f"the clean-room checkout's manifest could not be loaded: {exc}",
            )

        budget = gate_budget_seconds if gate_budget_seconds is not None else manifest.max_wall_clock_minutes * 60
        divergences: list[GateDivergence] = []
        spent = 0.0
        for index, gate in enumerate(manifest.gates):
            command = str(gate)
            attested = attested_gates[index] if index < len(attested_gates) else {}
            if not isinstance(attested, dict):
                attested = {}
            attested_exit_code = int(attested.get("exit_code", -1))
            attested_log_sha256 = str(attested.get("log_sha256", ""))

            remaining = max(budget - spent, 0.0)
            outcome, stdout, stderr = run_under_wall_clock(
                gate.argv,
                limit_seconds=remaining,
                cwd=dest,
                env=env,
            )
            spent += outcome.elapsed_seconds
            actual_exit_code = outcome.exit_code if outcome.exit_code is not None else -1
            actual_log = _decode(stdout) + _decode(stderr)
            divergences.append(
                GateDivergence(
                    command=command,
                    attested_exit_code=attested_exit_code,
                    actual_exit_code=actual_exit_code,
                    attested_log_sha256=attested_log_sha256,
                    actual_log_sha256=_sha256_hex(actual_log.encode("utf-8")),
                )
            )

        return CleanRoomResult(bundle_digest=bundle_digest, patch_applied=True, divergences=tuple(divergences))
    finally:
        # Self-cleaning on purpose, unlike the runner's own worktrees (which
        # a caller cleans up separately via task_finish.clean_room): nothing
        # downstream of this function needs to inspect the checkout
        # afterward, and a verification tool that leaves a worktree behind
        # on every invocation is a slow disk leak on a donor's machine.
        shutil.rmtree(scratch_root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Verification receipt -- bundle_digest chained to outcome
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CleanRoomVerificationReceipt:
    """A signed attestation of one bundle's clean-room outcome.

    Links ``bundle_digest`` to the clean-room outcome, per #3871's own
    acceptance criterion ("verification receipt links bundle hash to
    outcome"). Reuses :class:`~bernstein.core.security.result_receipt_bundle.ChainLink`
    rather than a fourth bespoke chaining scheme.

    Attributes:
        bundle_digest: The result-receipt bundle this receipt verifies.
        passed: :attr:`CleanRoomResult.passed` for that bundle.
        patch_applied: :attr:`CleanRoomResult.patch_applied`.
        refusal_reason: :attr:`CleanRoomResult.refusal_reason`, or ``""``
            when verification reached the gate phase. Empty string rather
            than ``None`` inside a signed, JSON-serialised payload, matching
            this module's other string fields.
        refusal_detail: :attr:`CleanRoomResult.refusal_detail`, or ``""``.
        outcome_divergent_gates: Commands whose exit code itself diverged --
            the severe case (see the module docstring).
        log_only_divergent_gates: Commands whose exit code agreed but whose
            log digest did not -- recorded separately, always visible,
            rather than folded into one boolean; see the module docstring's
            exit-code-vs-log-digest note.
        verifier_keyid: The signing key's keyid -- the identity that ran
            *this* clean-room check, independent of whoever signed the
            original bundle.
        verifier_public_key_pem: The signing key's PEM bytes.
        created_at: ISO-8601 UTC timestamp to the second.
        chain: Position in the verifier's own receipt chain.
    """

    bundle_digest: str
    passed: bool
    patch_applied: bool
    refusal_reason: str
    refusal_detail: str
    outcome_divergent_gates: tuple[str, ...]
    log_only_divergent_gates: tuple[str, ...]
    verifier_keyid: str
    verifier_public_key_pem: str
    created_at: str
    chain: ChainLink

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLEAN_ROOM_VERIFY_SCHEMA_VERSION,
            "bundle_digest": self.bundle_digest,
            "passed": self.passed,
            "patch_applied": self.patch_applied,
            "refusal_reason": self.refusal_reason,
            "refusal_detail": self.refusal_detail,
            "outcome_divergent_gates": list(self.outcome_divergent_gates),
            "log_only_divergent_gates": list(self.log_only_divergent_gates),
            "verifier": {
                "keyid": self.verifier_keyid,
                "public_key_pem": self.verifier_public_key_pem,
            },
            "created_at": self.created_at,
            "chain": self.chain.to_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        """sha256 of the canonical receipt bytes -- the chain anchor successors cite."""
        return _sha256_hex(self.canonical_bytes())


def clean_room_receipt_from_result(
    result: CleanRoomResult,
    *,
    chain: ChainLink,
    verifier_keyid: str,
    verifier_public_key_pem: str,
    created_at: str | None = None,
) -> CleanRoomVerificationReceipt:
    """Build the receipt this issue's acceptance criterion asks for.

    Pure field mapping from a :class:`CleanRoomResult` plus the verifier's own
    identity and chain position -- kept separate from :func:`verify_in_clean_room`
    itself so a caller can decide independently whether, and with which
    identity, a given result is worth attesting to.
    """
    return CleanRoomVerificationReceipt(
        bundle_digest=result.bundle_digest,
        passed=result.passed,
        patch_applied=result.patch_applied,
        refusal_reason=result.refusal_reason or "",
        refusal_detail=result.refusal_detail or "",
        outcome_divergent_gates=tuple(d.command for d in result.outcome_divergences),
        log_only_divergent_gates=tuple(d.command for d in result.log_only_divergences),
        verifier_keyid=verifier_keyid,
        verifier_public_key_pem=verifier_public_key_pem,
        created_at=created_at or _utc_second(),
        chain=chain,
    )


def build_clean_room_receipt(
    receipt: CleanRoomVerificationReceipt,
    *,
    signing_key: Ed25519PrivateKey,
    subject_name: str | None = None,
) -> Envelope:
    """Wrap a :class:`CleanRoomVerificationReceipt` in a signed DSSE envelope.

    Reuses the audit envelope machinery exactly as
    :func:`~bernstein.core.security.result_receipt_bundle.build_result_bundle`
    and :func:`~bernstein.core.volunteer.consent.build_consent_receipt` do.
    """
    receipt_dict = receipt.to_dict()
    receipt_bytes = canonical_bytes(receipt_dict)
    digest = _sha256_hex(receipt_bytes)

    subject = Subject(
        name=subject_name or f"clean-room-verify-{receipt.bundle_digest[:12]}.json",
        digest={"sha256": digest},
    )
    predicate = {
        "schema_version": CLEAN_ROOM_VERIFY_SCHEMA_VERSION,
        "receipt_kind": "clean-room-verify",
        "receipt": receipt_dict,
        "chain": receipt.chain.to_dict(),
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=CLEAN_ROOM_VERIFY_PREDICATE_TYPE,
        predicate=predicate,
    )

    payload = canonical_bytes(statement.to_dict())
    pae_bytes = pae(DSSE_PAYLOAD_TYPE, payload)
    signature = signing_key.sign(pae_bytes)
    keyid = keyid_from_public_key(signing_key.public_key())

    return Envelope(
        payload_type=DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode("ascii"),
        signatures=[Signature(keyid=keyid, sig=base64.b64encode(signature).decode("ascii"))],
    )


@dataclass(frozen=True, slots=True)
class CleanRoomReceiptVerification:
    """Outcome of :func:`verify_clean_room_receipt`.

    ``bundle_digest_checked`` and ``prev_digest_checked`` follow
    :class:`~bernstein.core.security.result_receipt_bundle.BundleVerification`'s
    own convention: a field a caller never asked about is not the same as one
    that was checked and agreed, and conflating the two is the defect that
    convention exists to avoid.
    """

    ok: bool
    keyid: str = ""
    digest: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    bundle_digest_checked: bool = False
    prev_digest_checked: bool = False


def verify_clean_room_receipt(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
    *,
    expected_bundle_digest: str | None = None,
    expected_prev_digest: str | None = None,
) -> CleanRoomReceiptVerification:
    """Offline, side-effect-free verification of a clean-room verification receipt.

    Mirrors :func:`~bernstein.core.volunteer.consent.verify_consent_receipt`'s
    steps: envelope signature and predicate type, embedded-receipt subject
    digest consistency, signer identity, chain-link shape and optional
    continuity, and -- when the caller names one -- that the receipt attests
    the expected bundle.
    """
    errors: list[str] = []

    env_v = verify_envelope(envelope, public_key, expected_predicate_type=CLEAN_ROOM_VERIFY_PREDICATE_TYPE)
    if not env_v.ok:
        return CleanRoomReceiptVerification(ok=False, keyid=env_v.keyid, errors=tuple(env_v.errors))

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return CleanRoomReceiptVerification(ok=False, errors=("predicate is not a dict",))

    raw_receipt = predicate_dict.get("receipt", {})
    receipt_dict = raw_receipt if isinstance(raw_receipt, dict) else {}
    if not isinstance(raw_receipt, dict):
        errors.append(f"receipt is {type(raw_receipt).__name__}, expected dict")

    raw_subject = statement.get("subject", [])
    attested_digest = ""
    subject_settled = True
    if not isinstance(raw_subject, list):
        subject_settled = False
        errors.append(f"subject is {type(raw_subject).__name__}, expected list")
    elif raw_subject:
        first_subject = raw_subject[0]
        if not isinstance(first_subject, dict):
            subject_settled = False
            errors.append(f"subject[0] is {type(first_subject).__name__}, expected dict")
        elif not isinstance(first_subject.get("digest"), dict):
            subject_settled = False
            errors.append("subject[0] missing digest")
        else:
            attested_digest = first_subject["digest"].get("sha256", "")

    # (2) internal hash consistency: the embedded receipt must reproduce the
    # subject digest byte-for-byte.
    if subject_settled:
        recomputed = _sha256_hex(canonical_bytes(raw_receipt))
        if recomputed != attested_digest:
            errors.append(f"embedded receipt hashes to {recomputed}, envelope attests {attested_digest}")

    # (3) the signer is the verifier the receipt names.
    verifier = receipt_dict.get("verifier", {})
    if not isinstance(verifier, dict):
        errors.append(f"verifier is {type(verifier).__name__}, expected dict")
    elif verifier.get("keyid") and env_v.keyid and verifier["keyid"] != env_v.keyid:
        errors.append(f"receipt names verifier {verifier['keyid']}, signature is by {env_v.keyid}")

    # (4) chain link shape, and optionally continuity with a predecessor.
    prev_digest_checked = False
    chain = receipt_dict.get("chain", {})
    if not isinstance(chain, dict):
        errors.append(f"chain is {type(chain).__name__}, expected dict")
    elif "anchor" not in chain or "length" not in chain:
        errors.append("chain missing anchor or length")
    elif not isinstance(chain.get("length"), int) or isinstance(chain["length"], bool) or chain["length"] < 1:
        errors.append(f"chain.length invalid: {chain.get('length')!r}")
    elif expected_prev_digest is not None:
        prev_digest_checked = True
        if chain.get("anchor") != expected_prev_digest:
            errors.append(f"chain.anchor {chain.get('anchor')} does not link to predecessor {expected_prev_digest}")

    # (5) the bundle this receipt attests to, when the caller names one.
    bundle_digest_checked = False
    if expected_bundle_digest is not None:
        bundle_digest_checked = True
        carried = receipt_dict.get("bundle_digest")
        if carried != expected_bundle_digest:
            errors.append(f"bundle_digest is {carried!r}, expected {expected_bundle_digest!r}")

    return CleanRoomReceiptVerification(
        ok=not errors,
        keyid=env_v.keyid,
        digest=attested_digest,
        receipt=receipt_dict,
        errors=tuple(errors),
        bundle_digest_checked=bundle_digest_checked,
        prev_digest_checked=prev_digest_checked,
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def write_clean_room_receipt(envelope: Envelope, path: Path) -> Path:
    """Persist a clean-room receipt envelope as canonical JSON."""
    return write_envelope(envelope, path)


def load_clean_room_receipt(path: Path) -> Envelope:
    """Load a clean-room receipt envelope from disk."""
    return load_envelope(path)


def parse_clean_room_receipt(data: dict[str, Any]) -> Envelope:
    """Parse a clean-room receipt envelope from an already-decoded dict."""
    return parse_envelope(data)


__all__ = [
    "CLEAN_ROOM_REFUSAL_REASONS",
    "CLEAN_ROOM_VERIFY_PREDICATE_TYPE",
    "CLEAN_ROOM_VERIFY_SCHEMA_VERSION",
    "DEFAULT_CLEAN_ROOM_RECEIPT_PATH",
    "GENESIS_ANCHOR",
    "REASON_BASE_COMMIT_UNAVAILABLE",
    "REASON_BUNDLE_INVALID",
    "REASON_MANIFEST_UNREADABLE",
    "REASON_PATCH_CONFLICT",
    "CleanRoomReceiptVerification",
    "CleanRoomResult",
    "CleanRoomVerificationReceipt",
    "GateDivergence",
    "build_clean_room_receipt",
    "clean_room_receipt_from_result",
    "load_clean_room_receipt",
    "parse_clean_room_receipt",
    "verify_clean_room_receipt",
    "verify_in_clean_room",
    "write_clean_room_receipt",
]
