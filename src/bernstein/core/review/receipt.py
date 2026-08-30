"""Attested pull-request review receipts (issue #2296).

A worker that reviews a PR must leave behind a single artefact that binds the
*issue body*, the *plan*, every *tool call* (the run journal head), and the
resulting *diff* -- and that artefact must recompute offline so a reviewer can
prove the PR was generated from the ticket without operator override. This
module makes the artefact the operator consumes *be* that proof: a
:class:`ReviewReceipt` is not "a review plus an audit line", it is a signed,
spine-anchored record whose identity is a lineage-spine entry hash. Strip the
spine and the signature and the receipt is just a file; anchored and signed it
is a chain-verifiable attestation that recomputes from the PR alone.

Shapes
------
* :class:`ReviewReceipt` -- binds ``{issue_hash, plan_hash, journal_head,
  diff_hash, findings, verdict}`` (AC1). Signed with the install's Ed25519
  identity and anchored: ``journal_entry_hash`` is the review-spine entry hash
  over the receipt's canonical binding bytes.
* :class:`AutofixReceipt` -- links a reviewer finding to the fix commit and the
  gate result (AC3), produced only after the fix ran in an isolated worktree.
* A *chain* of :class:`ReviewReceipt` -- one per pass of a fix-until-green
  review contour (#4481). Each pass additionally binds its ``pass_index``, the
  ``ruleset_digest`` its verdict was produced under, and the previous pass's
  anchor, so :func:`verify_review_chain` recomputes the whole sequence offline.
  Those three fields are omitted from the signed binding while unset, so a
  single-pass receipt is byte-identical to one emitted before the contour
  existed.

Determinism
-----------
Every hash is a pure ``sha256`` of canonical bytes (``compute_issue_hash`` /
``compute_plan_hash`` / ``compute_diff_hash``), and the receipt binding is
canonical JSON (sorted keys, minimal separators, UTF-8), so identical inputs
anchor byte-identically and two verifiers arrive at the same result.

Isolation (AC3)
---------------
:func:`run_autofix_in_worktree` checks the fix out into a throwaway
``git worktree`` that is never the primary repo, runs the caller's fix and gate
closures against it, records the fix commit, tears the worktree down, then emits
an :class:`AutofixReceipt`. The primary working tree is never mutated by the
fix.

Verifiability (AC2, AC4)
------------------------
:func:`verify_review_receipt` recomputes ``issue_hash`` and ``diff_hash`` from
the presented PR inputs, checks the Ed25519 signature against the receipt's
embedded public key, and re-anchors the receipt against the review spine. A
single-byte edit to the diff, the issue body, the receipt, or the spine fails
the check.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Run id under which every review receipt is anchored. Review lineage is kept
#: in one dedicated run so it never interleaves with per-task journals.
REVIEW_RUN_ID = "reviews"

#: Run id for autofix finding-to-fix-to-test receipts.
AUTOFIX_RUN_ID = "review-autofix"

#: Actor recorded on receipt spine entries.
_REVIEW_ACTOR = "bernstein.review_receipt"

#: Model string recorded on receipt spine entries (no model runs at anchor
#: time; the field is part of the spine schema).
_REVIEW_MODEL = "none"

#: Version stamped into every receipt binding preimage. Bump only on a
#: wire-format change.
REVIEW_SCHEMA_VERSION = 1

_RECEIPT_SUBPATH = (".sdd", "reviews", "receipts")
_AUTOFIX_SUBPATH = (".sdd", "reviews", "autofix")
_APPROVAL_SUBPATH = (".sdd", "reviews", "approvals")
_IDENTITY_PRIVATE_NAME = "review-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "review-identity-public.pem"


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def compute_issue_hash(issue_body: str) -> str:
    """Return the content hash of an issue body (the ticket the PR answers)."""
    return _sha256_bytes(issue_body.encode("utf-8"))


def compute_plan_hash(plan: str) -> str:
    """Return the content hash of the worker's plan."""
    return _sha256_bytes(plan.encode("utf-8"))


def compute_diff_hash(diff: bytes) -> str:
    """Return the content hash of the PR diff bytes."""
    return _sha256_bytes(diff)


# ---------------------------------------------------------------------------
# Install identity (Ed25519), persisted so verify is offline
# ---------------------------------------------------------------------------


def _safe_pr_name(pr_url: str) -> str:
    """Return a filesystem-safe basename for a PR url.

    The url is content-hashed so the name is portable and cannot introduce a
    path separator regardless of the url's shape.
    """
    if not pr_url:
        raise ValueError("empty pr_url")
    return hashlib.sha256(pr_url.encode("utf-8")).hexdigest()


def _safe_finding_name(finding_hash: str) -> str:
    if not finding_hash:
        raise ValueError("empty finding_hash")
    if "/" in finding_hash or "\\" in finding_hash or "\x00" in finding_hash:
        raise ValueError(f"finding_hash contains an unsafe character: {finding_hash!r}")
    return finding_hash.replace(":", "_")


def load_or_create_review_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's Ed25519 review identity.

    The keypair is persisted under ``identity_dir`` so the same install signs
    every receipt and a verifier can check the signature offline against the
    embedded public key. The private key file is written with ``0600`` mode.

    Args:
        identity_dir: Directory holding the persisted PEM pair.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    private_path = identity_dir / _IDENTITY_PRIVATE_NAME
    public_path = identity_dir / _IDENTITY_PUBLIC_NAME
    if private_path.is_file() and public_path.is_file():
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    identity_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(".pem.tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One reviewer finding carried on a receipt.

    Attributes:
        rule: The rule / check id (e.g. ``BLE001``).
        path: Repo-relative path the finding anchors to.
        line: 1-based line the finding anchors to.
        summary: One-line statement of the finding.
    """

    rule: str
    path: str
    line: int
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "path": self.path, "line": self.line, "summary": self.summary}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Finding:
        return cls(
            rule=str(row["rule"]),
            path=str(row["path"]),
            line=int(row["line"]),
            summary=str(row["summary"]),
        )

    def finding_hash(self) -> str:
        """Return the content hash of this finding."""
        return _sha256_bytes(_canonical_bytes(self.to_dict()))


# ---------------------------------------------------------------------------
# ReviewReceipt -- the signed, spine-anchored primary artefact (AC1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewReceipt:
    """The record binding issue, plan, tool calls (journal head), and diff.

    Attributes:
        pr_url: The pull request the review covers.
        repo: ``owner/repo`` slug.
        issue_hash: Content hash of the issue body the PR answers.
        plan_hash: Content hash of the worker's plan.
        journal_head: The run journal Merkle head -- the identity of every
            tool call the worker executed producing the diff.
        diff_hash: Content hash of the PR diff.
        findings: Ordered reviewer findings.
        verdict: The review verdict (``approve`` / ``request_changes`` / ...).
        task_id: The task the review was attributed to.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        signer_public_key_pem: The install's Ed25519 public key; a verifier
            checks the signature against it offline.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The review-spine entry hash anchoring the receipt.
        pass_index: 1-based pass of a multi-pass review contour; ``0`` for a
            single-pass review.
        ruleset_digest: Digest of the ruleset the verdict was produced under;
            empty when the review ran without one.
        prev_entry_hash: The preceding pass's ``journal_entry_hash``, which is
            what links one pass's receipt to the next.
    """

    pr_url: str
    repo: str
    issue_hash: str
    plan_hash: str
    journal_head: str
    diff_hash: str
    findings: tuple[Finding, ...]
    verdict: str
    task_id: str
    timestamp: int
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""
    pass_index: int = 0
    ruleset_digest: str = ""
    prev_entry_hash: str = ""
    resolution_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the signed + anchored binding (no signature / anchor).

        The contour fields are omitted while unset so a single-pass receipt
        binds -- and therefore recomputes -- byte-identically to one emitted
        before multi-pass reviews existed.
        """
        binding: dict[str, Any] = {
            "v": REVIEW_SCHEMA_VERSION,
            "pr_url": self.pr_url,
            "repo": self.repo,
            "issue_hash": self.issue_hash,
            "plan_hash": self.plan_hash,
            "journal_head": self.journal_head,
            "diff_hash": self.diff_hash,
            "findings": [f.to_dict() for f in self.findings],
            "verdict": self.verdict,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }
        if self.pass_index:
            binding["pass_index"] = self.pass_index
        if self.ruleset_digest:
            binding["ruleset_digest"] = self.ruleset_digest
        if self.prev_entry_hash:
            binding["prev_entry_hash"] = self.prev_entry_hash
        binding["resolution_hash"] = self.resolution_hash
        return binding

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (signed + spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> ReviewReceipt:
        row = json.loads(raw)
        return cls(
            pr_url=str(row["pr_url"]),
            repo=str(row["repo"]),
            issue_hash=str(row["issue_hash"]),
            plan_hash=str(row["plan_hash"]),
            journal_head=str(row["journal_head"]),
            diff_hash=str(row["diff_hash"]),
            findings=tuple(Finding.from_dict(f) for f in row.get("findings", [])),
            verdict=str(row["verdict"]),
            task_id=str(row["task_id"]),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
            pass_index=int(row.get("pass_index", 0)),
            ruleset_digest=str(row.get("ruleset_digest", "")),
            prev_entry_hash=str(row.get("prev_entry_hash", "")),
            resolution_hash=str(row.get("resolution_hash", "")),
        )


def receipt_path(workdir: Path, pr_url: str, pass_index: int = 0) -> Path:
    """Return the on-disk review-receipt path for ``pr_url``.

    Args:
        workdir: Project root.
        pr_url: The pull request the receipt covers.
        pass_index: 1-based contour pass; ``0`` is the single-pass receipt and
            keeps the path it has always had.
    """
    name = _safe_pr_name(pr_url)
    if pass_index > 0:
        name = f"{name}-p{pass_index}"
    return workdir.joinpath(*_RECEIPT_SUBPATH, f"{name}.json")


def read_review_receipt(workdir: Path, pr_url: str, pass_index: int = 0) -> ReviewReceipt | None:
    """Return the review receipt for ``pr_url`` or ``None`` if absent."""
    path = receipt_path(workdir, pr_url, pass_index)
    if not path.is_file():
        return None
    try:
        return ReviewReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("review: malformed receipt at %s", path)
        return None


def read_review_chain(workdir: Path, pr_url: str) -> tuple[ReviewReceipt, ...]:
    """Return the contour's per-pass receipts for ``pr_url``, in pass order.

    The walk stops at the first missing pass, so a truncated chain is returned
    as what is actually on disk rather than silently skipping a gap.
    """
    chain: list[ReviewReceipt] = []
    index = 1
    while True:
        receipt = read_review_receipt(workdir, pr_url, pass_index=index)
        if receipt is None:
            return tuple(chain)
        chain.append(receipt)
        index += 1


# ---------------------------------------------------------------------------
# Emit (AC1)
# ---------------------------------------------------------------------------


def emit_review_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    pr_url: str,
    repo: str,
    issue_body: str,
    plan: str,
    journal_head: str,
    diff: bytes,
    findings: tuple[Finding, ...],
    verdict: str,
    task_id: str,
    timestamp: int,
    pass_index: int = 0,
    ruleset_digest: str = "",
    prev_entry_hash: str = "",
    resolution_hash: str = "",
) -> ReviewReceipt:
    """Bind issue, plan, tool calls, and diff into a signed, anchored receipt.

    The receipt's canonical binding bytes are signed with the install's
    Ed25519 identity and are exactly the bytes the review spine hashes, so the
    returned receipt's ``signature`` and ``journal_entry_hash`` are its
    chain-verifiable identity (AC1). The receipt is persisted for offline
    verification.

    Args:
        workdir: Project root; the receipt lands under
            ``.sdd/reviews/receipts/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        private_key_pem: The install's Ed25519 private key (PEM).
        public_key_pem: The matching public key, embedded on the receipt.
        pr_url: The pull request under review.
        repo: ``owner/repo`` slug.
        issue_body: The issue body the PR answers; hashed into ``issue_hash``.
        plan: The worker plan; hashed into ``plan_hash``.
        journal_head: The run journal Merkle head (every tool call executed).
        diff: The PR diff bytes; hashed into ``diff_hash``.
        findings: Ordered reviewer findings.
        verdict: The review verdict.
        task_id: The task the review is attributed to.
        timestamp: Integer timestamp for the receipt.
        pass_index: 1-based pass of a multi-pass review contour; ``0`` emits
            the single-pass receipt at its historical path and binding.
        ruleset_digest: Digest of the ruleset the verdict was produced under.
        prev_entry_hash: The preceding pass's ``journal_entry_hash``.

    Returns:
        The signed, anchored :class:`ReviewReceipt`.
    """
    unsigned = ReviewReceipt(
        pr_url=pr_url,
        repo=repo,
        issue_hash=compute_issue_hash(issue_body),
        plan_hash=compute_plan_hash(plan),
        journal_head=journal_head,
        diff_hash=compute_diff_hash(diff),
        findings=findings,
        verdict=verdict,
        task_id=task_id,
        timestamp=timestamp,
        pass_index=pass_index,
        ruleset_digest=ruleset_digest,
        prev_entry_hash=prev_entry_hash,
        resolution_hash=resolution_hash,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)

    spine = LineageSpine(lineage_root, run_id=REVIEW_RUN_ID, hmac_key=hmac_key)
    path = receipt_path(workdir, pr_url, pass_index)
    artifact_path = "/".join((*_RECEIPT_SUBPATH, path.name))
    # Two passes over an unchanged diff must still anchor distinctly, so the
    # pass index rides along in the step id.
    step_id = f"{unsigned.diff_hash}#p{pass_index}" if pass_index > 0 else unsigned.diff_hash
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_REVIEW_ACTOR,
        step_id=step_id,
        model=_REVIEW_MODEL,
        timestamp=timestamp,
    )
    anchored = ReviewReceipt(
        pr_url=unsigned.pr_url,
        repo=unsigned.repo,
        issue_hash=unsigned.issue_hash,
        plan_hash=unsigned.plan_hash,
        journal_head=unsigned.journal_head,
        diff_hash=unsigned.diff_hash,
        findings=unsigned.findings,
        verdict=unsigned.verdict,
        task_id=unsigned.task_id,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
        pass_index=pass_index,
        ruleset_digest=ruleset_digest,
        prev_entry_hash=prev_entry_hash,
        resolution_hash=resolution_hash,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


# ---------------------------------------------------------------------------
# Verify (AC2, AC4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewVerifyResult:
    """Outcome of :func:`verify_review_receipt`."""

    ok: bool
    reason: str
    receipt: ReviewReceipt | None = None
    verdict: str = ""


def _recompute_anchor(spine: LineageSpine, canonical: bytes) -> str | None:
    """Return the spine entry hash whose content matches ``canonical`` bytes."""
    want = content_hash_of(canonical)
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


def verify_review_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    pr_url: str,
    issue_body: str,
    diff: bytes,
) -> ReviewVerifyResult:
    """Prove offline that ``pr_url``'s diff was reviewed against the issue (AC2).

    Recomputes, from the recorded receipt and the presented PR inputs alone:

    * ``issue_hash`` from the presented issue body and ``diff_hash`` from the
      presented diff match the receipt (AC4 -- a tampered diff diverges);
    * the Ed25519 signature checks out against the receipt's embedded public
      key over the canonical binding (no operator override to the binding);
    * the receipt's ``journal_entry_hash`` still equals the spine entry hash
      over the receipt's canonical bytes, and the review spine verifies.

    A single-byte edit to the diff, the issue body, the receipt, or the spine
    fails the check. ``ok`` is True only when every recomputation matches.
    """
    receipt = read_review_receipt(workdir, pr_url)
    if receipt is None:
        return ReviewVerifyResult(ok=False, reason="no review receipt found")

    if compute_issue_hash(issue_body) != receipt.issue_hash:
        return ReviewVerifyResult(
            ok=False,
            reason="issue_hash mismatch: presented issue body differs from the reviewed ticket",
            receipt=receipt,
        )
    if compute_diff_hash(diff) != receipt.diff_hash:
        return ReviewVerifyResult(
            ok=False,
            reason="diff_hash mismatch: presented diff differs from the reviewed diff",
            receipt=receipt,
        )

    if not receipt.resolution_hash:
        return ReviewVerifyResult(
            ok=False,
            reason="receipt carries no resolution_hash: conventions in scope were not recorded",
            receipt=receipt,
        )

    if not receipt.signature or not receipt.signer_public_key_pem:
        return ReviewVerifyResult(ok=False, reason="receipt is unsigned", receipt=receipt)
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return ReviewVerifyResult(
            ok=False,
            reason=f"signature does not verify ({outcome.reason})",
            receipt=receipt,
        )

    spine = LineageSpine(lineage_root, run_id=REVIEW_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return ReviewVerifyResult(
            ok=False,
            reason=f"review spine failed verification ({spine_result.status.value})",
            receipt=receipt,
        )
    recomputed = _recompute_anchor(spine, receipt.to_canonical_bytes())
    if recomputed is None:
        return ReviewVerifyResult(ok=False, reason="receipt is not anchored in the review spine", receipt=receipt)
    if recomputed != receipt.journal_entry_hash:
        return ReviewVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the receipt bytes",
            receipt=receipt,
        )

    return ReviewVerifyResult(ok=True, reason="", receipt=receipt, verdict=receipt.verdict)


# ---------------------------------------------------------------------------
# Chain verify -- one receipt per pass of a fix-until-green contour (#4481)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewChainVerifyResult:
    """Outcome of :func:`verify_review_chain`.

    Attributes:
        ok: True only when every pass recomputed and every link held.
        reason: Why the chain was rejected; empty when ``ok``.
        passes: Number of per-pass receipts walked.
        verdict: The last pass's verdict.
        ruleset_digest: The ruleset digest every pass was produced under.
    """

    ok: bool
    reason: str
    passes: int = 0
    verdict: str = ""
    ruleset_digest: str = ""


def _verify_one(
    receipt: ReviewReceipt,
    *,
    spine: LineageSpine,
    issue_body: str,
) -> str:
    """Return the rejection reason for one receipt, or ``""`` when it holds."""
    if compute_issue_hash(issue_body) != receipt.issue_hash:
        return "issue_hash mismatch: presented issue body differs from the reviewed ticket"
    if not receipt.resolution_hash:
        return "receipt carries no resolution_hash: conventions in scope were not recorded"
    if not receipt.signature or not receipt.signer_public_key_pem:
        return "receipt is unsigned"
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return f"signature does not verify ({outcome.reason})"
    recomputed = _recompute_anchor(spine, receipt.to_canonical_bytes())
    if recomputed is None:
        return "receipt is not anchored in the review spine"
    if recomputed != receipt.journal_entry_hash:
        return "recorded journal_entry_hash does not match the spine anchor over the receipt bytes"
    return ""


def verify_review_chain(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    pr_url: str,
    issue_body: str,
    diff: bytes | None = None,
    ruleset_digest: str | None = None,
) -> ReviewChainVerifyResult:
    """Prove offline that every pass of a review contour holds.

    Walks the per-pass receipts in order and, for each, recomputes the issue
    hash, checks the Ed25519 signature over the canonical binding, re-anchors
    the receipt against the review spine, and checks that its recorded
    ``prev_entry_hash`` is the previous pass's anchor. Because the binding
    carries the reviewed diff hash and the ruleset digest, editing either one
    on disk breaks that pass's signature; presenting a different diff or a
    different ruleset breaks the comparison below.

    Args:
        workdir: Project root holding ``.sdd/reviews/receipts/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        pr_url: The pull request the chain covers.
        issue_body: The issue body every pass was reviewed against.
        diff: When supplied, checked against the *last* pass's ``diff_hash``.
        ruleset_digest: When supplied, checked against every pass.

    Returns:
        A :class:`ReviewChainVerifyResult`.
    """
    chain = read_review_chain(workdir, pr_url)
    if not chain:
        return ReviewChainVerifyResult(ok=False, reason="no review chain found")

    spine = LineageSpine(lineage_root, run_id=REVIEW_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return ReviewChainVerifyResult(
            ok=False,
            reason=f"review spine failed verification ({spine_result.status.value})",
            passes=len(chain),
        )

    declared = chain[0].ruleset_digest
    previous = ""
    for expected, receipt in enumerate(chain, start=1):
        if receipt.pass_index != expected:
            return ReviewChainVerifyResult(
                ok=False,
                reason=f"pass {expected}: recorded pass_index {receipt.pass_index} breaks the chain order",
                passes=len(chain),
            )
        if receipt.prev_entry_hash != previous:
            return ReviewChainVerifyResult(
                ok=False,
                reason=f"pass {expected}: chain link does not match the previous pass's anchor",
                passes=len(chain),
            )
        if receipt.ruleset_digest != declared:
            return ReviewChainVerifyResult(
                ok=False,
                reason=f"pass {expected}: ruleset digest differs from the ruleset pass 1 was reviewed under",
                passes=len(chain),
            )
        if ruleset_digest is not None and receipt.ruleset_digest != ruleset_digest:
            return ReviewChainVerifyResult(
                ok=False,
                reason=f"pass {expected}: ruleset digest mismatch: presented ruleset differs from the reviewed one",
                passes=len(chain),
            )
        reason = _verify_one(receipt, spine=spine, issue_body=issue_body)
        if reason:
            return ReviewChainVerifyResult(ok=False, reason=f"pass {expected}: {reason}", passes=len(chain))
        previous = receipt.journal_entry_hash

    if diff is not None and compute_diff_hash(diff) != chain[-1].diff_hash:
        return ReviewChainVerifyResult(
            ok=False,
            reason="diff_hash mismatch: presented diff differs from the last reviewed diff",
            passes=len(chain),
        )

    return ReviewChainVerifyResult(
        ok=True,
        reason="",
        passes=len(chain),
        verdict=chain[-1].verdict,
        ruleset_digest=declared,
    )


# ---------------------------------------------------------------------------
# Autofix -- isolated worktree + finding-to-fix-to-test receipt (AC3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutofixReceipt:
    """The record linking a reviewer finding to its fix commit and gate result.

    Attributes:
        finding_hash: Content hash of the reviewer finding that spawned the fix.
        fix_commit_hash: The commit the fix produced in the isolated worktree.
        gate_passed: Whether the gate (tests/lint/types) passed on the fix.
        gate_summary: Short human-readable gate result.
        task_id: The task the autofix was attributed to.
        timestamp: Integer timestamp for the receipt.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The autofix-spine entry hash anchoring the receipt.
        thread_id: GraphQL node ID of the review thread (optional).
        thread_resolved: Whether the thread was resolved (optional).
        resolution_reply: Text posted to resolve the thread (optional).
        declined_reason: Reason the fix was not applied (optional).
    """

    finding_hash: str
    fix_commit_hash: str
    gate_passed: bool
    gate_summary: str
    task_id: str
    timestamp: int
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""
    thread_id: str | None = None
    thread_resolved: bool | None = None
    resolution_reply: str | None = None
    declined_reason: str | None = None

    def _binding(self) -> dict[str, Any]:
        binding: dict[str, Any] = {
            "v": REVIEW_SCHEMA_VERSION,
            "finding_hash": self.finding_hash,
            "fix_commit_hash": self.fix_commit_hash,
            "gate_passed": self.gate_passed,
            "gate_summary": self.gate_summary,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }
        if self.thread_id is not None:
            binding["thread_id"] = self.thread_id
        if self.thread_resolved is not None:
            binding["thread_resolved"] = self.thread_resolved
        if self.resolution_reply is not None:
            binding["resolution_reply"] = self.resolution_reply
        if self.declined_reason is not None:
            binding["declined_reason"] = self.declined_reason
        return binding

    def to_canonical_bytes(self) -> bytes:
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        base = self._binding()
        base["signer_public_key_pem"] = self.signer_public_key_pem
        base["signature"] = self.signature
        base["journal_entry_hash"] = self.journal_entry_hash
        return base

    @classmethod
    def from_bytes(cls, raw: bytes) -> AutofixReceipt:
        row = json.loads(raw)
        return cls(
            finding_hash=str(row["finding_hash"]),
            fix_commit_hash=str(row["fix_commit_hash"]),
            gate_passed=bool(row["gate_passed"]),
            gate_summary=str(row["gate_summary"]),
            task_id=str(row["task_id"]),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
            thread_id=str(row["thread_id"]) if "thread_id" in row else None,
            thread_resolved=bool(row["thread_resolved"]) if "thread_resolved" in row else None,
            resolution_reply=str(row["resolution_reply"]) if "resolution_reply" in row else None,
            declined_reason=str(row["declined_reason"]) if "declined_reason" in row else None,
        )


def autofix_receipt_path(workdir: Path, finding_hash: str) -> Path:
    """Return the on-disk autofix-receipt path for ``finding_hash``."""
    return workdir.joinpath(*_AUTOFIX_SUBPATH, f"{_safe_finding_name(finding_hash)}.json")


def read_autofix_receipt(workdir: Path, finding_hash: str) -> AutofixReceipt | None:
    """Return the autofix receipt for ``finding_hash`` or ``None`` if absent."""
    path = autofix_receipt_path(workdir, finding_hash)
    if not path.is_file():
        return None
    try:
        return AutofixReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("review: malformed autofix receipt at %s", path)
        return None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - args fully constructed by caller
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def run_autofix_in_worktree(
    *,
    repo: Path,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    finding: Finding,
    apply_fix: Callable[[Path], None],
    run_gate: Callable[[Path], tuple[bool, str]],
    task_id: str,
    timestamp: int,
) -> AutofixReceipt:
    """Run a fix for ``finding`` in an isolated worktree and emit a receipt (AC3).

    A throwaway ``git worktree`` is checked out from ``repo`` (never the primary
    working tree). ``apply_fix`` mutates only that worktree; the fix is
    committed there; ``run_gate`` runs against it; then the worktree is torn
    down. The returned :class:`AutofixReceipt` links the finding to the fix
    commit and the gate result, signed and anchored in the autofix spine.

    The primary repo working tree is never mutated by the fix, so a
    gate-failing fix leaves nothing behind but its receipt.

    Args:
        repo: The primary git repository to branch a worktree from.
        workdir: Project root; the receipt lands under ``.sdd/reviews/autofix/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        private_key_pem: The install's Ed25519 private key (PEM).
        public_key_pem: The matching public key, embedded on the receipt.
        finding: The reviewer finding that spawned the fix.
        apply_fix: Callable mutating the isolated worktree in place.
        run_gate: Callable returning ``(passed, summary)`` for the worktree.
        task_id: The task the autofix is attributed to.
        timestamp: Integer timestamp for the receipt.

    Returns:
        The signed, anchored :class:`AutofixReceipt`.
    """
    finding_hash = finding.finding_hash()
    branch = f"bernstein/autofix/{_safe_finding_name(finding_hash)[:16]}"
    worktree_parent = Path(tempfile.mkdtemp(prefix="bernstein-autofix-"))
    worktree = worktree_parent / "wt"
    fix_commit = ""
    try:
        _git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), "HEAD")
        apply_fix(worktree)
        _git(worktree, "add", "-A")
        _git(
            worktree,
            "-c",
            "user.email=autofix@bernstein",
            "-c",
            "user.name=bernstein",
            "commit",
            "-q",
            "-m",
            f"fix: {finding.rule} {finding.path}:{finding.line}",
        )
        fix_commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        gate_passed, gate_summary = run_gate(worktree)
    finally:
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            _git(repo, "worktree", "remove", "--force", str(worktree))
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            _git(repo, "branch", "-D", branch)
        _remove_tree(worktree_parent)

    receipt = _sign_and_anchor_autofix(
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        finding_hash=finding_hash,
        fix_commit_hash=fix_commit,
        gate_passed=gate_passed,
        gate_summary=gate_summary,
        task_id=task_id,
        timestamp=timestamp,
    )
    return receipt


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            try:
                child.unlink()
            except OSError:
                logger.debug("autofix: could not remove %s", child)
    try:
        path.rmdir()
    except OSError:
        logger.debug("autofix: could not rmdir %s", path)


def _sign_and_anchor_autofix(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    finding_hash: str,
    fix_commit_hash: str,
    gate_passed: bool,
    gate_summary: str,
    task_id: str,
    timestamp: int,
) -> AutofixReceipt:
    unsigned = AutofixReceipt(
        finding_hash=finding_hash,
        fix_commit_hash=fix_commit_hash,
        gate_passed=gate_passed,
        gate_summary=gate_summary,
        task_id=task_id,
        timestamp=timestamp,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)
    spine = LineageSpine(lineage_root, run_id=AUTOFIX_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_AUTOFIX_SUBPATH, f"{_safe_finding_name(finding_hash)}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_REVIEW_ACTOR,
        step_id=finding_hash,
        model=_REVIEW_MODEL,
        timestamp=timestamp,
    )
    anchored = AutofixReceipt(
        finding_hash=finding_hash,
        fix_commit_hash=fix_commit_hash,
        gate_passed=gate_passed,
        gate_summary=gate_summary,
        task_id=task_id,
        timestamp=timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
    )
    path = autofix_receipt_path(workdir, finding_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


@dataclass(frozen=True)
class AutofixVerifyResult:
    """Outcome of :func:`verify_autofix_receipt`."""

    ok: bool
    reason: str
    receipt: AutofixReceipt | None = None


def verify_autofix_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    finding: Finding,
) -> AutofixVerifyResult:
    """Prove offline that ``finding``'s fix receipt is intact and anchored.

    Recomputes the finding hash, checks the Ed25519 signature against the
    embedded public key, and re-anchors the receipt against the autofix spine.
    """
    finding_hash = finding.finding_hash()
    receipt = read_autofix_receipt(workdir, finding_hash)
    if receipt is None:
        return AutofixVerifyResult(ok=False, reason="no autofix receipt found")
    if receipt.finding_hash != finding_hash:
        return AutofixVerifyResult(ok=False, reason="finding_hash mismatch", receipt=receipt)
    if not receipt.signature or not receipt.signer_public_key_pem:
        return AutofixVerifyResult(ok=False, reason="receipt is unsigned", receipt=receipt)
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return AutofixVerifyResult(ok=False, reason=f"signature does not verify ({outcome.reason})", receipt=receipt)
    spine = LineageSpine(lineage_root, run_id=AUTOFIX_RUN_ID, hmac_key=hmac_key)
    if not spine.verify().ok:
        return AutofixVerifyResult(ok=False, reason="autofix spine failed verification", receipt=receipt)
    recomputed = _recompute_anchor(spine, receipt.to_canonical_bytes())
    if recomputed != receipt.journal_entry_hash:
        return AutofixVerifyResult(ok=False, reason="anchor mismatch", receipt=receipt)
    return AutofixVerifyResult(ok=True, reason="", receipt=receipt)


# --------------------------------------------------------------------------
# ApprovalBinding -- signed, spine-anchored approval artefact
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalBinding:
    """The record binding a PR approval to its diff and journal provenance.

    Attributes:
        pr_url: The pull request that was approved.
        diff_hash: Content hash of the approved diff.
        journal_head: The run journal Merkle head of the reviewed head commit.
        timestamp: Integer timestamp for the binding.
        signer_public_key_pem: The install's Ed25519 public key; a verifier
            checks the signature against it offline.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The review-spine entry hash anchoring the binding.
    """

    pr_url: str
    diff_hash: str
    journal_head: str
    timestamp: int
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the signed + anchored binding (no signature / anchor)."""
        return {
            "v": REVIEW_SCHEMA_VERSION,
            "pr_url": self.pr_url,
            "diff_hash": self.diff_hash,
            "journal_head": self.journal_head,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> ApprovalBinding:
        row = json.loads(raw)
        return cls(
            pr_url=str(row["pr_url"]),
            diff_hash=str(row["diff_hash"]),
            journal_head=str(row["journal_head"]),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


def approval_path(workdir: Path, pr_url: str) -> Path:
    """Return the on-disk approval-binding path for ``pr_url``."""
    return workdir.joinpath(*_APPROVAL_SUBPATH, f"{_safe_pr_name(pr_url)}.json")


def read_approval_binding(workdir: Path, pr_url: str) -> ApprovalBinding | None:
    """Return the approval binding for ``pr_url`` or ``None`` if absent."""
    path = approval_path(workdir, pr_url)
    if not path.is_file():
        return None
    try:
        return ApprovalBinding.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("review: malformed approval binding at %s", path)
        return None


def emit_approval_binding(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    pr_url: str,
    diff: bytes,
    journal_head: str,
    timestamp: int,
) -> ApprovalBinding:
    """Bind a PR approval into a signed, anchored approval binding.

    The binding's canonical bytes are signed with the install's Ed25519 identity
    and are anchored in the review spine, so the returned binding's
    ``signature`` and ``journal_entry_hash`` are its chain-verifiable identity.

    Args:
        workdir: Project root; the binding lands under ``.sdd/reviews/approvals/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        private_key_pem: The install's Ed25519 private key (PEM).
        public_key_pem: The matching public key, embedded on the binding.
        pr_url: The pull request that was approved.
        diff: The approved PR diff bytes; hashed into ``diff_hash``.
        journal_head: The run journal Merkle head of the reviewed head commit.
        timestamp: Integer timestamp for the binding.

    Returns:
        The signed, anchored :class:`ApprovalBinding`.
    """
    diff_hash = compute_diff_hash(diff)
    unsigned = ApprovalBinding(
        pr_url=pr_url,
        diff_hash=diff_hash,
        journal_head=journal_head,
        timestamp=timestamp,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)

    spine = LineageSpine(lineage_root, run_id=REVIEW_RUN_ID, hmac_key=hmac_key)
    path = approval_path(workdir, pr_url)
    artifact_path = "/".join((*_APPROVAL_SUBPATH, path.name))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_REVIEW_ACTOR,
        step_id=diff_hash,
        model=_REVIEW_MODEL,
        timestamp=timestamp,
    )
    anchored = ApprovalBinding(
        pr_url=pr_url,
        diff_hash=diff_hash,
        journal_head=journal_head,
        timestamp=timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


@dataclass(frozen=True)
class ApprovalVerifyResult:
    """Outcome of :func:`verify_approval_binding`."""

    ok: bool
    reason: str
    binding: ApprovalBinding | None = None


def verify_approval_binding(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    pr_url: str,
    diff: bytes,
) -> ApprovalVerifyResult:
    """Prove offline that ``pr_url``'s approval binding is intact and anchored.

    Recomputes ``diff_hash`` from the presented diff, checks the Ed25519
    signature against the binding's embedded public key, and re-anchors the
    binding against the review spine.
    """
    binding = read_approval_binding(workdir, pr_url)
    if binding is None:
        return ApprovalVerifyResult(ok=False, reason="no approval binding found")

    if compute_diff_hash(diff) != binding.diff_hash:
        return ApprovalVerifyResult(
            ok=False,
            reason="diff_hash mismatch: presented diff differs from the approved diff",
            binding=binding,
        )

    if not binding.signature or not binding.signer_public_key_pem:
        return ApprovalVerifyResult(ok=False, reason="binding is unsigned", binding=binding)
    outcome = verify_payload(
        binding.to_canonical_bytes(),
        binding.signature,
        binding.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return ApprovalVerifyResult(
            ok=False,
            reason=f"signature does not verify ({outcome.reason})",
            binding=binding,
        )

    spine = LineageSpine(lineage_root, run_id=REVIEW_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return ApprovalVerifyResult(
            ok=False,
            reason=f"review spine failed verification ({spine_result.status.value})",
            binding=binding,
        )
    recomputed = _recompute_anchor(spine, binding.to_canonical_bytes())
    if recomputed is None:
        return ApprovalVerifyResult(ok=False, reason="binding is not anchored in the review spine", binding=binding)
    if recomputed != binding.journal_entry_hash:
        return ApprovalVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the binding bytes",
            binding=binding,
        )

    return ApprovalVerifyResult(ok=True, reason="", binding=binding)


__all__ = [
    "AUTOFIX_RUN_ID",
    "REVIEW_RUN_ID",
    "REVIEW_SCHEMA_VERSION",
    "ApprovalBinding",
    "ApprovalVerifyResult",
    "AutofixReceipt",
    "AutofixVerifyResult",
    "Finding",
    "ReviewChainVerifyResult",
    "ReviewReceipt",
    "ReviewVerifyResult",
    "approval_path",
    "autofix_receipt_path",
    "compute_diff_hash",
    "compute_issue_hash",
    "compute_plan_hash",
    "emit_approval_binding",
    "emit_review_receipt",
    "load_or_create_review_identity",
    "read_approval_binding",
    "read_autofix_receipt",
    "read_review_chain",
    "read_review_receipt",
    "receipt_path",
    "run_autofix_in_worktree",
    "verify_approval_binding",
    "verify_autofix_receipt",
    "verify_review_chain",
    "verify_review_receipt",
]
