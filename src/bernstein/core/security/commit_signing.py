"""Code signing for agent-produced commits with verifiable provenance.

Every commit made by a Bernstein agent can be signed with a GPG or SSH key that
identifies the specific agent, task, and run.  ``git log --show-signature``
reveals full provenance.  Enterprises with signed-commit policies can adopt
Bernstein without weakening their commit hygiene requirements.

Implementation strategy:

1. **Provenance trailers** — Bernstein metadata is always written to the commit
   message as ``Bernstein-*`` trailers (RFC-822 style), regardless of whether
   cryptographic signing is enabled.  This allows ``git log`` to show which
   agent produced the commit even without GPG/SSH key infrastructure.

2. **SSH signing** — When ``signing_key`` is provided and points to an SSH
   private-key file (or an SSH agent socket is available), the commit is signed
   with ``git commit -S`` after configuring ``gpg.format=ssh`` and
   ``user.signingKey`` in the worktree-local git config.  Produces
   ``git verify-commit`` verifiable signatures.

3. **GPG signing** — When ``signing_key`` is a GPG key fingerprint / e-mail,
   the commit is signed via ``git commit --gpg-sign=<key>``.

4. **No-key fallback** — When no signing key is configured the commit is created
   normally with only the provenance trailers embedded.  The ``signed=False``
   flag on the returned :class:`SignedCommitResult` signals this path.

Usage::

    from bernstein.core.commit_signing import CommitProvenance, sign_and_commit

    provenance = CommitProvenance(
        agent_id="claude-security-ec5bab8b",
        task_id="4848a987d67e",
        run_id="security-ec5bab8b",
        role="security",
        model="claude-sonnet-4-6",
    )
    result = sign_and_commit(
        cwd=Path("/path/to/worktree"),
        message="feat(security): add DLP scanner",
        provenance=provenance,
    )
    print(result.sha)   # commit hash
    print(result.signed)  # True when cryptographic signature was applied
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.git_basic import GitResult, run_git

logger = logging.getLogger(__name__)

__all__ = [
    "CommitProvenance",
    "SignedCommitResult",
    "SigningConfig",
    "build_provenance_trailers",
    "is_agent_commit",
    "read_commit_provenance",
    "sign_and_commit",
    "verify_commit_signature",
]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

SigningMode = Literal["ssh", "gpg", "none"]

_TRAILER_PREFIX = "Bernstein"
_TRAILER_KEYS = (
    "Agent-ID",
    "Task-ID",
    "Run-ID",
    "Role",
    "Model",
    "Timestamp",
)


@dataclass(frozen=True)
class CommitProvenance:
    """Identity metadata for a Bernstein agent commit.

    These fields are embedded as RFC-822-style trailers in the commit message
    so that ``git log`` always shows provenance — regardless of whether a
    cryptographic signature is present.

    Attributes:
        agent_id: Unique identifier for the agent instance that produced the commit.
        task_id: Bernstein task ID this commit addresses.
        run_id: Orchestration run / worktree ID.
        role: Agent role (e.g. ``"security"``, ``"backend"``).
        model: LLM model identifier (e.g. ``"claude-sonnet-4-6"``).
        timestamp: ISO 8601 UTC timestamp.  Defaults to now.
    """

    agent_id: str
    task_id: str
    run_id: str
    role: str = ""
    model: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class SigningConfig:
    """Configuration for commit signing.

    Attributes:
        mode: Signing backend — ``"ssh"``, ``"gpg"``, or ``"none"``.
        signing_key: Path to SSH private key file, or GPG key fingerprint /
            e-mail address.  Required when mode is ``"ssh"`` or ``"gpg"``.
        gpg_program: Path to GPG binary.  Uses git's default when empty.
        ssh_key_type: SSH key type hint for allowed-signers file validation
            (e.g. ``"ssh-ed25519"``).  Informational only.
    """

    mode: SigningMode = "none"
    signing_key: str = ""
    gpg_program: str = ""
    ssh_key_type: str = ""


@dataclass(frozen=True)
class SignedCommitResult:
    """Result of a sign-and-commit operation.

    Attributes:
        git_result: Raw GitResult from the commit command.
        sha: The new commit hash, or empty string if the commit failed.
        signed: True when a cryptographic signature was applied.
        signing_mode: Which signing backend was used.
        provenance: The provenance embedded in the commit.
        trailers: The raw trailer lines appended to the commit message.
    """

    git_result: GitResult
    sha: str
    signed: bool
    signing_mode: SigningMode
    provenance: CommitProvenance
    trailers: list[str]

    @property
    def ok(self) -> bool:
        """Return True when the commit succeeded."""
        return self.git_result.ok


# ---------------------------------------------------------------------------
# Provenance trailer builder
# ---------------------------------------------------------------------------


def build_provenance_trailers(provenance: CommitProvenance) -> list[str]:
    """Build Bernstein provenance trailer lines for a commit message.

    Returns lines in ``Key: value`` format, suitable for appending to a
    commit message body after a blank separator line.

    Args:
        provenance: Agent and task identity metadata.

    Returns:
        List of trailer strings (no trailing newlines).
    """
    trailers: list[str] = []
    if provenance.agent_id:
        trailers.append(f"{_TRAILER_PREFIX}-Agent-ID: {provenance.agent_id}")
    if provenance.task_id:
        trailers.append(f"{_TRAILER_PREFIX}-Task-ID: {provenance.task_id}")
    if provenance.run_id:
        trailers.append(f"{_TRAILER_PREFIX}-Run-ID: {provenance.run_id}")
    if provenance.role:
        trailers.append(f"{_TRAILER_PREFIX}-Role: {provenance.role}")
    if provenance.model:
        trailers.append(f"{_TRAILER_PREFIX}-Model: {provenance.model}")
    if provenance.timestamp:
        trailers.append(f"{_TRAILER_PREFIX}-Timestamp: {provenance.timestamp}")
    return trailers


def _append_trailers(message: str, trailers: list[str]) -> str:
    """Return *message* with provenance *trailers* appended.

    Ensures exactly one blank line separates the message body from the
    trailer block, as required by ``git interpret-trailers``.
    """
    if not trailers:
        return message
    body = message.rstrip("\n")
    trailer_block = "\n".join(trailers)
    return f"{body}\n\n{trailer_block}\n"


# ---------------------------------------------------------------------------
# Signing helpers
# ---------------------------------------------------------------------------


def _resolve_commit_sha(cwd: Path) -> str:
    """Return the HEAD commit hash after a successful commit."""
    result = run_git(["rev-parse", "HEAD"], cwd)
    return result.stdout.strip() if result.ok else ""


def _configure_ssh_signing(cwd: Path, signing_key: str) -> None:
    """Set worktree-local git config for SSH signing."""
    run_git(["config", "--local", "gpg.format", "ssh"], cwd)
    run_git(["config", "--local", "user.signingKey", signing_key], cwd)


def _configure_gpg_program(cwd: Path, gpg_program: str) -> None:
    """Override the GPG binary if a custom path is specified."""
    if gpg_program:
        run_git(["config", "--local", "gpg.program", gpg_program], cwd)


def _commit_with_ssh_signing(cwd: Path, message: str, signing_key: str) -> GitResult:
    """Commit with SSH signature via ``git commit -S``."""
    _configure_ssh_signing(cwd, signing_key)
    return run_git(["commit", "-S", "-m", message], cwd)


def _commit_with_gpg_signing(cwd: Path, message: str, signing_key: str, gpg_program: str) -> GitResult:
    """Commit with GPG signature."""
    _configure_gpg_program(cwd, gpg_program)
    if signing_key:
        return run_git(["commit", f"--gpg-sign={signing_key}", "-m", message], cwd)
    # Use the user's default signing key from global git config.
    return run_git(["commit", "--gpg-sign", "-m", message], cwd)


def _commit_plain(cwd: Path, message: str) -> GitResult:
    """Commit without cryptographic signature (provenance trailers only)."""
    return run_git(["commit", "-m", message], cwd)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sign_and_commit(
    cwd: Path,
    message: str,
    provenance: CommitProvenance,
    *,
    signing_config: SigningConfig | None = None,
) -> SignedCommitResult:
    """Create a commit with embedded provenance and optional cryptographic signing.

    Provenance trailers are always written to the commit message regardless of
    whether cryptographic signing succeeds.  If signing fails (missing key,
    GPG not installed, etc.) the function falls back to an unsigned commit so
    that agent work is never silently lost.

    Args:
        cwd: Repository root / worktree directory.
        message: Commit message (subject line, optionally with body).
        provenance: Bernstein agent and task identity metadata.
        signing_config: Signing backend configuration.  Defaults to unsigned.

    Returns:
        SignedCommitResult with the git result, commit SHA, and signing status.
    """
    config = signing_config or SigningConfig()
    trailers = build_provenance_trailers(provenance)
    full_message = _append_trailers(message, trailers)

    git_result: GitResult
    signed = False
    mode_used: SigningMode = "none"

    if config.mode == "ssh" and config.signing_key:
        try:
            git_result = _commit_with_ssh_signing(cwd, full_message, config.signing_key)
            if git_result.ok:
                signed = True
                mode_used = "ssh"
            else:
                logger.warning(
                    "SSH-signed commit failed (%s), falling back to unsigned: %s",
                    git_result.returncode,
                    git_result.stderr[:200],
                )
                git_result = _commit_plain(cwd, full_message)
        except Exception as exc:
            logger.warning("SSH signing error, falling back to unsigned: %s", exc)
            git_result = _commit_plain(cwd, full_message)

    elif config.mode == "gpg":
        try:
            git_result = _commit_with_gpg_signing(cwd, full_message, config.signing_key, config.gpg_program)
            if git_result.ok:
                signed = True
                mode_used = "gpg"
            else:
                logger.warning(
                    "GPG-signed commit failed (%s), falling back to unsigned: %s",
                    git_result.returncode,
                    git_result.stderr[:200],
                )
                git_result = _commit_plain(cwd, full_message)
        except Exception as exc:
            logger.warning("GPG signing error, falling back to unsigned: %s", exc)
            git_result = _commit_plain(cwd, full_message)

    else:
        # No signing requested — provenance trailers only.
        git_result = _commit_plain(cwd, full_message)
        mode_used = "none"

    sha = _resolve_commit_sha(cwd) if git_result.ok else ""

    return SignedCommitResult(
        git_result=git_result,
        sha=sha,
        signed=signed,
        signing_mode=mode_used,
        provenance=provenance,
        trailers=trailers,
    )


def verify_commit_signature(cwd: Path, commit_ref: str = "HEAD") -> tuple[bool, str]:
    """Verify the cryptographic signature of a commit.

    Uses ``git verify-commit`` which works for both GPG and SSH signatures.

    Args:
        cwd: Repository root.
        commit_ref: Git ref to verify (default: ``HEAD``).

    Returns:
        Tuple of (verified: bool, detail: str).
    """
    result = run_git(["verify-commit", commit_ref], cwd)
    if result.ok:
        return True, result.stderr.strip() or result.stdout.strip()
    return False, result.stderr.strip() or "Signature verification failed"


def read_commit_provenance(cwd: Path, commit_ref: str = "HEAD") -> dict[str, str]:
    """Read Bernstein provenance trailers from a commit message.

    Parses ``Bernstein-*`` trailers from the commit message and returns them
    as a plain dict.  Returns an empty dict if no trailers are found or the
    commit cannot be read.

    Args:
        cwd: Repository root.
        commit_ref: Git ref to read (default: ``HEAD``).

    Returns:
        Dict mapping trailer key (without ``Bernstein-`` prefix) to value.
        E.g. ``{"Agent-ID": "...", "Task-ID": "...", "Run-ID": "..."}``.
    """
    result = run_git(["log", "-1", "--format=%B", commit_ref], cwd)
    if not result.ok:
        return {}

    provenance: dict[str, str] = {}
    prefix = f"{_TRAILER_PREFIX}-"
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            rest = line[len(prefix) :]
            if ": " in rest:
                key, _, value = rest.partition(": ")
                provenance[key.strip()] = value.strip()
    return provenance


def is_agent_commit(cwd: Path, commit_ref: str = "HEAD") -> bool:
    """Return True when the commit was produced by a Bernstein agent.

    Checks for the presence of at least one ``Bernstein-Agent-ID`` trailer.

    Args:
        cwd: Repository root.
        commit_ref: Git ref to check (default: ``HEAD``).
    """
    provenance = read_commit_provenance(cwd, commit_ref)
    return "Agent-ID" in provenance
