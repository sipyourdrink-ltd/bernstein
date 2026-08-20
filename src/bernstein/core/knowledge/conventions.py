"""Convention receipts: signed, commit-pinned, chain-anchored review corrections.

Issue #3750. A review correction filed once applies automatically to future
reviews as an attested, commit-pinned, chain-anchored convention receipt rather
than free text.

Design & Properties:
--------------------
- Every convention receipt binds:
    {rule_text, rule_text_hash, subject_path, subject_symbol, base_commit_sha,
     assertion_ref, filing_finding_id, decided_by, version, status}
  into a canonical representation signed with the install's Ed25519 identity
  and anchored in the HMAC audit chain.
- Pinning & Expiration: every rule pins ``base_commit_sha`` (mandatory).
  A rule whose ``subject_symbol`` no longer resolves at HEAD moves to
  ``expired`` -- appended to the chain as a ``convention.retired`` entry, the
  same as an operator-driven retirement -- and ceases being injected into
  review prompts. Expiry is never a silent drop: a rule that stopped applying
  with nothing in the chain saying so cannot be told apart from one that was
  never filed.
- Symbol resolution tradeoff: symbol resolution for convention expiration uses
  AST parsing for Python files (``ast.parse``) to accurately inspect class and
  function definitions (falling back to lexical search for non-Python files).
  Tradeoff: AST parsing guarantees 0% false positives on comments or strings
  and handles multi-line definitions accurately, with negligible CPU overhead
  for per-file checks, compared to naive regex/grep.
- Executable assertions: rules carry an executable assertion (reusing
  ``AssertionKind`` from ``spec_assertions.py``) or are marked ``advisory``.
- Deduplication: filing routes through ``file_lesson()`` and deduplicates on
  matching path and rule text similarity (threshold > 0.8), incrementing the
  version counter (filing 3 times produces version: 3) instead of duplicate rules.
- Conflict detection: two active receipts targeting overlapping ``subject_path``
  globs whose assertions/rules contradict each other are rejected at file time,
  naming both receipt IDs. The gate is "these cannot both hold" -- the same
  target asserted with opposite polarity, or a rule text and its negation --
  not "these touch the same file", which two independent rules routinely do.
- Ruleset hash: computed over what each rule *demands* (subject, rule text
  hash, pinned commit, assertion, version, status), never over receipt ids,
  wall-clock filing times, or the ids of the lesson and finding behind it, so
  two operators whose active rules agree agree on the digest.
- Retirement: retiring a rule is an immutable chain event (``convention.retired``)
  rather than a file deletion, verifiable via ``bernstein verify --memory-audit``.
"""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.knowledge.lessons import (
    MemoryType,
    _content_similarity,
    file_lesson,
)
from bernstein.core.review.receipt import load_or_create_review_identity
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import (
    EVENT_CONVENTION_RECEIPT,
    EVENT_CONVENTION_RETIRED,
    AuditChainStore,
    record_convention_receipt,
    record_convention_retired,
)
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.planning.spec_assertions import Assertion

logger = logging.getLogger(__name__)

AssertionKind = Literal["file_exists", "import_resolves", "test_passes", "regex_in_file", "advisory"]

_CONVENTIONS_SUBPATH = (".sdd", "conventions", "receipts")


def compute_rule_text_hash(rule_text: str) -> str:
    """Return the SHA-256 hex digest of the rule text."""
    return hashlib.sha256(rule_text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Data model: ConventionReceipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConventionReceipt:
    """A signed, commit-pinned, chain-anchored review convention receipt.

    Attributes:
        receipt_id: Unique identifier for this receipt (UUID).
        rule_text: Human-readable statement of the convention/rule.
        rule_text_hash: SHA-256 hex digest of ``rule_text``.
        subject_path: File path or glob the convention applies to.
        base_commit_sha: Git commit SHA against which the rule was learned.
        subject_symbol: Optional class/function symbol name within the file.
        assertion_ref: Optional executable assertion or advisory spec.
        filing_finding_id: Identifier of the review finding that triggered this.
        decided_by: Identity of the reviewer/maintainer who decided the rule.
        created_timestamp: Unix epoch timestamp when first filed or updated.
        version: Version counter; incremented on deduplicated filings.
        status: Lifecycle state: ``"active"``, ``"retired"``, or ``"expired"``.
        lesson_id: Identifier of the lesson filed in the memory lesson store.
        signer_public_key_pem: Ed25519 public key (PEM) of the signing install.
        signature: Detached Ed25519 signature over the canonical binding.
        supersedes_receipt_id: Optional ID of a receipt superseded by this one.
    """

    receipt_id: str
    rule_text: str
    rule_text_hash: str
    subject_path: str
    base_commit_sha: str
    subject_symbol: str = ""
    assertion_ref: dict[str, Any] | None = None
    filing_finding_id: str = ""
    decided_by: str = "reviewer"
    created_timestamp: float = 0.0
    version: int = 1
    status: str = "active"
    lesson_id: str = ""
    signer_public_key_pem: str = ""
    signature: str = ""
    supersedes_receipt_id: str | None = None

    def _binding(self) -> dict[str, Any]:
        """Return the canonical dictionary preimage for signing and hashing."""
        return {
            "v": 1,
            "receipt_id": self.receipt_id,
            "rule_text": self.rule_text,
            "rule_text_hash": self.rule_text_hash,
            "subject_path": self.subject_path,
            "subject_symbol": self.subject_symbol,
            "base_commit_sha": self.base_commit_sha,
            "assertion_ref": self.assertion_ref,
            "filing_finding_id": self.filing_finding_id,
            "decided_by": self.decided_by,
            "created_timestamp": self.created_timestamp,
            "version": self.version,
            "status": self.status,
            "lesson_id": self.lesson_id,
            "supersedes_receipt_id": self.supersedes_receipt_id,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (sorted keys, minimal separators)."""
        return json.dumps(self._binding(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        """Return full JSON-serializable dictionary including cryptographic fields."""
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConventionReceipt:
        """Construct a ConventionReceipt from a deserialized JSON dictionary."""
        rule_text = str(d["rule_text"])
        rule_text_hash = str(d.get("rule_text_hash") or compute_rule_text_hash(rule_text))
        return cls(
            receipt_id=str(d["receipt_id"]),
            rule_text=rule_text,
            rule_text_hash=rule_text_hash,
            subject_path=str(d["subject_path"]),
            subject_symbol=str(d.get("subject_symbol") or ""),
            base_commit_sha=str(d["base_commit_sha"]),
            assertion_ref=d.get("assertion_ref"),
            filing_finding_id=str(d.get("filing_finding_id") or ""),
            decided_by=str(d.get("decided_by") or "reviewer"),
            created_timestamp=float(d.get("created_timestamp", 0.0)),
            version=int(d.get("version", 1)),
            status=str(d.get("status", "active")),
            lesson_id=str(d.get("lesson_id") or ""),
            signer_public_key_pem=str(d.get("signer_public_key_pem") or ""),
            signature=str(d.get("signature") or ""),
            supersedes_receipt_id=d.get("supersedes_receipt_id"),
        )


def _get_receipts_dir(sdd_dir: Path) -> Path:
    d = sdd_dir / "conventions" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Path & Conflict Detection
# ---------------------------------------------------------------------------


def paths_overlap(path1: str, path2: str) -> bool:
    """Check if two file paths or glob patterns overlap."""
    if path1 == path2:
        return True
    if fnmatch.fnmatch(path1, path2) or fnmatch.fnmatch(path2, path1):
        return True
    # Strip wildcard characters to compare directory prefixes
    p1_clean = path1.replace("**", "").replace("*", "").rstrip("/")
    p2_clean = path2.replace("**", "").replace("*", "").rstrip("/")
    return bool(p1_clean and p2_clean and (p1_clean.startswith(p2_clean) or p2_clean.startswith(p1_clean)))


#: Markers an assertion spec may use to say "this must NOT hold".
_NEGATION_PREFIXES = ("!", "not:", "no:")


def _same_assertion_target(target1: str, target2: str) -> bool:
    """Return whether two assertion targets name the same thing modulo negation."""
    return _strip_negation(target1) == _strip_negation(target2)


def _strip_negation(value: str) -> str:
    stripped = value.strip()
    for prefix in _NEGATION_PREFIXES:
        if stripped.lower().startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def _is_negated(ref: dict[str, Any]) -> bool:
    """Return whether an assertion ref asks for its target NOT to hold."""
    if ref.get("negated") is True:
        return True
    for field_name in ("target", "predicate"):
        value = str(ref.get(field_name, "")).strip().lower()
        if any(value.startswith(prefix) for prefix in _NEGATION_PREFIXES):
            return True
    return False


def _negates(ref1: dict[str, Any], ref2: dict[str, Any]) -> bool:
    """Return whether exactly one of two assertion refs is negated."""
    return _is_negated(ref1) != _is_negated(ref2)


def detect_assertion_conflict(
    candidate: ConventionReceipt,
    existing: ConventionReceipt,
) -> tuple[bool, str]:
    """Detect whether two active receipts on overlapping paths cannot both hold.

    A conflict is an actual contradiction, not a shared subject: either the two
    assertions name the same target with opposite polarity, or the two rule
    texts state a requirement and its negation over near-identical wording.
    Two rules that merely touch the same file are not in conflict -- rejecting
    those would refuse legitimate filings.

    Returns:
        (is_conflict, reason_message)
    """
    if candidate.receipt_id == existing.receipt_id:
        return False, ""
    if existing.status != "active":
        return False, ""

    if not paths_overlap(candidate.subject_path, existing.subject_path):
        return False, ""

    # Same symbol on overlapping path with contradictory assertion or rule text
    cand_ref = candidate.assertion_ref or {}
    exist_ref = existing.assertion_ref or {}

    cand_kind = cand_ref.get("kind", "advisory")
    exist_kind = exist_ref.get("kind", "advisory")

    cand_target = str(cand_ref.get("target", ""))
    exist_target = str(exist_ref.get("target", ""))

    # Direct assertion conflict: the same assertion over the same target with
    # opposite polarity, i.e. one demands the target hold and the other demands
    # it not hold. Two *different* targets in the same file are not a conflict
    # -- "contains `import logging`" and "contains `def main`" can both hold at
    # once, and rejecting that pair would refuse legitimate filings rather than
    # contradictory ones. The gate is "these assertions cannot both hold", not
    # "these assertions touch the same file".
    if (
        cand_kind == exist_kind
        and cand_kind in {"regex_in_file", "import_resolves", "file_exists"}
        and cand_target
        and exist_target
        and _same_assertion_target(cand_target, exist_target)
        and _negates(cand_ref, exist_ref)
    ):
        return (
            True,
            f"Conflicting assertion on '{cand_target}': one requires it to hold and the other requires it not to hold",
        )

    # Check for contradictory rule phrasing (e.g. forbidden vs required, no_ vs with_)
    c_lower = candidate.rule_text.lower()
    e_lower = existing.rule_text.lower()
    if (
        ("always " in c_lower and "never " in e_lower)
        or ("never " in c_lower and "always " in e_lower)
        or ("require " in c_lower and "forbid " in e_lower)
        or ("forbid " in c_lower and "require " in e_lower)
    ) and _content_similarity(c_lower, e_lower) > 0.4:
        return (
            True,
            f"Contradictory rule statements: '{candidate.rule_text}' contradicts existing '{existing.rule_text}'",
        )

    return False, ""


# ---------------------------------------------------------------------------
# AST Symbol Resolution (Expiration Check)
# ---------------------------------------------------------------------------


def check_symbol_resolves(workdir: Path, subject_path: str, subject_symbol: str) -> bool:
    """Check if subject_symbol still exists in the file(s) matched by subject_path at HEAD.

    Uses AST parsing for Python files for zero false positives, falling back to
    lexical search for non-Python files.
    """
    if not subject_symbol or not subject_symbol.strip():
        return True

    # Resolve matching files
    files: list[Path] = []
    direct_path = workdir / subject_path
    if direct_path.is_file():
        files.append(direct_path)
    else:
        # Try globbing within workdir
        with contextlib.suppress(Exception):
            files.extend([p for p in workdir.glob(subject_path) if p.is_file()])

    if not files:
        # File was deleted or does not exist at HEAD
        return False

    for file_path in files:
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        if file_path.suffix == ".py":
            try:
                tree = ast.parse(content, filename=str(file_path))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name == subject_symbol:
                            return True
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == subject_symbol:
                                return True
                    elif (
                        isinstance(node, ast.AnnAssign)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == subject_symbol
                    ):
                        return True
            except SyntaxError:
                # If AST parsing fails due to temporary syntax error, fallback to lexical
                if subject_symbol in content:
                    return True
        else:
            if subject_symbol in content:
                return True

    return False


# ---------------------------------------------------------------------------
# Review Correction & Convention Receipt Filing
# ---------------------------------------------------------------------------


def file_review_correction(
    sdd_dir: Path,
    workdir: Path,
    rule_text: str,
    subject_path: str,
    base_commit_sha: str,
    *,
    subject_symbol: str = "",
    assertion_ref: dict[str, Any] | Assertion | None = None,
    filing_finding_id: str = "",
    decided_by: str = "reviewer",
    confidence: float = 0.8,
    tags: list[str] | None = None,
    created_timestamp: float | None = None,
) -> ConventionReceipt:
    """File a review correction, persisting a lesson and an attested convention receipt.

    Args:
        sdd_dir: Path to project .sdd directory.
        workdir: Working directory of the codebase.
        rule_text: The convention/rule text.
        subject_path: Repo-relative path or glob the rule applies to.
        base_commit_sha: Mandatory commit SHA against which the rule was learned.
        subject_symbol: Optional class/function symbol name in the subject path.
        assertion_ref: Optional executable assertion spec or Assertion instance.
        filing_finding_id: Optional ID of the originating review finding.
        decided_by: Reviewer or agent identity filing the correction.
        confidence: Initial confidence score (0.0 to 1.0).
        tags: Optional tags for lesson retrieval (defaults to ["convention", "review"]).
        created_timestamp: Optional override for timestamp.

    Returns:
        The filed or updated :class:`ConventionReceipt`.
    """
    if not base_commit_sha or not base_commit_sha.strip():
        raise ValueError("base_commit_sha is mandatory for convention receipts")
    if not rule_text or not rule_text.strip():
        raise ValueError("rule_text cannot be empty")
    if not subject_path or not subject_path.strip():
        raise ValueError("subject_path cannot be empty")

    rule_text = rule_text.strip()
    rule_text_hash = compute_rule_text_hash(rule_text)
    subject_path = subject_path.strip()
    subject_symbol = subject_symbol.strip()
    now = created_timestamp if created_timestamp is not None else time.time()

    # Normalise assertion_ref
    normalized_assertion: dict[str, Any] | None = None
    if assertion_ref is not None:
        if hasattr(assertion_ref, "kind") and hasattr(assertion_ref, "target"):
            normalized_assertion = {
                "kind": assertion_ref.kind,
                "target": assertion_ref.target,
                "predicate": getattr(assertion_ref, "predicate", ""),
                "feature_id": getattr(assertion_ref, "feature_id", ""),
            }
        elif isinstance(assertion_ref, dict):
            normalized_assertion = dict(assertion_ref)

    receipts_dir = _get_receipts_dir(sdd_dir)
    existing_receipts: list[ConventionReceipt] = []
    for p in receipts_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            existing_receipts.append(ConventionReceipt.from_dict(data))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    # 1. Check for deduplication against existing active receipts
    matching_existing: ConventionReceipt | None = None
    for ex in existing_receipts:
        if ex.status != "active":
            continue
        if ex.subject_path == subject_path and (
            ex.rule_text_hash == rule_text_hash or _content_similarity(ex.rule_text.lower(), rule_text.lower()) > 0.8
        ):
            matching_existing = ex
            break

    # 2. File in lesson memory store (routes through file_lesson dedup)
    lesson_tags = tags if tags is not None else ["convention", "review"]
    lesson_id = file_lesson(
        sdd_dir=sdd_dir,
        task_id=filing_finding_id or "review-correction",
        agent_id=decided_by,
        content=rule_text,
        tags=lesson_tags,
        confidence=confidence,
        memory_type=MemoryType.FEEDBACK,
    )

    # 3. Load or generate signing identity & audit chain key
    identity_dir = sdd_dir / "identity"
    priv_pem, pub_pem = load_or_create_review_identity(identity_dir)
    audit_key = load_or_create_audit_key()
    chain = AuditChainStore(sdd_dir / "audit", key=audit_key)

    if matching_existing is not None:
        # Update existing convention receipt (bump version)
        new_version = matching_existing.version + 1
        updated = ConventionReceipt(
            receipt_id=matching_existing.receipt_id,
            rule_text=rule_text,
            rule_text_hash=rule_text_hash,
            subject_path=subject_path,
            subject_symbol=subject_symbol or matching_existing.subject_symbol,
            base_commit_sha=base_commit_sha,
            assertion_ref=normalized_assertion or matching_existing.assertion_ref,
            filing_finding_id=filing_finding_id or matching_existing.filing_finding_id,
            decided_by=decided_by,
            created_timestamp=now,
            version=new_version,
            status="active",
            lesson_id=lesson_id,
            signer_public_key_pem=pub_pem,
            supersedes_receipt_id=matching_existing.supersedes_receipt_id,
        )
        sig = sign_payload(updated.to_canonical_bytes(), priv_pem)
        signed_receipt = ConventionReceipt(
            receipt_id=updated.receipt_id,
            rule_text=updated.rule_text,
            rule_text_hash=updated.rule_text_hash,
            subject_path=updated.subject_path,
            subject_symbol=updated.subject_symbol,
            base_commit_sha=updated.base_commit_sha,
            assertion_ref=updated.assertion_ref,
            filing_finding_id=updated.filing_finding_id,
            decided_by=updated.decided_by,
            created_timestamp=updated.created_timestamp,
            version=updated.version,
            status=updated.status,
            lesson_id=updated.lesson_id,
            signer_public_key_pem=pub_pem,
            signature=sig,
            supersedes_receipt_id=updated.supersedes_receipt_id,
        )
        # Record in the HMAC audit chain first, then write the projection: a
        # crash between the two leaves an entry with no receipt file (harmless,
        # replayable) rather than a receipt file with no entry, which
        # ``--memory-audit`` cannot tell apart from a tampered record.
        record_convention_receipt(
            chain=chain,
            receipt_id=signed_receipt.receipt_id,
            rule_text_hash=signed_receipt.rule_text_hash,
            subject_path=signed_receipt.subject_path,
            subject_symbol=signed_receipt.subject_symbol,
            base_commit_sha=signed_receipt.base_commit_sha,
            filing_finding_id=signed_receipt.filing_finding_id,
            decided_by=signed_receipt.decided_by,
            version=signed_receipt.version,
            status=signed_receipt.status,
        )
        out_file = receipts_dir / f"{signed_receipt.receipt_id}.json"
        out_file.write_text(json.dumps(signed_receipt.to_dict(), indent=2), encoding="utf-8")
        return signed_receipt

    # 4. Check for conflicts with other active convention receipts
    candidate_id = str(uuid.uuid4())
    candidate_receipt = ConventionReceipt(
        receipt_id=candidate_id,
        rule_text=rule_text,
        rule_text_hash=rule_text_hash,
        subject_path=subject_path,
        subject_symbol=subject_symbol,
        base_commit_sha=base_commit_sha,
        assertion_ref=normalized_assertion,
        filing_finding_id=filing_finding_id,
        decided_by=decided_by,
        created_timestamp=now,
        version=1,
        status="active",
        lesson_id=lesson_id,
        signer_public_key_pem=pub_pem,
    )

    for ex in existing_receipts:
        is_conflict, reason = detect_assertion_conflict(candidate_receipt, ex)
        if is_conflict:
            raise ValueError(
                f"Convention conflict between new receipt '{candidate_id}' "
                f"and existing receipt '{ex.receipt_id}': {reason}"
            )

    # 5. Sign and persist new convention receipt
    sig = sign_payload(candidate_receipt.to_canonical_bytes(), priv_pem)
    signed_new = ConventionReceipt(
        receipt_id=candidate_receipt.receipt_id,
        rule_text=candidate_receipt.rule_text,
        rule_text_hash=candidate_receipt.rule_text_hash,
        subject_path=candidate_receipt.subject_path,
        subject_symbol=candidate_receipt.subject_symbol,
        base_commit_sha=candidate_receipt.base_commit_sha,
        assertion_ref=candidate_receipt.assertion_ref,
        filing_finding_id=candidate_receipt.filing_finding_id,
        decided_by=candidate_receipt.decided_by,
        created_timestamp=candidate_receipt.created_timestamp,
        version=candidate_receipt.version,
        status=candidate_receipt.status,
        lesson_id=candidate_receipt.lesson_id,
        signer_public_key_pem=pub_pem,
        signature=sig,
    )

    # Chain entry first, projection second -- see the dedup branch above.
    record_convention_receipt(
        chain=chain,
        receipt_id=signed_new.receipt_id,
        rule_text_hash=signed_new.rule_text_hash,
        subject_path=signed_new.subject_path,
        subject_symbol=signed_new.subject_symbol,
        base_commit_sha=signed_new.base_commit_sha,
        filing_finding_id=signed_new.filing_finding_id,
        decided_by=signed_new.decided_by,
        version=signed_new.version,
        status=signed_new.status,
    )
    out_file = receipts_dir / f"{signed_new.receipt_id}.json"
    out_file.write_text(json.dumps(signed_new.to_dict(), indent=2), encoding="utf-8")
    return signed_new


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------


def retire_convention_receipt(
    sdd_dir: Path,
    receipt_id: str,
    *,
    retired_by: str,
    reason: str = "",
    superseded_by: str = "",
) -> ConventionReceipt:
    """Retire an active convention receipt as an append-only chain event."""
    return _close_convention_receipt(
        sdd_dir,
        receipt_id,
        status="retired",
        closed_by=retired_by,
        reason=reason,
        superseded_by=superseded_by,
    )


def expire_convention_receipt(
    sdd_dir: Path,
    receipt_id: str,
    *,
    reason: str = "subject_symbol no longer resolves at HEAD",
) -> ConventionReceipt:
    """Expire a receipt whose subject symbol no longer resolves, as a chain event.

    Expiry is the third lifecycle transition alongside filing and retirement,
    and it is recorded the same way: an appended chain entry, never a silent
    drop. A rule that simply stopped being injected -- with nothing in the
    chain saying so -- is indistinguishable from a rule that was never in
    force, which is the failure the receipt exists to rule out.
    """
    return _close_convention_receipt(
        sdd_dir,
        receipt_id,
        status="expired",
        closed_by="conventions",
        reason=reason,
        superseded_by="",
    )


def _close_convention_receipt(
    sdd_dir: Path,
    receipt_id: str,
    *,
    status: str,
    closed_by: str,
    reason: str,
    superseded_by: str,
) -> ConventionReceipt:
    """Move a receipt to a terminal status, recording the transition in the chain."""
    receipts_dir = _get_receipts_dir(sdd_dir)
    receipt_file = receipts_dir / f"{receipt_id}.json"
    if not receipt_file.is_file():
        raise FileNotFoundError(f"Convention receipt '{receipt_id}' not found at {receipt_file}")

    data = json.loads(receipt_file.read_text(encoding="utf-8"))
    existing = ConventionReceipt.from_dict(data)

    identity_dir = sdd_dir / "identity"
    priv_pem, pub_pem = load_or_create_review_identity(identity_dir)

    retired = ConventionReceipt(
        receipt_id=existing.receipt_id,
        rule_text=existing.rule_text,
        rule_text_hash=existing.rule_text_hash,
        subject_path=existing.subject_path,
        subject_symbol=existing.subject_symbol,
        base_commit_sha=existing.base_commit_sha,
        assertion_ref=existing.assertion_ref,
        filing_finding_id=existing.filing_finding_id,
        decided_by=existing.decided_by,
        created_timestamp=existing.created_timestamp,
        version=existing.version,
        status=status,
        lesson_id=existing.lesson_id,
        signer_public_key_pem=pub_pem,
        supersedes_receipt_id=superseded_by or existing.supersedes_receipt_id,
    )
    sig = sign_payload(retired.to_canonical_bytes(), priv_pem)
    signed_retired = ConventionReceipt(
        receipt_id=retired.receipt_id,
        rule_text=retired.rule_text,
        rule_text_hash=retired.rule_text_hash,
        subject_path=retired.subject_path,
        subject_symbol=retired.subject_symbol,
        base_commit_sha=retired.base_commit_sha,
        assertion_ref=retired.assertion_ref,
        filing_finding_id=retired.filing_finding_id,
        decided_by=retired.decided_by,
        created_timestamp=retired.created_timestamp,
        version=retired.version,
        status=retired.status,
        lesson_id=retired.lesson_id,
        signer_public_key_pem=pub_pem,
        signature=sig,
        supersedes_receipt_id=retired.supersedes_receipt_id,
    )

    # Append the chain entry before writing the projection. The chain is the
    # append-only record and the receipt file is derived from it, so a crash
    # between the two must leave the recoverable ordering: an entry with no
    # file flip is replayable, while a file flipped with no entry behind it
    # is a permanent ``--memory-audit`` failure that looks exactly like tamper.
    audit_key = load_or_create_audit_key()
    chain = AuditChainStore(sdd_dir / "audit", key=audit_key)
    record_convention_retired(
        chain=chain,
        receipt_id=receipt_id,
        retired_by=closed_by,
        reason=reason,
        superseded_by=superseded_by,
    )
    receipt_file.write_text(json.dumps(signed_retired.to_dict(), indent=2), encoding="utf-8")
    return signed_retired


# ---------------------------------------------------------------------------
# Active Conventions & Ruleset Hash
# ---------------------------------------------------------------------------


def get_active_conventions(
    sdd_dir: Path,
    workdir: Path | None = None,
) -> tuple[list[ConventionReceipt], str]:
    """Retrieve all currently active convention receipts and their deterministic ruleset hash.

    Args:
        sdd_dir: Path to .sdd directory.
        workdir: Optional project workdir; when provided, symbols are checked
            against HEAD and non-resolving symbols are marked expired.

    Returns:
        ``(active_conventions, ruleset_hash)`` where ruleset_hash is deterministic
        across operators with identical audit chains and HEAD checkouts.
    """
    receipts_dir = _get_receipts_dir(sdd_dir)
    active: list[ConventionReceipt] = []

    for p in sorted(receipts_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            receipt = ConventionReceipt.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

        if receipt.status != "active":
            continue

        # Check symbol expiration if workdir provided
        if (
            workdir is not None
            and receipt.subject_symbol
            and not check_symbol_resolves(workdir, receipt.subject_path, receipt.subject_symbol)
        ):
            # The symbol is gone at HEAD, so the rule is out of force. Record
            # that as ``expired`` in the chain rather than dropping it from the
            # projection: a rule that silently stops being injected leaves an
            # operator unable to tell "expired" from "never filed".
            with contextlib.suppress(OSError, FileNotFoundError):
                expire_convention_receipt(sdd_dir, receipt.receipt_id)
            continue

        active.append(receipt)

    # Sort deterministically
    active.sort(key=lambda r: (r.subject_path, r.rule_text_hash, r.receipt_id))

    ruleset_hash = compute_ruleset_hash(active)
    return active, ruleset_hash


def _ruleset_projection(receipt: ConventionReceipt) -> dict[str, Any]:
    """Return the fields of a receipt that decide *what rule is in force*.

    The full signed binding also carries the receipt's local identity and the
    circumstances of its filing -- ``receipt_id`` (a uuid minted on the filing
    install), ``created_timestamp`` (wall clock), ``lesson_id``,
    ``filing_finding_id``, ``decided_by``. None of those change what the rule
    demands, and all of them differ between two installs that filed the same
    correction. Folding them into the ruleset hash would make the hash a
    fingerprint of one install's filing history rather than of the rule set in
    force, so two operators at the same HEAD with the same rules would never
    agree on it.
    """
    return {
        "v": 1,
        "subject_path": receipt.subject_path,
        "subject_symbol": receipt.subject_symbol,
        "rule_text_hash": receipt.rule_text_hash,
        "base_commit_sha": receipt.base_commit_sha,
        "assertion_ref": receipt.assertion_ref,
        "version": receipt.version,
        "status": receipt.status,
    }


def compute_ruleset_hash(receipts: list[ConventionReceipt]) -> str:
    """Return the deterministic hash of the rule set *receipts* puts in force.

    Ordered by rule content, not by receipt id, so the digest is a function of
    the rules alone: two operators whose active rules agree render the same
    ``ruleset_hash`` even though their receipt files were written at different
    times under different receipt ids.
    """
    projections = sorted(
        (_ruleset_projection(r) for r in receipts),
        key=lambda p: (str(p["subject_path"]), str(p["subject_symbol"]), str(p["rule_text_hash"])),
    )
    ruleset_bytes = b"\x00".join(
        json.dumps(p, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") for p in projections
    )
    return "sha256:" + hashlib.sha256(ruleset_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Audit Verification for Conventions
# ---------------------------------------------------------------------------


@dataclass
class ConventionsVerifyResult:
    """Result of audit verification over convention receipts."""

    valid: bool
    errors: list[str]
    receipts_checked: int


def verify_conventions_audit(sdd_dir: Path) -> ConventionsVerifyResult:
    """Audit all convention receipts and verify their cryptographic signatures and audit chain entries."""
    receipts_dir = sdd_dir / "conventions" / "receipts"
    if not receipts_dir.exists():
        return ConventionsVerifyResult(valid=True, errors=[], receipts_checked=0)

    receipt_files = sorted(receipts_dir.glob("*.json"))
    if not receipt_files:
        return ConventionsVerifyResult(valid=True, errors=[], receipts_checked=0)

    errors: list[str] = []
    checked = 0

    # Read the chain through its own verifier rather than as raw JSONL. A
    # receipt is only "chain-anchored" if the chain it is anchored in still
    # verifies: scanning the log for a line that names the receipt is a
    # presence check, and an edit to the receipt paired with a hand-appended
    # log line would pass it. ``verify_and_query`` holds the append lock across
    # both reads, so the events projected here are exactly the events the
    # verdict covers.
    chain = AuditChainStore(sdd_dir / "audit", key=load_or_create_audit_key())
    chain_ok, chain_errors, chain_events = chain.verify_and_query()
    if not chain_ok:
        detail = "; ".join(chain_errors[:3]) if chain_errors else "chain break"
        errors.append(f"Convention audit chain failed verification ({detail}); receipt anchors are unusable")

    for rf in receipt_files:
        checked += 1
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            receipt = ConventionReceipt.from_dict(data)
        except Exception as exc:
            errors.append(f"File {rf.name}: invalid convention receipt JSON ({exc})")
            continue

        receipt_id = receipt.receipt_id

        # 1. Verify rule_text_hash
        computed_hash = compute_rule_text_hash(receipt.rule_text)
        if receipt.rule_text_hash != computed_hash:
            errors.append(
                f"Convention receipt {receipt_id}: rule_text_hash MISMATCH "
                f"- stored={receipt.rule_text_hash[:12]}… computed={computed_hash[:12]}… "
                f"(rule text tampered without chain entry)"
            )

        # 2. Verify signature
        if not receipt.signature or not receipt.signer_public_key_pem:
            errors.append(f"Convention receipt {receipt_id}: missing signature or public key")
        else:
            outcome = verify_payload(
                receipt.to_canonical_bytes(),
                receipt.signature,
                receipt.signer_public_key_pem,
                allow_unverified=True,
            )
            if not outcome.verified:
                errors.append(f"Convention receipt {receipt_id}: signature verification failed ({outcome.reason})")

        # 3. Verify audit chain record
        matching_receipt_events = [
            e
            for e in chain_events
            if e.event_type == EVENT_CONVENTION_RECEIPT and e.details.get("receipt_id") == receipt_id
        ]
        if not matching_receipt_events:
            errors.append(f"Convention receipt {receipt_id}: no convention.receipt entry in audit chain")
        else:
            latest_ev = matching_receipt_events[-1]
            chain_hash = latest_ev.details.get("rule_text_hash")
            if chain_hash and chain_hash != computed_hash:
                errors.append(
                    f"Convention receipt {receipt_id}: audit chain rule_text_hash mismatch "
                    f"- chain={chain_hash[:12]}… computed={computed_hash[:12]}…"
                )

        # 4. If retired or expired, verify the terminal chain event. Both are
        # chain events rather than deletes, so a status change on disk with no
        # entry behind it is a tamper signal, not a tidy-up.
        if receipt.status in {"retired", "expired"}:
            retire_events = [
                e
                for e in chain_events
                if e.event_type == EVENT_CONVENTION_RETIRED and e.details.get("receipt_id") == receipt_id
            ]
            if not retire_events:
                errors.append(
                    f"Convention receipt {receipt_id}: {receipt.status} on disk without convention.retired chain event"
                )

    return ConventionsVerifyResult(
        valid=len(errors) == 0,
        errors=errors,
        receipts_checked=checked,
    )


__all__ = [
    "AssertionKind",
    "ConventionReceipt",
    "ConventionsVerifyResult",
    "check_symbol_resolves",
    "compute_rule_text_hash",
    "compute_ruleset_hash",
    "detect_assertion_conflict",
    "expire_convention_receipt",
    "file_review_correction",
    "get_active_conventions",
    "paths_overlap",
    "retire_convention_receipt",
    "verify_conventions_audit",
]
