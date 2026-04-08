"""Agent output fingerprinting for detecting copy-paste from training data.

Detects when agent output is likely copied verbatim from training data
(license risk). Uses MinHash/LSH similarity to compare output against
known code patterns. Flags matches above a configurable threshold for
human review.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 0.7
_DEFAULT_NUM_PERM = 128
_DEFAULT_NGRAM_SIZE = 5
_DEFAULT_SHINGLE_TYPE = "token"
_SEED_PRIME = 4294967311
_MAX_HASH = (1 << 32) - 1


@dataclass(frozen=True)
class FingerprintConfig:
    """Configuration for the output fingerprinting gate.

    Attributes:
        enabled: Master switch.
        threshold: Jaccard similarity threshold (0.0-1.0). Matches at or
            above this value are flagged.
        num_perm: Number of MinHash permutations (higher = more accurate).
        ngram_size: Size of n-grams (shingles) for tokenization.
        shingle_type: Either "token" (word-level) or "char" (character-level).
        block_on_match: Whether a match above threshold blocks the task.
        corpus_paths: Paths to corpus files/directories for comparison.
    """

    enabled: bool = False
    threshold: float = _DEFAULT_THRESHOLD
    num_perm: int = _DEFAULT_NUM_PERM
    ngram_size: int = _DEFAULT_NGRAM_SIZE
    shingle_type: str = _DEFAULT_SHINGLE_TYPE
    block_on_match: bool = False
    corpus_paths: tuple[str, ...] = ()


@dataclass
class FingerprintMatch:
    """A single fingerprint match result.

    Attributes:
        source_label: Label identifying the corpus entry.
        similarity: Jaccard similarity estimate (0.0-1.0).
        flagged: Whether this match exceeds the threshold.
    """

    source_label: str
    similarity: float
    flagged: bool


@dataclass
class FingerprintResult:
    """Result of the fingerprinting analysis.

    Attributes:
        passed: True if no matches exceed the threshold.
        blocked: Whether the gate blocks the task.
        detail: Human-readable summary.
        matches: List of matches found.
        errors: Errors encountered during analysis.
    """

    passed: bool
    blocked: bool
    detail: str
    matches: list[FingerprintMatch] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_code(text: str) -> str:
    """Normalize code for comparison: strip comments, collapse whitespace."""
    # Remove single-line comments
    text = re.sub(r"#[^\n]*", "", text)
    # Remove docstrings (triple-quoted)
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    text = re.sub(r"'''[\s\S]*?'''", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def _tokenize(text: str, ngram_size: int, shingle_type: str) -> set[str]:
    """Split text into n-gram shingles."""
    if shingle_type == "char":
        return {text[i : i + ngram_size] for i in range(len(text) - ngram_size + 1)}

    # Token (word) shingles
    tokens = text.split()
    if len(tokens) < ngram_size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + ngram_size]) for i in range(len(tokens) - ngram_size + 1)}


def _hash_shingle(shingle: str) -> int:
    """Hash a shingle to a 32-bit integer using MD5."""
    digest = hashlib.md5(shingle.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4], "little")


class MinHash:
    """MinHash signature for Jaccard similarity estimation.

    Uses a simple universal hashing scheme: h_i(x) = (a_i * x + b_i) mod p mod 2^32
    where a_i, b_i are derived from the permutation index.
    """

    __slots__ = ("_hashvalues", "_num_perm")

    def __init__(self, num_perm: int = _DEFAULT_NUM_PERM) -> None:
        self._num_perm = num_perm
        self._hashvalues = [_MAX_HASH] * num_perm

    @property
    def hashvalues(self) -> list[int]:
        return list(self._hashvalues)

    def update(self, shingles: set[str]) -> None:
        """Update the MinHash signature with a set of shingles."""
        for shingle in shingles:
            h = _hash_shingle(shingle)
            for i in range(self._num_perm):
                # Universal hash: (a * h + b) mod p mod 2^32
                a = (i + 1) * 6364136223846793005 & _MAX_HASH
                b = i * 1442695040888963407 & _MAX_HASH
                val = (a * h + b) & _MAX_HASH
                if val < self._hashvalues[i]:
                    self._hashvalues[i] = val

    def jaccard(self, other: MinHash) -> float:
        """Estimate Jaccard similarity with another MinHash."""
        if self._num_perm != other._num_perm:
            msg = "Cannot compare MinHash signatures with different num_perm"
            raise ValueError(msg)
        matches = sum(
            1 for a, b in zip(self._hashvalues, other._hashvalues, strict=True) if a == b
        )
        return matches / self._num_perm


def compute_minhash(
    text: str,
    num_perm: int = _DEFAULT_NUM_PERM,
    ngram_size: int = _DEFAULT_NGRAM_SIZE,
    shingle_type: str = _DEFAULT_SHINGLE_TYPE,
) -> MinHash:
    """Compute a MinHash signature for the given text."""
    normalized = _normalize_code(text)
    shingles = _tokenize(normalized, ngram_size, shingle_type)
    mh = MinHash(num_perm=num_perm)
    mh.update(shingles)
    return mh


class CorpusIndex:
    """In-memory index of MinHash signatures for known code."""

    __slots__ = ("_config", "_entries")

    def __init__(self, config: FingerprintConfig) -> None:
        self._config = config
        self._entries: list[tuple[str, MinHash]] = []

    @property
    def size(self) -> int:
        return len(self._entries)

    def add(self, label: str, text: str) -> None:
        """Add a code sample to the index."""
        mh = compute_minhash(
            text,
            num_perm=self._config.num_perm,
            ngram_size=self._config.ngram_size,
            shingle_type=self._config.shingle_type,
        )
        self._entries.append((label, mh))

    def query(self, text: str) -> list[FingerprintMatch]:
        """Query the index for matches against the given text."""
        if not self._entries:
            return []

        query_mh = compute_minhash(
            text,
            num_perm=self._config.num_perm,
            ngram_size=self._config.ngram_size,
            shingle_type=self._config.shingle_type,
        )

        matches: list[FingerprintMatch] = []
        for label, corpus_mh in self._entries:
            sim = query_mh.jaccard(corpus_mh)
            if sim >= self._config.threshold * 0.5:  # Keep near-matches for context
                matches.append(
                    FingerprintMatch(
                        source_label=label,
                        similarity=round(sim, 4),
                        flagged=sim >= self._config.threshold,
                    )
                )

        matches.sort(key=lambda m: m.similarity, reverse=True)
        return matches


def check_fingerprint(
    code: str,
    corpus_index: CorpusIndex,
    config: FingerprintConfig,
) -> FingerprintResult:
    """Check agent-produced code against the corpus index.

    Args:
        code: The agent-produced code to check.
        corpus_index: Pre-built index of known code.
        config: Fingerprinting configuration.

    Returns:
        FingerprintResult with match details.
    """
    if not config.enabled:
        return FingerprintResult(
            passed=True,
            blocked=False,
            detail="Output fingerprinting disabled.",
        )

    if not code.strip():
        return FingerprintResult(
            passed=True,
            blocked=False,
            detail="No code to fingerprint (empty input).",
        )

    try:
        matches = corpus_index.query(code)
    except Exception as exc:
        logger.warning("Fingerprint query failed: %s", exc)
        return FingerprintResult(
            passed=False,
            blocked=False,
            detail=f"Fingerprint analysis error: {exc}",
            errors=[str(exc)],
        )

    flagged = [m for m in matches if m.flagged]

    if flagged:
        labels = ", ".join(m.source_label for m in flagged[:5])
        detail = (
            f"Potential training data match detected ({len(flagged)} source(s) "
            f"above {config.threshold:.0%} threshold): {labels}"
        )
        return FingerprintResult(
            passed=False,
            blocked=config.block_on_match,
            detail=detail,
            matches=matches,
        )

    return FingerprintResult(
        passed=True,
        blocked=False,
        detail=f"No matches above {config.threshold:.0%} threshold ({len(matches)} near-matches checked).",
        matches=matches,
    )


def extract_code_from_diff(diff: str) -> str:
    """Extract added lines from a unified diff for fingerprinting."""
    added_lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:])
    return "\n".join(added_lines)
