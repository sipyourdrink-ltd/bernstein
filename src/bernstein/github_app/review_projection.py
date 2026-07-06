"""Tracker-comment projection of an attested review receipt (issue #2296).

The receipt is the artefact; the tracker comment is a *projection* of it --
a short verdict plus the offline ``verify`` command, never the receipt body
(AC5). A reviewer reads the verdict, then runs the printed command to
recompute ``issue_hash`` / ``diff_hash`` from the PR and check the signature
offline. The comment carries a stable marker so the responder can find and
update its own comment in place rather than stacking duplicates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.review.receipt import ReviewReceipt

#: Stable marker the responder greps for to update its comment in place.
REVIEW_RECEIPT_MARKER = "<!-- bernstein-review-receipt -->"

_PROJECTION_TEMPLATE = """\
{marker}
> **Attested review**: `{verdict}` ({finding_count} finding(s))
> Receipt anchor `{anchor_short}` binds issue to diff. Verify offline:
> `bernstein review-receipt verify --pr {pr_url} --issue <issue.md> --diff <pr.diff>`
"""


def build_review_projection(receipt: ReviewReceipt) -> str:
    """Return the tracker-comment projection of a review receipt.

    The projection references the receipt (verdict, finding count, anchor
    prefix, verify command) without embedding the signature, the public key,
    or the finding payloads.

    Args:
        receipt: The signed, anchored review receipt to project.

    Returns:
        The markdown comment body.
    """
    anchor = receipt.journal_entry_hash or ""
    anchor_short = anchor.split(":", 1)[-1][:12] if anchor else "unanchored"
    return _PROJECTION_TEMPLATE.format(
        marker=REVIEW_RECEIPT_MARKER,
        verdict=receipt.verdict,
        finding_count=len(receipt.findings),
        anchor_short=anchor_short,
        pr_url=receipt.pr_url,
    )


__all__ = ["REVIEW_RECEIPT_MARKER", "build_review_projection"]
