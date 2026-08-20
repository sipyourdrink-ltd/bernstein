"""Scope enforcement, gate re-runs, and the receipt a passing run produces.

Every test is named for the property it protects. Containment is a question
about a real filesystem, so the scope tests build a real temporary worktree
with real symlinks rather than patching the checks out from under themselves;
the gate tests run real subprocesses under a real wall clock, because a mocked
kill proves the mock.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit_dsse import keyid_from_public_key
from bernstein.core.security.result_receipt_bundle import (
    GENESIS_ANCHOR,
    ChainLink,
    TaskRef,
    verify_result_bundle,
)
from bernstein.core.volunteer.claim import ClaimClient
from bernstein.core.volunteer.manifest import VolunteerManifest, load_manifest
from bernstein.core.volunteer.sandbox_profile import VolunteerSandboxProfile, build_volunteer_profile
from bernstein.core.volunteer.task_finish import (
    REASON_GATE_BUDGET_EXHAUSTED,
    REASON_GATE_FAILED,
    REASON_GATE_NOT_EXECUTABLE,
    REASON_GATE_WALL_CLOCK,
    REASON_PATCH_NAMES_NO_PATH,
    REASON_PATH_ESCAPES_WORKSPACE,
    REASON_PATH_NOT_REPO_RELATIVE,
    REASON_PATH_OUTSIDE_ALLOWED,
    REASON_PROFILE_MANIFEST_MISMATCH,
    REFUSAL_REASONS,
    SignedResultBundle,
    TaskProvenance,
    VolunteerRefusal,
    finish_volunteer_task,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Gate commands. Written as argv with no shell metacharacters, because the
# manifest loader refuses those outright -- which is itself the reason a gate
# is an argv in the first place.
# ---------------------------------------------------------------------------

PASSING_GATE = [sys.executable, "-c", "pass"]
FAILING_GATE = [sys.executable, "-c", "raise SystemExit(3)"]
HANGING_GATE = [sys.executable, "-c", "__import__('time').sleep(30)"]
SENTINEL_GATE = [sys.executable, "-c", "__import__('pathlib').Path('gate-ran').touch()"]
SECOND_SENTINEL_GATE = [sys.executable, "-c", "__import__('pathlib').Path('second-gate-ran').touch()"]
ENV_ECHO_GATE = [
    sys.executable,
    "-c",
    "__import__('sys').stdout.write(__import__('os').environ.get('DONOR_SECRET', 'absent'))",
]


def _manifest(*, allowed_paths: list[str] | None = None, gates: list[list[str]] | None = None) -> VolunteerManifest:
    document: dict[str, Any] = {
        "version": 1,
        "license": "Apache-2.0",
        "gates": gates if gates is not None else [PASSING_GATE],
        "sandbox": "container",
        "max_wall_clock_minutes": 5,
    }
    if allowed_paths is not None:
        document["allowed_paths"] = allowed_paths
    return load_manifest(json.dumps(document))


def _profile(manifest: VolunteerManifest) -> VolunteerSandboxProfile:
    return build_volunteer_profile(
        manifest,
        available_backends=["container"],
        donor_accepts_plain_container=True,
    )


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([4033 % 256]) * 32)


def _provenance() -> TaskProvenance:
    return TaskProvenance(
        task=TaskRef(repo="example/project", commit_sha="0123456789abcdef", issue_number=4033),
        adapter_id="adapter.mock.v1",
        model_id="mock-model",
        selection_receipt="sel-receipt-4033",
        chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


#: One edited line, which is all any of these fixtures needs the hunk to say.
_HUNK = "@@ -1 +1 @@\n-old\n+new\n"


def _diff_touching(*paths: str) -> str:
    """A minimal but well-formed unified diff editing each path in place."""
    blocks: list[str] = []
    for path in paths:
        blocks.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{_HUNK}")
    return "".join(blocks)


def _finish(
    tmp_path: Path,
    *,
    patch: str,
    manifest: VolunteerManifest,
    workspace: Path | None = None,
    profile: VolunteerSandboxProfile | None = None,
    gate_budget_seconds: int | None = None,
    gate_env: dict[str, str] | None = None,
) -> SignedResultBundle | VolunteerRefusal:
    return finish_volunteer_task(
        patch=patch,
        manifest=manifest,
        profile=profile if profile is not None else _profile(manifest),
        workspace=workspace if workspace is not None else _workspace(tmp_path),
        provenance=_provenance(),
        signing_key=_key(),
        gate_budget_seconds=gate_budget_seconds,
        gate_env=gate_env if gate_env is not None else {},
        created_at="2026-08-17T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Scope: refused before anything executes
# ---------------------------------------------------------------------------


def test_a_diff_touching_a_path_outside_allowed_paths_is_refused_before_any_gate_runs(tmp_path: Path) -> None:
    """The gate subprocess must not start, not merely be reported as skipped.

    Asserted through the filesystem rather than through the return value: a
    refusal that says "no gates ran" while a gate ran is exactly the bug worth
    a test, and only the sentinel can tell the two apart.
    """
    workspace = _workspace(tmp_path)
    manifest = _manifest(allowed_paths=["docs/**"], gates=[SENTINEL_GATE])

    result = _finish(tmp_path, patch=_diff_touching("src/pkg/mod.py"), manifest=manifest, workspace=workspace)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATH_OUTSIDE_ALLOWED
    assert result.paths == ("src/pkg/mod.py",)
    assert not (workspace / "gate-ran").exists()


def test_a_symlinked_directory_escaping_the_worktree_is_refused_although_the_glob_admits_it(
    tmp_path: Path,
) -> None:
    """``src/`` matching ``src/**`` says nothing about where ``src/`` points.

    The glob compares spellings. Only the filesystem knows this one is a
    symlink out of the worktree, so only a resolved check can refuse it.
    """
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "src").symlink_to(outside, target_is_directory=True)
    manifest = _manifest(allowed_paths=["src/**"], gates=[SENTINEL_GATE])

    result = _finish(tmp_path, patch=_diff_touching("src/stolen.py"), manifest=manifest, workspace=workspace)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATH_ESCAPES_WORKSPACE
    assert result.paths == ("src/stolen.py",)
    assert not (workspace / "gate-ran").exists()


def test_a_dot_dot_segment_a_glob_admits_is_refused_as_a_traversal(tmp_path: Path) -> None:
    """``docs/../src/x.py`` is a string the glob ``docs/**`` matches.

    The matcher is right to match it -- it compares spellings and resolving
    paths is not its job. A caller that stopped at the glob would accept a
    write to ``src/``.
    """
    manifest = _manifest(allowed_paths=["docs/**"], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("docs/../src/secrets.py"), manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATH_NOT_REPO_RELATIVE
    assert result.paths == ("docs/../src/secrets.py",)


def test_an_absolute_patch_path_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(allowed_paths=[], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("/etc/passwd"), manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATH_NOT_REPO_RELATIVE


def test_a_case_variant_spelling_of_an_allowed_path_is_refused(tmp_path: Path) -> None:
    """Scope matching stays case-sensitive on a case-insensitive filesystem.

    macOS would open ``SRC/mod.py`` and ``src/mod.py`` as the same file, and
    it is tempting to make the matcher agree. It must not: on Linux those are
    two files, and a matcher that case-folds would let a scope written for one
    admit the other. Refusing the variant is the direction that is correct on
    both.
    """
    manifest = _manifest(allowed_paths=["src/**"], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("SRC/mod.py"), manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATH_OUTSIDE_ALLOWED
    assert result.paths == ("SRC/mod.py",)


def test_a_rename_out_of_allowed_paths_is_refused_although_it_prints_no_hunk(tmp_path: Path) -> None:
    """A content-preserving rename has no ``+++`` line to read.

    Reading only hunk headers would see an empty patch here and admit a move
    out of the declared scope.
    """
    manifest = _manifest(allowed_paths=["docs/**"], gates=[SENTINEL_GATE])
    workspace = _workspace(tmp_path)
    patch = (
        "diff --git a/docs/guide.md b/src/pkg/guide.md\n"
        "similarity index 100%\n"
        "rename from docs/guide.md\n"
        "rename to src/pkg/guide.md\n"
    )

    result = _finish(tmp_path, patch=patch, manifest=manifest, workspace=workspace)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATH_OUTSIDE_ALLOWED
    assert result.paths == ("src/pkg/guide.md",)
    assert not (workspace / "gate-ran").exists()


def test_a_mode_change_outside_allowed_paths_is_refused_although_it_prints_no_hunk(tmp_path: Path) -> None:
    """Making a script executable is a change, and it prints no hunk either."""
    manifest = _manifest(allowed_paths=["docs/**"], gates=[PASSING_GATE])
    patch = "diff --git a/scripts/run.sh b/scripts/run.sh\nold mode 100644\nnew mode 100755\n"

    result = _finish(tmp_path, patch=patch, manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATH_OUTSIDE_ALLOWED
    assert result.paths == ("scripts/run.sh",)


def test_a_non_empty_patch_naming_no_readable_path_is_refused(tmp_path: Path) -> None:
    """Unreadable is not the same as "touches nothing".

    Treating it as empty would run the gates and, on a pass, sign a bundle
    attesting a patch whose scope was never checked.
    """
    manifest = _manifest(allowed_paths=["docs/**"], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch="this is not a diff at all\n", manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PATCH_NAMES_NO_PATH


def test_an_empty_allowed_paths_manifest_admits_every_changed_path(tmp_path: Path) -> None:
    """An unset ``allowed_paths`` means repo-wide, and always has.

    Every project that never declared one carries an empty list. Flipping the
    default to "admit nothing" would refuse all of them at once.
    """
    manifest = _manifest(allowed_paths=[], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("anywhere/at/all.py", "src/pkg/mod.py"), manifest=manifest)

    assert isinstance(result, SignedResultBundle)


def test_a_profile_derived_from_another_manifest_is_refused_before_any_gate_runs(tmp_path: Path) -> None:
    """The containment chain has to reproduce, or the receipt attests nothing.

    A profile built from a different policy would put a digest in the bundle
    that a maintainer rebuilding from the submitted commit cannot reproduce.
    """
    workspace = _workspace(tmp_path)
    manifest = _manifest(allowed_paths=[], gates=[SENTINEL_GATE])
    foreign = _profile(_manifest(allowed_paths=["docs/**"], gates=[SENTINEL_GATE]))

    result = _finish(
        tmp_path,
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        workspace=workspace,
        profile=foreign,
    )

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_PROFILE_MANIFEST_MISMATCH
    assert not (workspace / "gate-ran").exists()


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_all_gates_passing_produces_a_bundle_whose_manifest_sha256_equals_the_source_manifests_digest(
    tmp_path: Path,
) -> None:
    """Byte-for-byte the manifest's own digest, not merely 64 hex characters.

    The receipt binds to the policy through this field. A digest computed a
    second way could disagree with the one a maintainer recomputes, and a
    plausible-looking hash is worse than no hash.
    """
    manifest = _manifest(allowed_paths=["src/**"], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("src/pkg/mod.py"), manifest=manifest)

    assert isinstance(result, SignedResultBundle)
    assert result.bundle.manifest_sha256 == manifest.digest
    assert result.bundle.sandbox_profile == _profile(manifest).digest
    assert len(result.bundle.gates) == 1
    assert result.bundle.gates[0].exit_code == 0


def test_one_failing_gate_produces_a_refusal_with_no_bundle_at_all(tmp_path: Path) -> None:
    """Not a bundle carrying a failure flag -- no bundle.

    A signed artefact is a claim the work is acceptable. One that says so in a
    boolean is one careless reader away from being treated as a pass.
    """
    manifest = _manifest(allowed_paths=[], gates=[PASSING_GATE, FAILING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("src/pkg/mod.py"), manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    assert not hasattr(result, "bundle")
    assert result.reason == REASON_GATE_FAILED
    assert result.gate == " ".join(FAILING_GATE)


def test_a_failing_gate_stops_the_gates_after_it_from_running(tmp_path: Path) -> None:
    """The rest cannot change the outcome, and the machine is not ours."""
    workspace = _workspace(tmp_path)
    manifest = _manifest(allowed_paths=[], gates=[FAILING_GATE, SENTINEL_GATE])

    result = _finish(tmp_path, patch=_diff_touching("src/pkg/mod.py"), manifest=manifest, workspace=workspace)

    assert isinstance(result, VolunteerRefusal)
    assert not (workspace / "gate-ran").exists()


def test_a_wall_clock_kill_on_any_gate_produces_a_refusal_naming_which_gate(tmp_path: Path) -> None:
    """A real hang, a real kill. Which gate hung is the operator's first question."""
    manifest = _manifest(allowed_paths=[], gates=[HANGING_GATE])

    result = _finish(
        tmp_path,
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        gate_budget_seconds=1,
    )

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_GATE_WALL_CLOCK
    assert result.gate == " ".join(HANGING_GATE)


def test_the_gate_budget_is_shared_across_gates_rather_than_granted_to_each(tmp_path: Path) -> None:
    """Otherwise a manifest multiplies the donor's ceiling by declaring gates.

    The first gate consumes the one-second budget just by starting, so the
    second is refused for want of time. Were the budget per gate, the second
    would have got a fresh second and passed.
    """
    workspace = _workspace(tmp_path)
    manifest = _manifest(allowed_paths=[], gates=[PASSING_GATE, SECOND_SENTINEL_GATE])

    result = _finish(
        tmp_path,
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        workspace=workspace,
        gate_budget_seconds=1,
    )

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_GATE_BUDGET_EXHAUSTED
    assert result.gate == " ".join(SECOND_SENTINEL_GATE)
    assert not (workspace / "second-gate-ran").exists()


def test_a_gate_whose_program_is_missing_is_a_refusal_not_a_crash(tmp_path: Path) -> None:
    """A donor's machine simply not having a project's toolchain is routine."""
    manifest = _manifest(allowed_paths=[], gates=[["definitely-not-installed-4033", "--version"]])

    result = _finish(tmp_path, patch=_diff_touching("src/pkg/mod.py"), manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_GATE_NOT_EXECUTABLE
    assert result.gate == "definitely-not-installed-4033 --version"


def test_a_gate_never_sees_the_donors_own_environment(tmp_path: Path, monkeypatch: Any) -> None:
    """The default environment is built, never inherited.

    A gate is a command chosen by a repository the donor does not control. An
    omitted ``gate_env`` has to mean "the sandbox's environment", because the
    other reading hands a stranger's command every credential on the machine.
    """
    monkeypatch.setenv("DONOR_SECRET", "donor-secret-value-4033")
    manifest = _manifest(allowed_paths=[], gates=[ENV_ECHO_GATE])

    result = finish_volunteer_task(
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        profile=_profile(manifest),
        workspace=_workspace(tmp_path),
        provenance=_provenance(),
        signing_key=_key(),
        created_at="2026-08-17T00:00:00Z",
    )

    assert isinstance(result, SignedResultBundle)
    assert result.bundle.gates[0].log == "absent"
    assert "donor-secret-value-4033" not in result.bundle.gates[0].log


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


def test_the_signed_envelope_verifies_against_the_worker_identity_the_bundle_names(tmp_path: Path) -> None:
    """The bundle's worker fields come from the signing key, so they agree.

    Passing them in alongside the key would let a bundle name one worker while
    the signature is by another -- a mismatch verification rejects only after
    the submission has been opened.
    """
    manifest = _manifest(allowed_paths=["src/**"], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("src/pkg/mod.py"), manifest=manifest)

    assert isinstance(result, SignedResultBundle)
    verification = verify_result_bundle(result.envelope, _key().public_key())
    assert verification.ok, verification.errors
    assert verification.bundle["manifest_sha256"] == manifest.digest


def test_a_refusal_is_a_record_rather_than_prose(tmp_path: Path) -> None:
    """Refusals are the common case on a donor fleet; they need counting."""
    manifest = _manifest(allowed_paths=["docs/**"], gates=[PASSING_GATE])

    result = _finish(tmp_path, patch=_diff_touching("src/pkg/mod.py"), manifest=manifest)

    assert isinstance(result, VolunteerRefusal)
    record = result.as_record()
    assert record["outcome"] == "refused"
    assert record["reason"] in REFUSAL_REASONS
    assert record["manifest_sha256"] == manifest.digest
    assert record["paths"] == ["src/pkg/mod.py"]


# ---------------------------------------------------------------------------
# Claim etiquette: the terminal edit to the claim comment
# ---------------------------------------------------------------------------


@dataclass
class _RecordingClaimRunner:
    """A ``gh`` stub that records the edits ``finish_volunteer_task`` makes.

    The finish step only ever edits an existing claim comment, so a PATCH is
    the only call to expect; anything else is a test that drifted from the code.
    """

    patches: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ["api", "-X", "PATCH"], f"unexpected gh call: {args}"
        self.patches.append({"url": args[3], "body": json.loads(stdin or "{}").get("body")})
        return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="{}", stderr="")


def test_a_signed_bundle_edits_the_claim_comment_to_a_completion(tmp_path: Path) -> None:
    """A passing run resolves the claim it posted at start into a completion,
    carrying the PR link, by editing the same comment rather than posting a new
    one -- so the thread shows one comment per worker, not a growing log."""
    runner = _RecordingClaimRunner()
    manifest = _manifest(allowed_paths=["src/**"], gates=[PASSING_GATE])

    result = finish_volunteer_task(
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        profile=_profile(manifest),
        workspace=_workspace(tmp_path),
        provenance=_provenance(),
        signing_key=_key(),
        gate_env={},
        created_at="2026-08-17T00:00:00Z",
        claim=ClaimClient(runner=runner),
        claim_repo="example/project",
        claim_comment_id=555,
        claim_fingerprint="fp-abc",
        pr_url="https://github.com/example/project/pull/9",
    )

    assert isinstance(result, SignedResultBundle)
    assert len(runner.patches) == 1
    assert runner.patches[0]["url"].endswith("/issues/comments/555")
    assert "Completed" in runner.patches[0]["body"]
    assert "pull/9" in runner.patches[0]["body"]


def test_a_gate_refusal_edits_the_claim_comment_to_a_release(tmp_path: Path) -> None:
    """A run that fails its gates releases the claim, so the task stops looking
    claimed to the next donor -- the same comment, edited, never a duplicate."""
    runner = _RecordingClaimRunner()
    manifest = _manifest(allowed_paths=["src/**"], gates=[FAILING_GATE])

    result = finish_volunteer_task(
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        profile=_profile(manifest),
        workspace=_workspace(tmp_path),
        provenance=_provenance(),
        signing_key=_key(),
        gate_env={},
        created_at="2026-08-17T00:00:00Z",
        claim=ClaimClient(runner=runner),
        claim_repo="example/project",
        claim_comment_id=777,
    )

    assert isinstance(result, VolunteerRefusal)
    assert result.reason == REASON_GATE_FAILED
    assert len(runner.patches) == 1
    assert runner.patches[0]["url"].endswith("/issues/comments/777")
    assert "Released" in runner.patches[0]["body"]


def test_a_completion_defaults_its_fingerprint_to_the_signing_keys_keyid(tmp_path: Path) -> None:
    """Omitting ``claim_fingerprint`` must not silently stamp a meaningless
    value: it now defaults to the same worker keyid the signed bundle itself
    carries, so the public claim comment is matchable against the signed
    result instead of being decoration."""
    runner = _RecordingClaimRunner()
    manifest = _manifest(allowed_paths=["src/**"], gates=[PASSING_GATE])
    key = _key()

    result = finish_volunteer_task(
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        profile=_profile(manifest),
        workspace=_workspace(tmp_path),
        provenance=_provenance(),
        signing_key=key,
        gate_env={},
        created_at="2026-08-17T00:00:00Z",
        claim=ClaimClient(runner=runner),
        claim_repo="example/project",
        claim_comment_id=555,
    )

    assert isinstance(result, SignedResultBundle)
    expected_keyid = keyid_from_public_key(key.public_key())
    assert expected_keyid == result.bundle.worker_keyid
    assert f"`{expected_keyid}`" in runner.patches[0]["body"]


def test_no_claim_client_leaves_the_finish_step_untouched(tmp_path: Path) -> None:
    """The claim arguments are entirely opt-in: omitting them is the pre-existing
    behaviour, byte for byte."""
    manifest = _manifest(allowed_paths=["src/**"], gates=[PASSING_GATE])

    result = finish_volunteer_task(
        patch=_diff_touching("src/pkg/mod.py"),
        manifest=manifest,
        profile=_profile(manifest),
        workspace=_workspace(tmp_path),
        provenance=_provenance(),
        signing_key=_key(),
        gate_env={},
        created_at="2026-08-17T00:00:00Z",
    )

    assert isinstance(result, SignedResultBundle)
