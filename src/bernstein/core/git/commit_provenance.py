"""Commit provenance tracking for agent-produced commits.

Provides verifiable provenance chains that map each commit back to the
agent, task, and run that produced it.  Supports SSH and GPG signing
plus SLSA-like attestation generation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.tasks.models import AgentSession

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data models
# ------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceRecord:
    """Single commit provenance entry.

    Attributes:
        commit_sha: Full 40-character commit SHA.
        agent_id: Identifier of the agent session that created the commit.
        task_id: Task the commit was produced for.
        run_id: Orchestrator run that owns this commit.
        model: Model identifier used by the agent.
        role: Agent role (e.g. ``backend``, ``qa``).
        timestamp: Unix epoch when the record was created.
        signature_type: Type of cryptographic signature applied.
    """

    commit_sha: str
    agent_id: str
    task_id: str
    run_id: str
    model: str
    role: str
    timestamp: float
    signature_type: Literal["ssh", "gpg", "none"]


@dataclass(frozen=True)
class ProvenanceChain:
    """Ordered chain of provenance records for a single run.

    Attributes:
        records: Immutable sequence of provenance records.
        run_id: Orchestrator run identifier.
        verified: True when every record's commit signature has been validated.
    """

    records: tuple[ProvenanceRecord, ...]
    run_id: str
    verified: bool


# ------------------------------------------------------------------
# Record creation
# ------------------------------------------------------------------


def create_provenance_record(
    commit_sha: str,
    session: AgentSession,
    run_id: str,
) -> ProvenanceRecord:
    """Build a provenance record from an agent session.

    Args:
        commit_sha: The SHA of the commit to track.
        session: Active agent session with identity metadata.
        run_id: Current orchestrator run identifier.

    Returns:
        A frozen :class:`ProvenanceRecord`.
    """
    task_id = session.task_ids[0] if session.task_ids else ""
    return ProvenanceRecord(
        commit_sha=commit_sha,
        agent_id=session.id,
        task_id=task_id,
        run_id=run_id,
        model=session.model_config.model,
        role=session.role,
        timestamp=time.time(),
        signature_type="none",
    )


# ------------------------------------------------------------------
# Signing
# ------------------------------------------------------------------


def sign_commit_ssh(commit_sha: str, key_path: Path, *, workdir: Path) -> bool:
    """Sign a commit with an SSH key via ``git commit --amend``.

    The function configures the repository for SSH signing, then amends
    the commit at HEAD (which must match *commit_sha*) to attach the
    signature.

    Args:
        commit_sha: Expected HEAD SHA to sign.
        key_path: Path to the SSH private key.
        workdir: Repository working directory.

    Returns:
        True if the signing succeeded, False otherwise.
    """
    head = run_git(["rev-parse", "HEAD"], workdir)
    if not head.ok or head.stdout.strip() != commit_sha:
        logger.warning(
            "HEAD %s does not match requested commit %s; skipping sign",
            head.stdout.strip(),
            commit_sha,
        )
        return False

    config_cmds: list[list[str]] = [
        ["config", "gpg.format", "ssh"],
        ["config", "user.signingkey", str(key_path)],
    ]
    for cmd in config_cmds:
        result = run_git(cmd, workdir)
        if not result.ok:
            logger.error("Failed to configure SSH signing: %s", result.stderr)
            return False

    sign_result = run_git(
        ["commit", "--amend", "--no-edit", "-S"],
        workdir,
        timeout=60,
    )
    if not sign_result.ok:
        logger.error("SSH signing failed: %s", sign_result.stderr)
        return False

    logger.info("Signed commit %s with SSH key %s", commit_sha, key_path)
    return True


# ------------------------------------------------------------------
# Verification
# ------------------------------------------------------------------

_GOOD_SIG_MARKERS: tuple[str, ...] = (
    'Good "git" signature',
    "Good signature from",
    "gpg: Good signature",
)


def verify_commit_signature(commit_sha: str, workdir: Path) -> bool:
    """Verify the cryptographic signature on a commit.

    Runs ``git log --show-signature -1`` and inspects the output for
    known "good signature" markers produced by Git's SSH and GPG
    verification.

    Args:
        commit_sha: Commit to verify.
        workdir: Repository working directory.

    Returns:
        True if a valid signature was found, False otherwise.
    """
    result = run_git(
        ["log", "--show-signature", "-1", "--format=%H", commit_sha],
        workdir,
        timeout=30,
    )
    if not result.ok:
        logger.debug("git log --show-signature failed: %s", result.stderr)
        return False

    combined = result.stdout + result.stderr
    return any(marker in combined for marker in _GOOD_SIG_MARKERS)


# ------------------------------------------------------------------
# Chain construction
# ------------------------------------------------------------------


def build_provenance_chain(
    run_id: str,
    archive_path: Path,
) -> ProvenanceChain:
    """Build a provenance chain from stored records on disk.

    Reads ``provenance.jsonl`` from *archive_path* where each line is a
    JSON-serialised :class:`ProvenanceRecord`.

    Args:
        run_id: Run identifier to filter by.
        archive_path: Directory containing ``provenance.jsonl``.

    Returns:
        A :class:`ProvenanceChain` (unverified).
    """
    jsonl_path = archive_path / "provenance.jsonl"
    if not jsonl_path.exists():
        logger.warning("No provenance archive at %s", jsonl_path)
        return ProvenanceChain(records=(), run_id=run_id, verified=False)

    records: list[ProvenanceRecord] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        if data.get("run_id") != run_id:
            continue
        records.append(
            ProvenanceRecord(
                commit_sha=data["commit_sha"],
                agent_id=data["agent_id"],
                task_id=data["task_id"],
                run_id=data["run_id"],
                model=data["model"],
                role=data["role"],
                timestamp=float(data["timestamp"]),
                signature_type=data.get("signature_type", "none"),
            )
        )

    records.sort(key=lambda r: r.timestamp)
    return ProvenanceChain(records=tuple(records), run_id=run_id, verified=False)


# ------------------------------------------------------------------
# Attestation
# ------------------------------------------------------------------


def generate_provenance_attestation(chain: ProvenanceChain) -> dict[str, Any]:
    """Generate a SLSA-like provenance attestation for a chain.

    The output follows the general shape of an `in-toto Statement`_
    with ``predicateType`` set to a Bernstein-specific URI.

    Args:
        chain: Provenance chain to attest.

    Returns:
        Attestation dictionary suitable for JSON serialisation.

    .. _in-toto Statement: https://in-toto.io/Statement/v1
    """
    subjects = [{"name": r.commit_sha, "digest": {"gitCommit": r.commit_sha}} for r in chain.records]

    materials = [
        {
            "commit_sha": r.commit_sha,
            "agent_id": r.agent_id,
            "task_id": r.task_id,
            "model": r.model,
            "role": r.role,
            "timestamp": r.timestamp,
            "signature_type": r.signature_type,
        }
        for r in chain.records
    ]

    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://bernstein.dev/provenance/v1",
        "subject": subjects,
        "predicate": {
            "buildType": "https://bernstein.dev/AgentOrchestration/v1",
            "builder": {"id": "bernstein-orchestrator"},
            "metadata": {
                "run_id": chain.run_id,
                "verified": chain.verified,
                "record_count": len(chain.records),
            },
            "materials": materials,
        },
    }


# ------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------


def render_provenance_report(chain: ProvenanceChain) -> str:
    """Render a Markdown table mapping commits to agents and tasks.

    Args:
        chain: Provenance chain to render.

    Returns:
        Markdown string with a summary table.
    """
    lines: list[str] = []
    lines.append(f"# Provenance Report -- run {chain.run_id}")
    lines.append("")
    lines.append(f"**Verified:** {'yes' if chain.verified else 'no'}  ")
    lines.append(f"**Records:** {len(chain.records)}")
    lines.append("")

    if not chain.records:
        lines.append("_No provenance records found._")
        return "\n".join(lines)

    lines.append("| Commit | Agent | Task | Model | Role | Signature |")
    lines.append("|--------|-------|------|-------|------|-----------|")

    for r in chain.records:
        short_sha = r.commit_sha[:8]
        lines.append(f"| `{short_sha}` | {r.agent_id} | {r.task_id} | {r.model} | {r.role} | {r.signature_type} |")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Persistence helper
# ------------------------------------------------------------------


def append_provenance_record(record: ProvenanceRecord, archive_path: Path) -> None:
    """Append a provenance record to the JSONL archive.

    Args:
        record: Record to persist.
        archive_path: Directory that will contain ``provenance.jsonl``.
    """
    archive_path.mkdir(parents=True, exist_ok=True)
    jsonl_path = archive_path / "provenance.jsonl"
    entry = {
        "commit_sha": record.commit_sha,
        "agent_id": record.agent_id,
        "task_id": record.task_id,
        "run_id": record.run_id,
        "model": record.model,
        "role": record.role,
        "timestamp": record.timestamp,
        "signature_type": record.signature_type,
    }
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
