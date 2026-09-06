"""
bernstein-bench: task admission and contamination detection.

A task whose reference solution exists verbatim in public code hosting
measures retrieval, not capability.  During task admission, the reference
solution is fingerprinted (n-gram overlap analysis) against public corpora
and rejected when found contaminated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from bernstein.eval.bench.suite import BenchTask


# ---------------------------------------------------------------------------
# Contamination Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContaminationVerdict:
    """The outcome of an admission-time contamination check."""

    is_contaminated: bool
    overlap_score: float  # [0.0, 1.0] fraction of n-grams found in public corpus
    matched_ngrams: tuple[str, ...]
    threshold: float = 0.8
    n: int = 5
    detail: str = ""


# ---------------------------------------------------------------------------
# Fingerprinting & N-Gram Extraction
# ---------------------------------------------------------------------------


_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Tokenize code or text into normalized lowercase tokens."""
    tokens = [t.lower() for t in _TOKEN_SPLIT_RE.split(text) if t]
    return tokens


def extract_ngrams(text: str, n: int = 5) -> set[tuple[str, ...]]:
    """Extract a set of n-grams (as tuples of strings) from *text*."""
    tokens = _tokenize(text)
    if not tokens:
        return set()
    if len(tokens) < n:
        # If text is shorter than n, return the entire token sequence as a single gram
        return {tuple(tokens)}
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


# ---------------------------------------------------------------------------
# Contamination Checking
# ---------------------------------------------------------------------------


def check_solution_contamination(
    solution_text: str,
    public_corpus: Collection[str] | Sequence[str],
    n: int = 5,
    threshold: float = 0.8,
) -> ContaminationVerdict:
    """
    Check *solution_text* against *public_corpus* using n-gram fingerprinting.

    Returns a :class:`ContaminationVerdict` with the overlap score, matched
    n-grams, and whether the verdict exceeds *threshold*.
    """
    cleaned_solution = solution_text.strip()
    if not cleaned_solution:
        return ContaminationVerdict(
            is_contaminated=False,
            overlap_score=0.0,
            matched_ngrams=(),
            threshold=threshold,
            n=n,
            detail="Empty reference solution; no contamination detected.",
        )

    # Check verbatim inclusion first
    for doc in public_corpus:
        doc_clean = doc.strip()
        if not doc_clean:
            continue
        if cleaned_solution == doc_clean or cleaned_solution in doc:
            return ContaminationVerdict(
                is_contaminated=True,
                overlap_score=1.0,
                matched_ngrams=(cleaned_solution[:80] + ("..." if len(cleaned_solution) > 80 else ""),),
                threshold=threshold,
                n=n,
                detail="Exact verbatim match found in public corpus.",
            )

    solution_ngrams = extract_ngrams(solution_text, n=n)
    if not solution_ngrams:
        return ContaminationVerdict(
            is_contaminated=False,
            overlap_score=0.0,
            matched_ngrams=(),
            threshold=threshold,
            n=n,
            detail="No n-grams extracted from solution.",
        )

    # Build corpus n-grams
    corpus_ngrams: set[tuple[str, ...]] = set()
    for doc in public_corpus:
        corpus_ngrams.update(extract_ngrams(doc, n=n))

    matched = solution_ngrams.intersection(corpus_ngrams)
    overlap_score = len(matched) / len(solution_ngrams)

    is_contaminated = overlap_score >= threshold
    matched_strings = tuple(sorted(" ".join(g) for g in matched))

    detail = (
        f"Contamination overlap {overlap_score:.2%} >= threshold {threshold:.2%} "
        f"({len(matched)}/{len(solution_ngrams)} n-grams matched)."
        if is_contaminated
        else f"Clean overlap {overlap_score:.2%} < threshold {threshold:.2%}."
    )

    return ContaminationVerdict(
        is_contaminated=is_contaminated,
        overlap_score=overlap_score,
        matched_ngrams=matched_strings,
        threshold=threshold,
        n=n,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Task Admission Gate
# ---------------------------------------------------------------------------


def admit_task(
    task: BenchTask,
    reference_solution: str,
    public_corpus: Collection[str] | Sequence[str],
    n: int = 5,
    threshold: float = 0.8,
) -> tuple[bool, ContaminationVerdict]:
    """
    Admission gate for a task into a benchmark suite.

    The task's reference solution is checked for contamination. Returns
    ``(admitted, verdict)``. If admitted is False, the task must be rejected.
    """
    verdict = check_solution_contamination(reference_solution, public_corpus, n=n, threshold=threshold)
    admitted = not verdict.is_contaminated
    return admitted, verdict
