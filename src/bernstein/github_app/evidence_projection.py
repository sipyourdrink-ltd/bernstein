"""Tracker-comment projection of a sealed evidence bundle (issue #2362).

The bundle is the artefact; the tracker/PR comment is a *projection* of it -- a
short gate verdict, the pass/fail counts, the anchor prefix, and the offline
``verify`` command, never the evidence bytes. A reviewer reads the verdict, then
runs the printed command to recompute the bundle from the stored evidence
offline. The comment carries a stable marker so the responder can find and
update its own comment in place rather than stacking duplicates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.evidence.bundle import EvidenceBundle

#: Stable marker the responder greps for to update its comment in place.
EVIDENCE_BUNDLE_MARKER = "<!-- bernstein-evidence-bundle -->"

_PROJECTION_TEMPLATE = """\
{marker}
> **Verification evidence**: gate `{verdict}` ({passed} passed / {failed} failed)
> Bundle anchor `{anchor_short}` seals proof-of-done for task `{task_id}`. Verify offline:
> `bernstein evidence verify {task_id}`
"""


def build_evidence_projection(bundle: EvidenceBundle) -> str:
    """Return the tracker-comment projection of a sealed evidence bundle.

    The projection references the bundle (gate verdict, producer counts, anchor
    prefix, verify command) without embedding any captured evidence bytes.

    Args:
        bundle: The signed, anchored evidence bundle to project.

    Returns:
        The markdown comment body.
    """
    anchor = bundle.journal_entry_hash or ""
    anchor_short = anchor.split(":", 1)[-1][:12] if anchor else "unanchored"
    return _PROJECTION_TEMPLATE.format(
        marker=EVIDENCE_BUNDLE_MARKER,
        verdict="pass" if bundle.gate_passed else "fail",
        passed=bundle.passed_count,
        failed=bundle.failed_count,
        anchor_short=anchor_short,
        task_id=bundle.task_id,
    )


__all__ = ["EVIDENCE_BUNDLE_MARKER", "build_evidence_projection"]
