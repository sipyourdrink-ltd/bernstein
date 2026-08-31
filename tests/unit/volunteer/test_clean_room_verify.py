"""Clean-room gate verification: the #3871 test matrix.

Named ``test_clean_room_verify.py`` rather than ``test_clean_room.py``: that
filename is already taken by
:mod:`tests.unit.volunteer.test_clean_room`, which covers a same-named but
unrelated function -- ``task_finish.clean_room()``, a worktree-cleanup
utility.  This file covers a different concept entirely: re-running a
result bundle's gates in an independent worktree and comparing the outcome.
Reusing the existing filename would either silently extend an unrelated
test file's scope or silently replace its contents; a new, clearly-scoped
filename avoids both.

    1. an honest bundle passes clean-room re-verification
    2. a bundle that only passes because of an external workspace side
       effect is blocked, even though the patch itself is in scope
    3. a patch generated against a different base commit is blocked with a
       structured reason, not a raw ``git apply`` stderr dump
    4. two verification receipts built in sequence chain-link correctly
    5. real gate nondeterminism (a second, real execution of a real command)
       surfaces as a divergence rather than being silently swallowed

Every fixture bundle is produced by actually running the gate command for
real and capturing its real exit code and log -- never a hand-typed
``GateResult``. A bundle built from strings the test author invented would
not exercise the property this file exists to check: that a *second,
independent, real* execution agrees or disagrees with the *first*.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key
from bernstein.core.security.result_receipt_bundle import (
    GENESIS_ANCHOR,
    ChainLink,
    GateResult,
    ResultBundle,
    TaskRef,
    build_result_bundle,
)
from bernstein.core.volunteer.clean_room import (
    REASON_PATCH_CONFLICT,
    build_clean_room_receipt,
    clean_room_receipt_from_result,
    verify_clean_room_receipt,
    verify_in_clean_room,
)
from bernstein.core.volunteer.wall_clock import run_under_wall_clock

_GIT_IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@invalid"]

_MANIFEST: dict[str, Any] = {
    "version": 1,
    "license": "Apache-2.0",
    "gates": [],
    "allowed_paths": [],
    "egress_allowlist": [],
    "sandbox": "microvm",
    "max_wall_clock_minutes": 5,
    "task_label": "volunteer-ok",
    "local_ok": True,
}


def _key(seed_byte: int = 3) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


def _fixture_repo(tmp_path: Path, *, gates: list[list[str]]) -> tuple[Path, str]:
    """A real git repository with one commit, opted into the volunteer program.

    Returns the repo path and its HEAD commit sha.
    """
    repo = tmp_path / "fixture-repo"
    (repo / ".bernstein").mkdir(parents=True)
    manifest = {**_MANIFEST, "gates": gates}
    (repo / ".bernstein" / "volunteer.json").write_text(json.dumps(manifest), encoding="utf-8")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *_GIT_IDENTITY, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *_GIT_IDENTITY, "commit", "-qm", "fixture"], cwd=repo, check=True)
    base_commit = _run_git(["rev-parse", "HEAD"], repo).strip()
    return repo, base_commit


def _real_patch(repo: Path) -> str:
    """A real unified diff against HEAD, produced by ``git diff`` and reverted.

    Uncommitted on purpose -- :class:`~bernstein.core.volunteer.runner.TaskDiff`
    documents that an agent's diff is exactly this: changes against the base
    commit that were never committed.
    """
    (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    diff = _run_git(["diff"], repo)
    subprocess.run(["git", "checkout", "--", "README.md"], cwd=repo, check=True)
    return diff


def _real_gate_result(argv: list[str], *, cwd: Path) -> GateResult:
    """Actually run *argv* and capture its real exit code and log.

    The same execution path :func:`verify_in_clean_room` itself uses, so a
    bundle built this way represents a genuine first run, not a guess about
    what one would have produced.
    """
    outcome, stdout, stderr = run_under_wall_clock(argv, limit_seconds=30, cwd=cwd, env=None)
    log = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
    exit_code = outcome.exit_code if outcome.exit_code is not None else -1
    return GateResult(command=" ".join(argv), exit_code=exit_code, log=log)


def _bundle(
    key: Ed25519PrivateKey,
    *,
    base_commit: str,
    patch: str,
    gates: tuple[GateResult, ...],
) -> ResultBundle:
    pub = key.public_key()
    return ResultBundle(
        task=TaskRef(repo="fixture/repo", commit_sha=base_commit, issue_number=3871),
        patch=patch,
        gates=gates,
        manifest_sha256="unused-in-these-tests",
        adapter_id="adapter.test",
        model_id="test-model",
        sandbox_profile="restricted-net-off",
        selection_receipt="sel-receipt-test",
        created_at="2026-08-30T00:00:00Z",
        worker_keyid=keyid_from_public_key(pub),
        worker_public_key_pem=export_public_key_pem(pub).decode("ascii"),
        chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
    )


# --------------------------------------------------------------------------
# 1. honest bundle passes clean room
# --------------------------------------------------------------------------


def test_an_honest_bundle_passes_clean_room(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "print('gate ok')"]
    repo, base_commit = _fixture_repo(tmp_path, gates=[argv])
    patch = _real_patch(repo)
    gate_result = _real_gate_result(argv, cwd=repo)

    key = _key()
    bundle = _bundle(key, base_commit=base_commit, patch=patch, gates=(gate_result,))
    envelope = build_result_bundle(bundle, signing_key=key)

    result = verify_in_clean_room(envelope, repo_root=repo, public_key=key.public_key())

    assert result.patch_applied is True
    assert result.refusal_reason is None, result.refusal_detail
    assert all(not d.diverged for d in result.divergences), result.divergences
    assert result.passed is True


# --------------------------------------------------------------------------
# 2. workspace-side-effect bundle blocked
# --------------------------------------------------------------------------


def test_a_bundle_that_only_passes_with_workspace_side_effects_is_blocked(tmp_path: Path) -> None:
    """The gate reads a file the agent's run created OUTSIDE the patch.

    Nothing about ``allowed_paths`` or the patch's own scope names this file
    -- it is an absolute path outside the repository entirely -- so a check
    that only inspected the patch's file list would never catch this. Only an
    independent re-run, in a workspace that never had the file, catches it.
    """
    marker = tmp_path / "side-effect-marker.txt"
    # A single expression, deliberately -- the manifest loader rejects any
    # gate argv element containing a shell metacharacter (';', '\n', ...) as
    # a defence against shell tricks reaching a shell-less exec, so the
    # script cannot be two statements joined by ';' or '\n'.
    argv = [
        sys.executable,
        "-c",
        f"__import__('sys').exit(0 if __import__('pathlib').Path({str(marker)!r}).exists() else 1)",
    ]
    repo, base_commit = _fixture_repo(tmp_path, gates=[argv])
    patch = _real_patch(repo)

    marker.write_text("left behind by the agent's own run\n", encoding="utf-8")
    try:
        gate_result = _real_gate_result(argv, cwd=repo)
    finally:
        marker.unlink()  # the clean room must not see it

    assert gate_result.exit_code == 0, "fixture is broken: the marker-present run must pass"

    key = _key()
    bundle = _bundle(key, base_commit=base_commit, patch=patch, gates=(gate_result,))
    envelope = build_result_bundle(bundle, signing_key=key)

    result = verify_in_clean_room(envelope, repo_root=repo, public_key=key.public_key())

    assert result.patch_applied is True
    assert len(result.divergences) == 1
    divergence = result.divergences[0]
    assert divergence.exit_code_diverged is True, "the side effect must change the gate's actual outcome"
    assert divergence.actual_exit_code != 0
    assert result.passed is False
    assert divergence in result.outcome_divergences


# --------------------------------------------------------------------------
# 3. patch-conflict-on-base blocked with a structured reason
# --------------------------------------------------------------------------


def test_a_patch_that_conflicts_with_the_base_commit_is_blocked_with_a_structured_reason(
    tmp_path: Path,
) -> None:
    repo, base_commit = _fixture_repo(tmp_path, gates=[["true"]])

    # Advance the repo past base_commit, then build a patch against the NEW
    # tip -- its context lines expect content base_commit does not have.
    (repo / "README.md").write_text("hello\nsecond commit\n", encoding="utf-8")
    subprocess.run(["git", *_GIT_IDENTITY, "commit", "-aqm", "second"], cwd=repo, check=True)
    stale_patch = _real_patch(repo)
    assert stale_patch.strip(), "fixture is broken: expected a non-empty diff"

    key = _key()
    # Attest base_commit (the OLD tip) while carrying a patch generated
    # against the repo's current (newer) state -- git apply must refuse.
    bundle = _bundle(key, base_commit=base_commit, patch=stale_patch, gates=())
    envelope = build_result_bundle(bundle, signing_key=key)

    result = verify_in_clean_room(envelope, repo_root=repo, public_key=key.public_key())

    assert result.patch_applied is False
    assert result.refusal_reason == REASON_PATCH_CONFLICT
    assert result.refusal_detail is not None
    # structured: names the concept, not just a bare stderr dump.
    assert "patch" in result.refusal_detail.lower()
    assert "conflict" in result.refusal_detail.lower() or "apply cleanly" in result.refusal_detail.lower()
    assert result.passed is False


# --------------------------------------------------------------------------
# 4. verification receipt chain link verifies
# --------------------------------------------------------------------------


def test_the_verification_receipt_chain_link_verifies(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "print('gate ok')"]
    repo, base_commit = _fixture_repo(tmp_path, gates=[argv])
    patch = _real_patch(repo)
    gate_result = _real_gate_result(argv, cwd=repo)

    bundle_key = _key(seed_byte=5)
    bundle = _bundle(bundle_key, base_commit=base_commit, patch=patch, gates=(gate_result,))
    envelope = build_result_bundle(bundle, signing_key=bundle_key)
    result = verify_in_clean_room(envelope, repo_root=repo, public_key=bundle_key.public_key())
    assert result.passed is True

    verifier_key = _key(seed_byte=9)
    verifier_pub = verifier_key.public_key()
    verifier_pem = export_public_key_pem(verifier_pub).decode("ascii")
    verifier_keyid = keyid_from_public_key(verifier_pub)

    first = clean_room_receipt_from_result(
        result,
        chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
        verifier_keyid=verifier_keyid,
        verifier_public_key_pem=verifier_pem,
        created_at="2026-08-30T00:00:00Z",
    )
    first_envelope = build_clean_room_receipt(first, signing_key=verifier_key)
    first_verification = verify_clean_room_receipt(first_envelope, verifier_pub)
    assert first_verification.ok, first_verification.errors

    second = clean_room_receipt_from_result(
        result,
        chain=ChainLink(anchor=first.digest, length=2),
        verifier_keyid=verifier_keyid,
        verifier_public_key_pem=verifier_pem,
        created_at="2026-08-30T00:05:00Z",
    )
    second_envelope = build_clean_room_receipt(second, signing_key=verifier_key)

    good = verify_clean_room_receipt(second_envelope, verifier_pub, expected_prev_digest=first.digest)
    assert good.ok, good.errors
    assert good.prev_digest_checked is True

    bad = verify_clean_room_receipt(second_envelope, verifier_pub, expected_prev_digest="deadbeef")
    assert not bad.ok
    assert any("chain.anchor" in e for e in bad.errors)


# --------------------------------------------------------------------------
# 5. gate nondeterminism surfaces as divergence, not a silent pass
# --------------------------------------------------------------------------


def test_gate_nondeterminism_surfaces_as_divergence_not_a_silent_pass(tmp_path: Path) -> None:
    """A gate whose exit code is stable but whose OUTPUT genuinely varies.

    The attested log comes from one real execution; the clean room performs a
    SECOND, independent, real execution of the identical command. Nothing
    about the two runs' log text is hand-typed -- if this test is flaky
    because the two really did happen to match, that is a real property of
    the fixture command, not a stub standing in for one.
    """
    # A single expression for the same reason as the side-effect fixture
    # above: no ';' or '\n' allowed inside one gate argv element.
    argv = [sys.executable, "-c", "print(__import__('time').time_ns())"]
    repo, base_commit = _fixture_repo(tmp_path, gates=[argv])
    patch = _real_patch(repo)
    gate_result = _real_gate_result(argv, cwd=repo)
    assert gate_result.exit_code == 0

    key = _key()
    bundle = _bundle(key, base_commit=base_commit, patch=patch, gates=(gate_result,))
    envelope = build_result_bundle(bundle, signing_key=key)

    result = verify_in_clean_room(envelope, repo_root=repo, public_key=key.public_key())

    assert result.patch_applied is True
    assert len(result.divergences) == 1
    divergence = result.divergences[0]
    # exit code is stable across both real runs...
    assert divergence.exit_code_diverged is False
    # ...but the log text is not, and that must not be swallowed.
    assert divergence.diverged is True
    assert divergence in result.log_only_divergences
    assert divergence not in result.outcome_divergences
    # Still blocks submission by default -- see the module docstring's
    # exit-code-vs-log-digest note: a log-only mismatch is reported
    # differently from an outcome mismatch, but it is not silently ignored.
    assert result.passed is False
