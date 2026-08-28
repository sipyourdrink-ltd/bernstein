"""GitHub projection for volunteer protocol documents.

Maps each volunteer document kind (claim, submission, verdict, merge-receipt)
to and from a GitHub issue comment / PR body representation.

The format is a fenced JSON block wrapped in a bernstein marker, identical to
the convention used by the conformance harness in
:mod:`bernstein.core.protocols.volunteer.conformance`.  A human reader can see
the document; a machine can extract and re-parse it.

Each document kind gets its own projection helpers so callers can pick the
right one without pattern-matching on ``document_kind`` at the call site.
"""

from __future__ import annotations

from typing import Any

# Runtime imports for the kind_map dispatcher.
from bernstein.core.protocols.volunteer.claim import Claim
from bernstein.core.protocols.volunteer.receipt import MergeReceipt
from bernstein.core.protocols.volunteer.submission import Submission
from bernstein.core.protocols.volunteer.verdict import VerificationVerdict

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _to_github_comment(doc: dict[str, Any]) -> str:
    """Wrap a canonical dict in a GitHub-issue-comment friendly format.

    Format::

        <!-- bernstein-volunteer-doc -->
        ```json
        <canonical JSON of doc>
        ```
    """
    from bernstein.core.protocols.volunteer.conformance import to_github_projection

    return to_github_projection(doc)


def _from_github_comment(raw: str) -> dict[str, Any]:
    """Extract a canonical dict from a GitHub-issue-comment string."""
    from bernstein.core.protocols.volunteer.conformance import from_github_projection

    return from_github_projection(raw)


# ---------------------------------------------------------------------------
# Claim projection
# ---------------------------------------------------------------------------


def to_github_claim(claim: Claim) -> str:
    """Project a :class:`Claim` onto a GitHub issue comment string."""
    return _to_github_comment(claim.to_canonical_dict())


def from_github_claim(raw: str) -> Claim:
    """Parse a :class:`Claim` from a GitHub issue comment string."""
    from bernstein.core.protocols.volunteer.claim import Claim

    doc = _from_github_comment(raw)
    return Claim(**doc)


# ---------------------------------------------------------------------------
# Submission projection
# ---------------------------------------------------------------------------


def to_github_submission(submission: Submission) -> str:
    """Project a :class:`Submission` onto a GitHub issue comment string."""
    return _to_github_comment(submission.to_canonical_dict())


def from_github_submission(raw: str) -> Submission:
    """Parse a :class:`Submission` from a GitHub issue comment string."""
    from bernstein.core.protocols.volunteer.submission import Submission

    doc = _from_github_comment(raw)
    return Submission(**doc)


# ---------------------------------------------------------------------------
# Verdict projection
# ---------------------------------------------------------------------------


def to_github_verdict(verdict: VerificationVerdict) -> str:
    """Project a :class:`VerificationVerdict` onto a GitHub issue comment string."""
    return _to_github_comment(verdict.to_canonical_dict())


def from_github_verdict(raw: str) -> VerificationVerdict:
    """Parse a :class:`VerificationVerdict` from a GitHub issue comment string."""
    from bernstein.core.protocols.volunteer.verdict import VerificationVerdict

    doc = _from_github_comment(raw)
    return VerificationVerdict(**doc)


# ---------------------------------------------------------------------------
# Merge-receipt projection
# ---------------------------------------------------------------------------


def to_github_merge_receipt(receipt: MergeReceipt) -> str:
    """Project a :class:`MergeReceipt` onto a GitHub issue comment string."""
    return _to_github_comment(receipt.to_canonical_dict())


def from_github_merge_receipt(raw: str) -> MergeReceipt:
    """Parse a :class:`MergeReceipt` from a GitHub issue comment string."""
    from bernstein.core.protocols.volunteer.receipt import MergeReceipt

    doc = _from_github_comment(raw)
    return MergeReceipt(**doc)


# ---------------------------------------------------------------------------
# Universal dispatcher
# ---------------------------------------------------------------------------


def to_github_projection(document: Any) -> str:
    """Project any volunteer protocol document to a GitHub comment string.

    Dispatches based on the document type.  Raises ``TypeError`` for unknown
    types so callers get a clear signal rather than a silent failure.

    Args:
        document: A :class:`Claim`, :class:`Submission`,
            :class:`VerificationVerdict`, or :class:`MergeReceipt`.

    Returns:
        A GitHub-compatible comment string.

    Raises:
        TypeError: If ``document`` is not a recognised volunteer document type.
    """
    from bernstein.core.protocols.volunteer.claim import Claim
    from bernstein.core.protocols.volunteer.receipt import MergeReceipt
    from bernstein.core.protocols.volunteer.submission import Submission
    from bernstein.core.protocols.volunteer.verdict import VerificationVerdict

    if isinstance(document, Claim):
        return to_github_claim(document)
    if isinstance(document, Submission):
        return to_github_submission(document)
    if isinstance(document, VerificationVerdict):
        return to_github_verdict(document)
    if isinstance(document, MergeReceipt):
        return to_github_merge_receipt(document)
    raise TypeError(f"unsupported document type {type(document).__name__}")


def from_github_projection(raw: str, document_kind: str) -> Any:
    """Parse a GitHub comment string back into the matching document type.

    Args:
        raw: A GitHub issue comment string (as returned by the GitHub API).
        document_kind: The document kind to instantiate
            (``"claim"``, ``"submission"``, ``"verification-verdict"``,
            ``"merge-receipt"``).

    Returns:
        The parsed document instance.

    Raises:
        TypeError: If ``document_kind`` is not recognised.
        ValueError: If the comment does not contain a valid document.
    """
    doc = _from_github_comment(raw)
    kind_map = {
        "claim": Claim,
        "submission": Submission,
        "verification-verdict": VerificationVerdict,
        "merge-receipt": MergeReceipt,
    }
    cls = kind_map.get(document_kind)
    if cls is None:
        raise TypeError(f"unknown document_kind {document_kind!r}; expected one of {sorted(kind_map)}")
    return cls(**doc)
