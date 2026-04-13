"""Memory provenance and integrity for agent lessons.

Countermeasures for OWASP ASI06 2026 (Agentic Memory Poisoning):

- Per-entry SHA-256 *content hash*: protects the immutable fields of each
  lesson against tampering after it has been filed.
- *Hash chain* (prev_hash → chain_hash): links every JSONL entry to its
  predecessor, so insertion, deletion, or reordering of entries breaks the
  chain and is immediately detectable.
- *Prompt-injection pattern detection*: rejects adversarial lesson content
  before it enters the memory store, preventing poisoned lessons from being
  injected into future agent prompts.

Integrity fields written to every lesson entry
-----------------------------------------------
``content_hash``
    SHA-256 of the *immutable* lesson fields: lesson_id, tags (sorted),
    content, created_timestamp, filed_by_agent, task_id.
    Confidence and version are intentionally excluded — legitimate updates
    must not invalidate the hash.

``prev_hash``
    The ``chain_hash`` of the entry that was written immediately before this
    one in the JSONL file.  The very first entry uses ``GENESIS_HASH``.

``chain_hash``
    SHA-256 of ``"chain:" + content_hash + ":" + prev_hash``.
    Tying position (prev_hash) to content (content_hash) means that even a
    byte-perfect copy of an entry inserted at a different position breaks the
    chain.

Usage
-----
::

    from bernstein.core.knowledge.memory_integrity import (
        build_entry_integrity,
        detect_memory_poisoning,
        verify_chain,
        audit_provenance,
    )

    # Before filing a new lesson
    poison = detect_memory_poisoning(content, tags)
    if poison.is_suspicious:
        raise ValueError(poison.reason)

    integrity = build_entry_integrity(lesson_dict, prev_chain_hash)
    lesson_dict.update(integrity.as_dict())

    # Periodic chain verification
    result = verify_chain(lessons_path)
    if not result.valid:
        alert(result.errors)
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sentinel used as prev_hash for the very first entry in a lessons file.
GENESIS_HASH: str = "genesis"

# Minimum weighted score required to trigger rejection.
# Score 1 alone (e.g. confidence pinned at 1.0) is a low-severity hint that
# should NOT block filing.  A single structural injection marker scores ≥ 2
# and will reject.  Two weak signals (e.g. external URL + pinned confidence)
# also reach the threshold.
_POISON_THRESHOLD: int = 2


# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------


def _sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def _canonical(lesson_dict: dict[str, Any]) -> str:
    """Return the canonical string representation of immutable lesson fields.

    Only includes fields that must never change after a lesson is filed.
    Mutable fields (confidence, version) are excluded so legitimate updates
    do not invalidate the hash.
    """
    tags_key = "|".join(sorted(str(t) for t in lesson_dict.get("tags", [])))
    parts = [
        str(lesson_dict.get("lesson_id", "")),
        tags_key,
        str(lesson_dict.get("content", "")),
        str(lesson_dict.get("created_timestamp", "")),
        str(lesson_dict.get("filed_by_agent", "")),
        str(lesson_dict.get("task_id", "")),
    ]
    return "\x00".join(parts)  # NUL-separated; safe against field-boundary attacks


# ---------------------------------------------------------------------------
# Poison pattern detection (OWASP ASI06 2026)
# ---------------------------------------------------------------------------

# Compiled patterns representing known prompt-injection / memory-poisoning
# markers.  Each is assigned a severity weight (1-3).
_POISON_RULES: list[tuple[re.Pattern[str], int, str]] = [
    # --- Delimiter injection (model turn markers) ---
    (re.compile(r"<\|im_start\|>|<\|im_end\|>", re.IGNORECASE), 3, "model-turn delimiter injection"),
    (re.compile(r"\[/?INST\]", re.IGNORECASE), 3, "Llama instruction delimiter injection"),
    (re.compile(r"<\|endoftext\|>|<\|eot_id\|>|<\|eom_id\|>", re.IGNORECASE), 3, "EOS token injection"),
    (re.compile(r"</s>", re.IGNORECASE), 2, "EOS token injection (</s>)"),
    # --- Classic prompt override phrases ---
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE), 3, "override instruction"),
    (re.compile(r"disregard\s+(all\s+)?previous\s+instructions", re.IGNORECASE), 3, "override instruction"),
    (re.compile(r"forget\s+(everything|all)\s+you", re.IGNORECASE), 3, "context wipe attempt"),
    (re.compile(r"you\s+are\s+now\s+(a\s+)?(?!bernstein)", re.IGNORECASE), 2, "persona override"),
    # --- System-role injection ---
    (
        re.compile(r"^\s*#+\s*(system|human|assistant|user)\s*:", re.IGNORECASE | re.MULTILINE),
        2,
        "chat-role header injection",
    ),
    (re.compile(r"\n{2,}(human|assistant|system)\s*:", re.IGNORECASE), 2, "chat-role separator injection"),
    (re.compile(r"<\s*system\s*>|<\s*/\s*system\s*>", re.IGNORECASE), 2, "XML system tag injection"),
    # --- Exfiltration / SSRF bait ---
    (re.compile(r"https?://(?!localhost|127\.0\.0\.1)", re.IGNORECASE), 1, "external URL in lesson content"),
    # --- Shell / code execution bait ---
    (
        re.compile(r"`{3,}[^\n]{0,100}(bash|sh|python|exec|eval|os\.system)", re.IGNORECASE),
        2,
        "embedded shell code block",
    ),
    (re.compile(r"\bos\.system\s*\(|\bsubprocess\s*\.", re.IGNORECASE), 3, "subprocess/os.system call"),
    # --- Confidence manipulation ---
    # Detected separately in detect_memory_poisoning() via the confidence arg.
]


@dataclass(frozen=True)
class PoisonDetectionResult:
    """Result of a memory-poisoning scan on lesson content.

    Attributes:
        is_suspicious: True if content should be rejected.
        score: Weighted sum of matched rule severities.
        matched_rules: Human-readable descriptions of matched rules.
        reason: Single-sentence summary suitable for logging.
    """

    is_suspicious: bool
    score: int
    matched_rules: list[str]
    reason: str


def detect_memory_poisoning(
    content: str,
    tags: list[str],
    confidence: float | None = None,
) -> PoisonDetectionResult:
    """Scan lesson content for prompt-injection / memory-poisoning indicators.

    Checks the content string against a set of compiled regex rules.  Tags
    are also scanned because a short injected tag (e.g. ``"[INST]"``) can
    bypass content-only filters.

    Args:
        content: The lesson text.
        tags: List of tags associated with the lesson.
        confidence: Optional confidence score; artificially-high values
            (≥ 1.0 exactly as filed) are treated as a low-severity signal.

    Returns:
        PoisonDetectionResult with ``is_suspicious=True`` if the weighted
        score exceeds the threshold.
    """
    combined = content + "\n" + " ".join(tags)
    total_score = 0
    matched: list[str] = []

    for pattern, weight, label in _POISON_RULES:
        if pattern.search(combined):
            total_score += weight
            matched.append(label)

    # Artificially perfect confidence is suspicious when filed externally.
    if confidence is not None and confidence >= 1.0:
        total_score += 1
        matched.append("confidence pinned at 1.0")

    is_suspicious = total_score >= _POISON_THRESHOLD
    reason = f"Memory poisoning suspected (score={total_score}): {'; '.join(matched)}" if is_suspicious else "clean"

    return PoisonDetectionResult(
        is_suspicious=is_suspicious,
        score=total_score,
        matched_rules=matched,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Entry integrity (content hash + chain hash)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryIntegrity:
    """Cryptographic integrity fields for a single lesson entry.

    Attributes:
        content_hash: SHA-256 of the immutable lesson fields.
        prev_hash: chain_hash of the preceding entry (or GENESIS_HASH).
        chain_hash: SHA-256 of ``"chain:" + content_hash + ":" + prev_hash``.
    """

    content_hash: str
    prev_hash: str
    chain_hash: str

    def as_dict(self) -> dict[str, str]:
        """Return a plain dict for merging into a lesson JSON object."""
        return {
            "content_hash": self.content_hash,
            "prev_hash": self.prev_hash,
            "chain_hash": self.chain_hash,
        }


def build_entry_integrity(
    lesson_dict: dict[str, Any],
    prev_chain_hash: str,
) -> EntryIntegrity:
    """Compute integrity fields for a new lesson entry.

    Args:
        lesson_dict: The lesson data (must include the immutable fields).
        prev_chain_hash: ``chain_hash`` of the previous entry, or
            ``GENESIS_HASH`` for the first entry in a file.

    Returns:
        EntryIntegrity with all three hash fields populated.
    """
    content_hash = _sha256(_canonical(lesson_dict))
    chain_hash = _sha256(f"chain:{content_hash}:{prev_chain_hash}")
    return EntryIntegrity(
        content_hash=content_hash,
        prev_hash=prev_chain_hash,
        chain_hash=chain_hash,
    )


def verify_entry_hash(lesson_data: dict[str, Any]) -> bool:
    """Check that the stored content_hash matches the entry's immutable fields.

    Returns False if the hash is absent, empty, or does not match the
    re-computed value.  Does NOT verify chain position.

    Args:
        lesson_data: Parsed JSON object from a JSONL lessons file.

    Returns:
        True if the content hash is valid, False otherwise.
    """
    stored = lesson_data.get("content_hash", "")
    if not stored:
        return False
    expected = _sha256(_canonical(lesson_data))
    return stored == expected


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


@dataclass
class ChainVerifyResult:
    """Result of a full hash-chain verification over a lessons file.

    Attributes:
        valid: True if every entry's chain_hash is correct and the chain is
            unbroken from genesis to the last entry.
        errors: Human-readable descriptions of detected violations.
        entries_checked: Total number of entries examined.
        broken_at: Index of the first entry where the chain breaks (or -1).
    """

    valid: bool
    errors: list[str] = field(default_factory=list)
    entries_checked: int = 0
    broken_at: int = -1


def _verify_content_hash(data: dict[str, Any], lineno: int, lesson_id: str, result: ChainVerifyResult) -> bool:
    """Verify the content_hash field of an entry.

    Args:
        data: Parsed JSON entry.
        lineno: Line number for error messages.
        lesson_id: Entry identifier for error messages.
        result: Result accumulator (mutated on mismatch).

    Returns:
        True if content_hash is present and valid.
    """
    stored = data.get("content_hash", "")
    if not stored:
        result.errors.append(
            f"Line {lineno} ({lesson_id}): missing content_hash (entry pre-dates integrity enforcement)"
        )
        return False

    recomputed = _sha256(_canonical(data))
    if stored != recomputed:
        result.errors.append(
            f"Line {lineno} ({lesson_id}): content_hash MISMATCH "
            f"— stored={stored[:12]}… computed={recomputed[:12]}… (immutable fields tampered)"
        )
        if result.broken_at < 0:
            result.broken_at = lineno
    return True


def _verify_prev_hash(
    data: dict[str, Any], expected: str, lineno: int, lesson_id: str, result: ChainVerifyResult
) -> None:
    """Verify the prev_hash field matches the expected chain position.

    Args:
        data: Parsed JSON entry.
        expected: Expected prev_hash value.
        lineno: Line number for error messages.
        lesson_id: Entry identifier.
        result: Result accumulator.
    """
    stored = data.get("prev_hash", "")
    if stored != expected:
        result.errors.append(
            f"Line {lineno} ({lesson_id}): prev_hash MISMATCH "
            f"— stored={stored[:12] if stored else '(empty)'}… "
            f"expected={expected[:12]}… (entry inserted, deleted, or reordered)"
        )
        if result.broken_at < 0:
            result.broken_at = lineno


def _verify_chain_hash(data: dict[str, Any], lineno: int, lesson_id: str, result: ChainVerifyResult) -> str | None:
    """Verify the chain_hash field and return it if valid.

    Args:
        data: Parsed JSON entry.
        lineno: Line number for error messages.
        lesson_id: Entry identifier.
        result: Result accumulator.

    Returns:
        The stored chain_hash (for chaining), or None if missing.
    """
    stored = data.get("chain_hash", "")
    if not stored:
        result.errors.append(f"Line {lineno} ({lesson_id}): missing chain_hash")
        if result.broken_at < 0:
            result.broken_at = lineno
        return None

    stored_content = data.get("content_hash", "")
    stored_prev = data.get("prev_hash", "")
    expected = _sha256(f"chain:{stored_content}:{stored_prev}")
    if stored != expected:
        result.errors.append(
            f"Line {lineno} ({lesson_id}): chain_hash MISMATCH — stored={stored[:12]}… expected={expected[:12]}… "
        )
        if result.broken_at < 0:
            result.broken_at = lineno
    return stored


def verify_chain(lessons_path: Path) -> ChainVerifyResult:
    """Verify the hash chain across all entries in *lessons_path*.

    Detects:
    - Missing or empty ``content_hash`` / ``chain_hash`` fields
    - ``content_hash`` that does not match re-computed value (field tampering)
    - ``chain_hash`` that does not match re-computed value (chain break)
    - ``prev_hash`` mismatch (entries reordered, inserted, or deleted)

    Args:
        lessons_path: Path to the ``lessons.jsonl`` file.

    Returns:
        ChainVerifyResult describing the outcome.
    """
    result = ChainVerifyResult(valid=False)

    if not lessons_path.exists():
        result.errors.append("Lessons file not found")
        return result

    expected_prev_hash = GENESIS_HASH
    count = 0

    try:
        with open(lessons_path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    result.errors.append(f"Line {lineno}: invalid JSON entry")
                    result.broken_at = lineno
                    result.entries_checked = count
                    return result

                lesson_id = data.get("lesson_id", f"<line {lineno}>")

                if not _verify_content_hash(data, lineno, lesson_id, result):
                    count += 1
                    chain_h = data.get("chain_hash", "")
                    if chain_h:
                        expected_prev_hash = chain_h
                    continue

                _verify_prev_hash(data, expected_prev_hash, lineno, lesson_id, result)
                chain_hash = _verify_chain_hash(data, lineno, lesson_id, result)
                if chain_hash is not None:
                    expected_prev_hash = chain_hash

                count += 1

    except OSError:
        result.errors.append("Failed to read lessons file")
        result.entries_checked = count
        return result

    result.entries_checked = count
    result.valid = len(result.errors) == 0
    return result


# ---------------------------------------------------------------------------
# Provenance audit trail
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceEntry:
    """A single entry in the provenance audit trail.

    Attributes:
        line_number: 1-based line number in the JSONL file.
        lesson_id: Unique ID of the lesson.
        filed_by_agent: Agent that originally filed this lesson.
        task_id: Task that generated this lesson.
        created_timestamp: Unix timestamp when filed.
        created_iso: ISO-8601 representation for human readability.
        content_hash: SHA-256 of immutable fields (or empty string).
        chain_hash: SHA-256 of chain position (or empty string).
        prev_hash: Hash of predecessor entry (or empty string).
        hash_valid: Whether the content_hash verified correctly.
        chain_position_valid: Whether prev_hash matched the expected value.
    """

    line_number: int
    lesson_id: str
    filed_by_agent: str
    task_id: str
    created_timestamp: float
    created_iso: str
    content_hash: str
    chain_hash: str
    prev_hash: str
    hash_valid: bool
    chain_position_valid: bool


def audit_provenance(lessons_path: Path) -> list[ProvenanceEntry]:
    """Build a provenance audit trail for every entry in *lessons_path*.

    Unlike :func:`verify_chain`, this function returns a record per entry
    rather than failing fast, allowing callers to inspect the full trail
    even when errors are present.

    Args:
        lessons_path: Path to the ``lessons.jsonl`` file.

    Returns:
        List of ProvenanceEntry objects in file order.
    """
    if not lessons_path.exists():
        return []

    trail: list[ProvenanceEntry] = []
    expected_prev = GENESIS_HASH

    try:
        with open(lessons_path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                ts = float(data.get("created_timestamp", 0.0))
                created_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

                stored_content = data.get("content_hash", "")
                stored_chain = data.get("chain_hash", "")
                stored_prev = data.get("prev_hash", "")

                # Verify content hash
                hash_valid = stored_content == _sha256(_canonical(data)) if stored_content else False

                # Verify chain position
                chain_position_valid = stored_prev == expected_prev

                trail.append(
                    ProvenanceEntry(
                        line_number=lineno,
                        lesson_id=str(data.get("lesson_id", "")),
                        filed_by_agent=str(data.get("filed_by_agent", "")),
                        task_id=str(data.get("task_id", "")),
                        created_timestamp=ts,
                        created_iso=created_iso,
                        content_hash=stored_content,
                        chain_hash=stored_chain,
                        prev_hash=stored_prev,
                        hash_valid=hash_valid,
                        chain_position_valid=chain_position_valid,
                    )
                )

                # Advance expected_prev for next entry
                if stored_chain:
                    expected_prev = stored_chain
                elif stored_content:
                    # Partially-formed entry — best effort
                    expected_prev = stored_content

    except OSError:
        pass

    return trail


# ---------------------------------------------------------------------------
# Helper: read last chain_hash from an existing lessons file
# ---------------------------------------------------------------------------


def get_last_chain_hash(lessons_path: Path) -> str:
    """Return the ``chain_hash`` of the last entry in *lessons_path*.

    Used by :func:`~bernstein.core.lessons.file_lesson` to obtain the
    correct ``prev_hash`` for the next entry.

    Returns ``GENESIS_HASH`` if the file does not exist, is empty, or the
    last entry has no ``chain_hash`` field (legacy entry).

    Args:
        lessons_path: Path to the ``lessons.jsonl`` file.

    Returns:
        The chain_hash string, or ``GENESIS_HASH``.
    """
    if not lessons_path.exists():
        return GENESIS_HASH

    last_chain: str = GENESIS_HASH
    try:
        with open(lessons_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                    ch = data.get("chain_hash", "")
                    if ch:
                        last_chain = ch
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return last_chain
