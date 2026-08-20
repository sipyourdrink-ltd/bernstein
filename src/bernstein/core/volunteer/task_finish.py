"""Scope, gates, and the receipt a passing volunteer run produces.

A diff exists.  The question this module answers is whether it may become a
submission, and the answer is either a signed receipt bundle or a refusal
record -- never both, and never something in between.

Three things have to be true, in this order, and the order is the point.

*The patch stays inside the paths the project declared.*  Checked before any
gate starts.  A gate is a command of the project's choosing running on a
donor's machine; a patch already known to be out of scope must not buy the
right to execute anything.

*Every gate the manifest declares passes.*  The project's gates, as argv,
under a wall clock the sandbox profile fixes -- not bernstein's own quality
pipeline, which is configured against a ``.sdd`` runtime a stranger's project
does not have.

*Only then is a bundle assembled.*  A failure produces a refusal record and no
bundle at all.  There is deliberately no "bundle marked failed": a signed
bundle is a claim that the work is acceptable, and an artefact whose
acceptability hides in a boolean field is one careless reader away from being
treated as a pass.

Why scope enforcement needs two different helpers
-------------------------------------------------

``allowed_paths`` is a glob list, and matching it is
:func:`~bernstein.core.path_scope.paths_outside_scope`'s job, reached through
:meth:`~bernstein.core.volunteer.manifest.VolunteerManifest.paths_outside_scope`.
That answers *"does the declared policy admit this spelling?"* and nothing
else -- deliberately, since the same question is asked of an agent
credential's ``allowed_files`` and the two must not diverge.

It is not the whole question here, because a patch path is a string a model
wrote after reading a stranger's issue text, not a string ``git diff`` was
observed to print.  Two shapes pass a glob check and still name a file the
project never admitted:

* ``docs/../src/secrets.py`` matches the glob ``docs/**`` -- the matcher
  compares path *spellings* and does not resolve ``..``, which is correct for
  a matcher and fatal for a caller that stops there;
* ``src/plugins/x.py`` matches ``src/**`` while ``src/plugins`` is a symlink
  out of the worktree, so the write lands somewhere the glob never described.

Both are the filesystem containment barrier's department, so
:func:`~bernstein.core.security.path_containment.validate_relative_path` runs
before the glob check and
:func:`~bernstein.core.security.path_containment.contained_subpath` runs after
it, against the real worktree.  Neither is re-implemented here; a second
opinion about what ``..`` means is how the two surfaces drift apart.

The order of the three refusals is also deliberate.  A traversal or a symlink
escape is an attack signal and is reported as one; being outside
``allowed_paths`` is an ordinary mistake a contributor can fix.  Reporting the
ordinary one first would bury the interesting one.

What this module does not do
----------------------------

It does not run the adapter, clone anything, or sanitise issue text -- those
are the runner's, and they hand it a patch.  It does not re-verify from a
clean room: #3871 re-runs these same gates from a fresh worktree precisely
because it does not trust this run, and sharing the code would defeat that.
It does not open a pull request; #3872 consumes
:class:`SignedResultBundle` for that.

It does not verify what it produces.  ``manifest_sha256`` is populated here
with the manifest's own digest, which is this side of the contract; making
*verification* compare that field against the manifest at the commit a
submission names is #3911, and until that lands a bundle can verify as
internally consistent while carrying a digest nobody checked against the
project.  Nothing here needs changing when it does -- the field is already
the real digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bernstein.core.diff_paths import extract_paths_from_unified_diff
from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key
from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_subpath,
    validate_relative_path,
)
from bernstein.core.security.result_receipt_bundle import (
    GateResult,
    ResultBundle,
    build_result_bundle,
)
from bernstein.core.volunteer.claim import (
    build_completion_body,
    build_release_body,
)
from bernstein.core.volunteer.sandbox_profile import sandbox_env
from bernstein.core.volunteer.wall_clock import run_under_wall_clock

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.audit_dsse import Envelope
    from bernstein.core.security.result_receipt_bundle import ChainLink, TaskRef
    from bernstein.core.volunteer.claim import ClaimClient
    from bernstein.core.volunteer.manifest import VolunteerManifest
    from bernstein.core.volunteer.sandbox_profile import VolunteerSandboxProfile

#: A patch path that is not a usable repository-relative path at all:
#: absolute, drive-qualified, carrying a ``..`` component, or holding a NUL.
REASON_PATH_NOT_REPO_RELATIVE = "patch_path_not_repo_relative"

#: A patch path that resolves outside the worktree once symlinks are followed.
REASON_PATH_ESCAPES_WORKSPACE = "patch_path_escapes_workspace"

#: A patch path the project's ``allowed_paths`` does not admit.
REASON_PATH_OUTSIDE_ALLOWED = "patch_outside_allowed_paths"

#: A non-empty patch that names no path this build can read.  Refused rather
#: than treated as "touches nothing": an unreadable patch whose gates pass
#: would be attested as in-scope having never been scope-checked.
REASON_PATCH_NAMES_NO_PATH = "patch_names_no_path"

#: The sandbox profile was derived from a different manifest than the one
#: being enforced, so the receipt's containment chain would not reproduce.
REASON_PROFILE_MANIFEST_MISMATCH = "profile_manifest_mismatch"

#: A gate finished on time with a non-zero exit status.
REASON_GATE_FAILED = "gate_failed"

#: A gate was killed by the wall clock.
REASON_GATE_WALL_CLOCK = "gate_wall_clock_exceeded"

#: The gate budget ran out before a declared gate could start.
REASON_GATE_BUDGET_EXHAUSTED = "gate_budget_exhausted"

#: A gate's program could not be executed at all.
REASON_GATE_NOT_EXECUTABLE = "gate_not_executable"

#: Every reason this module can refuse with.  Callers persist ``reason`` as a
#: stable code, so this set is append-only in the same way the audit chain's
#: event types are: renaming one silently invalidates whatever counted it.
REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        REASON_PATH_NOT_REPO_RELATIVE,
        REASON_PATH_ESCAPES_WORKSPACE,
        REASON_PATH_OUTSIDE_ALLOWED,
        REASON_PATCH_NAMES_NO_PATH,
        REASON_PROFILE_MANIFEST_MISMATCH,
        REASON_GATE_FAILED,
        REASON_GATE_WALL_CLOCK,
        REASON_GATE_BUDGET_EXHAUSTED,
        REASON_GATE_NOT_EXECUTABLE,
    }
)

#: Shortest wall clock worth starting a gate under.  Below this the gate is
#: killed before it has loaded its interpreter, and a kill that says "your
#: tests are too slow" when it means "there was no time left" is a misleading
#: refusal.
_MIN_GATE_SECONDS = 1


@dataclass(frozen=True, slots=True)
class VolunteerRefusal:
    """Why a task produced no bundle, as a record rather than as prose.

    Refusals are the common case across a heterogeneous donor fleet, so they
    carry the same structure as a success or they end up as log lines nobody
    counts.  Shaped to match
    :func:`~bernstein.core.volunteer.sandbox_profile.describe_refusal`, which
    records the same kind of outcome one step earlier in the pipeline.

    Attributes:
        reason: A stable code from :data:`REFUSAL_REASONS`.
        detail: One human-readable sentence.  Never parsed.
        manifest_sha256: The policy in force, so a refusal is attributable to
            a manifest version without re-reading the repository.
        paths: The offending patch paths, when the refusal is about paths.
            Named so a contributor is told which files broke the rule rather
            than that something did.
        gate: The gate command that refused, when the refusal is about a gate.
    """

    reason: str
    detail: str
    manifest_sha256: str
    paths: tuple[str, ...] = ()
    gate: str | None = None

    def as_record(self) -> dict[str, object]:
        """The refusal as a structure a runner can persist without parsing."""
        return {
            "outcome": "refused",
            "reason": self.reason,
            "detail": self.detail,
            "manifest_sha256": self.manifest_sha256,
            "paths": list(self.paths),
            "gate": self.gate,
        }


@dataclass(frozen=True, slots=True)
class TaskProvenance:
    """Everything about the run that the receipt records but this step cannot know.

    The runner performed the clone and the adapter spawn, so it holds these;
    passing them as one value keeps the entry point from growing a parameter
    per receipt field.

    Attributes:
        task: Repository, commit, and issue the patch answers.
        adapter_id: Which adapter produced the patch.
        model_id: Which model the adapter drove.
        selection_receipt: The receipt for how that adapter and model were
            selected.
        chain: This bundle's position in the worker's receipt chain.
    """

    task: TaskRef
    adapter_id: str
    model_id: str
    selection_receipt: str
    chain: ChainLink


@dataclass(frozen=True, slots=True)
class SignedResultBundle:
    """A passing run: the bundle, and the envelope that attests it.

    Both, because the two must agree.  ``worker_keyid`` and
    ``worker_public_key_pem`` inside the bundle are derived from the key that
    signed the envelope rather than passed in beside it, so the bundle cannot
    name one worker while the signature is by another -- a mismatch
    :func:`~bernstein.core.security.result_receipt_bundle.verify_result_bundle`
    would reject at step 3, after the submission had already been opened.

    Attributes:
        bundle: The attested contents.
        envelope: The signed DSSE envelope carrying them.
    """

    bundle: ResultBundle
    envelope: Envelope


def enforce_allowed_paths(
    patch: str,
    *,
    manifest: VolunteerManifest,
    workspace: Path,
) -> VolunteerRefusal | None:
    """Refuse a patch that touches anything the project did not admit.

    Runs before any gate, and touches the filesystem only to resolve
    symlinks -- it neither reads nor writes a file.

    Args:
        patch: The unified diff the adapter produced.
        manifest: The project's declared policy.
        workspace: The worktree the patch applies to.  Paths are resolved
            against it, so a symlinked component is caught as the escape it
            is rather than as a name that merely looked fine.

    Returns:
        ``None`` when every path the patch touches is admitted, otherwise the
        refusal naming the offending paths.  An empty patch is admitted: it
        touches nothing, and a run that produced no changes is the gates'
        problem, not the scope check's.
    """
    digest = manifest.digest
    paths = extract_paths_from_unified_diff(patch)
    if not paths:
        if not patch.strip():
            return None
        return VolunteerRefusal(
            reason=REASON_PATCH_NAMES_NO_PATH,
            detail="the patch is not empty but names no file this build can read, so its scope cannot be checked",
            manifest_sha256=digest,
        )

    # (1) Shape. Refused before the globs get a say, because a matcher
    # compares spellings and `docs/../src/x.py` is a spelling `docs/**`
    # admits.
    unusable: list[str] = []
    for path in paths:
        try:
            validate_relative_path(path, label="patch path")
        except PathContainmentError:
            unusable.append(path)
    if unusable:
        return VolunteerRefusal(
            reason=REASON_PATH_NOT_REPO_RELATIVE,
            detail="the patch names paths that are not repository-relative, so no scope can contain them",
            manifest_sha256=digest,
            paths=tuple(unusable),
        )

    # (2) Filesystem truth. A name can be well-formed and still resolve out of
    # the worktree through a symlinked component, which no amount of string
    # checking sees.
    escaping = [path for path in paths if not _resolves_inside(workspace, path)]
    if escaping:
        return VolunteerRefusal(
            reason=REASON_PATH_ESCAPES_WORKSPACE,
            detail="the patch names paths that resolve outside the worktree once symlinks are followed",
            manifest_sha256=digest,
            paths=tuple(escaping),
        )

    # (3) Declared policy, through the one matcher both scope surfaces share.
    outside = manifest.paths_outside_scope(paths)
    if outside:
        return VolunteerRefusal(
            reason=REASON_PATH_OUTSIDE_ALLOWED,
            detail="the patch touches paths this project's allowed_paths does not admit",
            manifest_sha256=digest,
            paths=outside,
        )
    return None


def finish_volunteer_task(
    *,
    patch: str,
    manifest: VolunteerManifest,
    profile: VolunteerSandboxProfile,
    workspace: Path,
    provenance: TaskProvenance,
    signing_key: Ed25519PrivateKey,
    gate_env: Mapping[str, str] | None = None,
    gate_budget_seconds: int | None = None,
    created_at: str | None = None,
    claim: ClaimClient | None = None,
    claim_repo: str | None = None,
    claim_comment_id: int | None = None,
    claim_fingerprint: str | None = None,
    pr_url: str | None = None,
) -> SignedResultBundle | VolunteerRefusal:
    """Enforce scope and gates, sign the result, and resolve the claim comment.

    Thin wrapper over :func:`_finish_volunteer_task`: it runs the scope/gate/
    sign pipeline unchanged, then -- when a claim client and comment id are
    supplied -- edits the claim comment posted at run start to its terminal
    state.  A signed bundle edits it to a completion (with ``pr_url`` when a PR
    already exists); any refusal edits it to a release, so a task this worker
    gives up stops looking claimed to the next donor.  The edit is best-effort
    and never changes the returned value; omitting the claim arguments leaves
    behaviour identical to the pipeline alone.

    Args:
        claim: Optional claim-etiquette client (donor ``gh`` auth).
        claim_repo: ``owner/name`` of the issue's repository.
        claim_comment_id: REST id of the claim comment to edit.  Not returned by
            ``run_claimed_task`` on its success path today --
            :class:`~bernstein.core.volunteer.runner.TaskDiff` carries no such
            field -- so a caller wiring this up currently has to re-read the
            issue and call :func:`~bernstein.core.volunteer.claim.find_own_claim`
            to recover it.
        claim_fingerprint: Worker fingerprint stamped into the edit.  Defaults
            to the worker keyid derived from ``signing_key`` -- the same
            identity the signed bundle carries -- which is what makes the
            public claim comment matchable against the signed result instead
            of being decoration.  An explicit value overrides that default.
        pr_url: Link to the opened PR, when one exists, for the completion note.

    See :func:`_finish_volunteer_task` for the pipeline arguments and semantics.
    """
    result = _finish_volunteer_task(
        patch=patch,
        manifest=manifest,
        profile=profile,
        workspace=workspace,
        provenance=provenance,
        signing_key=signing_key,
        gate_env=gate_env,
        gate_budget_seconds=gate_budget_seconds,
        created_at=created_at,
    )
    if claim is not None and claim_repo is not None and claim_comment_id is not None:
        # A real per-worker identifier by default, derived fresh from the
        # signing key -- the same keyid a completion's bundle carries, so the
        # public comment is matchable against the signed result rather than
        # decoration.  An explicit claim_fingerprint still wins.
        fingerprint = claim_fingerprint or keyid_from_public_key(signing_key.public_key())
        if isinstance(result, SignedResultBundle):
            body = build_completion_body(fingerprint=fingerprint, pr_url=pr_url)
        else:
            body = build_release_body(fingerprint=fingerprint, reason=result.reason)
        claim.edit_claim(claim_repo, claim_comment_id, body)
    return result


def _finish_volunteer_task(
    *,
    patch: str,
    manifest: VolunteerManifest,
    profile: VolunteerSandboxProfile,
    workspace: Path,
    provenance: TaskProvenance,
    signing_key: Ed25519PrivateKey,
    gate_env: Mapping[str, str] | None = None,
    gate_budget_seconds: int | None = None,
    created_at: str | None = None,
) -> SignedResultBundle | VolunteerRefusal:
    """Enforce scope, re-run the project's gates, and sign the result.

    Args:
        patch: The unified diff the adapter produced, verbatim.  The bundle
            attests exactly these bytes, and exactly these bytes are what the
            scope check reads -- checking one artefact and attesting another
            is how a receipt ends up describing a run that did not happen.
        manifest: The project's declared policy.  Its digest becomes the
            bundle's ``manifest_sha256``.
        profile: The containment decision this task ran behind.  Its digest is
            recorded as the bundle's sandbox profile, continuing the
            manifest -> profile -> receipt chain.
        workspace: The worktree the patch applies to and the gates run in.
        provenance: What the runner knows and this step does not.
        signing_key: The worker's Ed25519 key.  Also the source of the
            worker identity recorded in the bundle.
        gate_env: The complete environment for every gate.  ``None`` means
            the environment derived from ``profile``, **not** the donor's own:
            a gate is a stranger's command and there is no version of
            inheriting ``os.environ`` that is safe here.
        gate_budget_seconds: Wall clock for the whole gate phase, shared
            across gates.  ``None`` takes the profile's ceiling.  The runner
            owns how much of a task's budget is left by this point (#4032);
            this function only guarantees the phase cannot exceed what it is
            given.
        created_at: Bundle timestamp.  Supply one to make a run reproducible
            byte-for-byte; the default is the current UTC second.

    Returns:
        :class:`SignedResultBundle` when the patch is in scope and every gate
        passed, otherwise a :class:`VolunteerRefusal`.  Never a bundle
        describing a failure.
    """
    if profile.manifest_sha256 != manifest.digest:
        return VolunteerRefusal(
            reason=REASON_PROFILE_MANIFEST_MISMATCH,
            detail=(
                f"the sandbox profile was derived from manifest {profile.manifest_sha256}, "
                f"but the policy being enforced is {manifest.digest}"
            ),
            manifest_sha256=manifest.digest,
        )

    refusal = enforce_allowed_paths(patch, manifest=manifest, workspace=workspace)
    if refusal is not None:
        return refusal

    budget = profile.wall_clock_seconds if gate_budget_seconds is None else gate_budget_seconds
    gates, gate_refusal = _run_gates(
        manifest,
        workspace=workspace,
        env=dict(gate_env) if gate_env is not None else sandbox_env(profile),
        budget_seconds=budget,
    )
    if gate_refusal is not None:
        return gate_refusal

    public_key = signing_key.public_key()
    bundle = ResultBundle(
        task=provenance.task,
        patch=patch,
        gates=gates,
        manifest_sha256=manifest.digest,
        adapter_id=provenance.adapter_id,
        model_id=provenance.model_id,
        sandbox_profile=profile.digest,
        selection_receipt=provenance.selection_receipt,
        created_at=created_at or _utc_second(),
        worker_keyid=keyid_from_public_key(public_key),
        worker_public_key_pem=export_public_key_pem(public_key).decode("ascii"),
        chain=provenance.chain,
    )
    return SignedResultBundle(bundle=bundle, envelope=build_result_bundle(bundle, signing_key=signing_key))


def _run_gates(
    manifest: VolunteerManifest,
    *,
    workspace: Path,
    env: Mapping[str, str],
    budget_seconds: int,
) -> tuple[tuple[GateResult, ...], VolunteerRefusal | None]:
    """Run every declared gate until one fails, and stop there.

    Stopping is not an optimisation.  The remaining gates cannot change the
    outcome, and continuing would spend a donor's machine producing logs for
    a bundle that will not exist.

    The budget is shared across gates rather than granted per gate, so a
    manifest cannot multiply its ceiling by declaring more of them.

    What is spent is measured from the gates themselves rather than from a
    deadline taken before the loop.  A deadline would charge this function's
    own bookkeeping to the project's budget, which is both wrong and, for a
    one-second budget, enough to refuse the first gate before it starts.
    """
    digest = manifest.digest
    results: list[GateResult] = []
    spent = 0.0

    for gate in manifest.gates:
        command = str(gate)
        remaining = budget_seconds - spent
        if remaining < _MIN_GATE_SECONDS:
            return tuple(results), VolunteerRefusal(
                reason=REASON_GATE_BUDGET_EXHAUSTED,
                detail=f"the {budget_seconds}s gate budget ran out before {command!r} could start",
                manifest_sha256=digest,
                gate=command,
            )

        try:
            outcome, stdout, stderr = run_under_wall_clock(
                gate.argv,
                # Truncated, never rounded up: a rounded ceiling would let a
                # long enough list of gates spend a second more than the
                # budget each, one gate at a time.
                limit_seconds=int(remaining),
                cwd=workspace,
                env=env,
            )
        except OSError as exc:
            # A gate naming a program that is not installed is an ordinary
            # donor-fleet condition, not a crash: the donor's machine simply
            # cannot run this project's gates.
            return tuple(results), VolunteerRefusal(
                reason=REASON_GATE_NOT_EXECUTABLE,
                detail=f"gate {command!r} could not be executed: {exc}",
                manifest_sha256=digest,
                gate=command,
            )

        spent += outcome.elapsed_seconds
        log = _decode(stdout) + _decode(stderr)
        if outcome.killed:
            return tuple(results), VolunteerRefusal(
                reason=REASON_GATE_WALL_CLOCK,
                detail=(
                    f"gate {command!r} was killed after {outcome.limit_seconds}s"
                    f"{' (SIGKILL)' if outcome.escalated_to_sigkill else ''}"
                ),
                manifest_sha256=digest,
                gate=command,
            )

        exit_code = outcome.exit_code if outcome.exit_code is not None else -1
        results.append(GateResult(command=command, exit_code=exit_code, log=log))
        if exit_code != 0:
            return tuple(results), VolunteerRefusal(
                reason=REASON_GATE_FAILED,
                detail=f"gate {command!r} exited {exit_code}",
                manifest_sha256=digest,
                gate=command,
            )

    return tuple(results), None


def _resolves_inside(workspace: Path, path: str) -> bool:
    """Whether ``path`` names a location inside ``workspace`` on this filesystem."""
    try:
        contained_subpath(workspace, path, label="patch path")
    except PathContainmentError:
        return False
    return True


def _decode(raw: bytes) -> str:
    """Gate output as text, never as an exception.

    A gate prints whatever it likes, including invalid UTF-8.  Losing a byte
    to a replacement character costs a slightly wrong log; raising here would
    turn a passing run into a crash, and the log is attested by its own digest
    either way.
    """
    return raw.decode("utf-8", errors="replace")


def _utc_second() -> str:
    """Current UTC time to the second, in the spelling the bundle carries."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
