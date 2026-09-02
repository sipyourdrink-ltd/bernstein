"""Anchor resolution follows the target bytes, never the line number (issue #3456).

An operator annotation binds the bytes it targets. These tests hold the
property that a later edit which preserves those bytes keeps the annotation
resolving -- at its shifted range -- while an edit that removes them records
``orphaned`` with a reason code instead of silently re-anchoring to whatever
now occupies the original line numbers.
"""

from __future__ import annotations

import pytest

from bernstein.core.review.annotation_anchor import (
    ORPHAN_TARGET_BYTES_ABSENT,
    ORPHAN_TARGET_BYTES_AMBIGUOUS,
    AnnotationAnchor,
    Resolution,
    derive_anchor,
    resolve_anchor,
)

BASE = b"alpha\nbravo\nTARGET-1\nTARGET-2\nTARGET-3\ncharlie\ndelta\n"


def _anchor(
    blob: bytes = BASE, *, start: int = 3, end: int = 5, comment: str = "please rename this"
) -> AnnotationAnchor:
    return derive_anchor(blob_bytes=blob, start_line=start, end_line=end, comment=comment)


def test_anchor_resolution_follows_bytes_not_line_number() -> None:
    """Ten unrelated lines inserted above the hunk shift the range, not the anchor."""
    anchor = _anchor()
    shifted = b"".join(b"pad-%d\n" % i for i in range(10)) + BASE

    resolution = resolve_anchor(anchor, shifted)

    assert resolution.status == "resolved"
    assert (resolution.start_line, resolution.end_line) == (13, 15)
    assert resolution.reason is None


def test_anchor_resolves_unmoved_when_blob_is_unchanged() -> None:
    anchor = _anchor()

    resolution = resolve_anchor(anchor, BASE)

    assert resolution.status == "resolved"
    assert (resolution.start_line, resolution.end_line) == (3, 5)


def test_deleted_target_records_orphaned_and_never_reanchors_to_line_number() -> None:
    """The lines are removed; other content now sits at 3-5 and must not be adopted."""
    anchor = _anchor()
    without_target = b"alpha\nbravo\nusurper-1\nusurper-2\nusurper-3\ncharlie\ndelta\n"

    resolution = resolve_anchor(anchor, without_target)

    assert resolution.status == "orphaned"
    assert resolution.reason == ORPHAN_TARGET_BYTES_ABSENT
    assert resolution.start_line is None
    assert resolution.end_line is None


def test_single_byte_edit_inside_the_target_orphans_the_anchor() -> None:
    anchor = _anchor()
    edited = BASE.replace(b"TARGET-2", b"TARGET-X")

    resolution = resolve_anchor(anchor, edited)

    assert resolution.status == "orphaned"
    assert resolution.reason == ORPHAN_TARGET_BYTES_ABSENT


def test_derive_anchor_is_byte_identical_for_identical_inputs() -> None:
    first = _anchor()
    second = _anchor()

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.anchor_digest() == second.anchor_digest()
    assert first.blob_sha256 == second.blob_sha256
    assert first.comment_digest == second.comment_digest
    assert first.anchor_digest().startswith("sha256:")


def test_comment_digest_binds_the_comment_text() -> None:
    first = _anchor(comment="please rename this")
    second = _anchor(comment="please rename this.")

    assert first.comment_digest != second.comment_digest
    assert first.anchor_digest() != second.anchor_digest()


def test_repeated_target_in_a_changed_blob_orphans_rather_than_guessing() -> None:
    """Two candidate occurrences: neither can be shown to be the annotated one."""
    blob = b"x\nTARGET\ny\nTARGET\nz\n"
    anchor = derive_anchor(blob_bytes=blob, start_line=4, end_line=4, comment="here")
    grown = b"pad\n" + blob

    resolution = resolve_anchor(anchor, grown)

    assert resolution.status == "orphaned"
    assert resolution.reason == ORPHAN_TARGET_BYTES_AMBIGUOUS
    assert resolution.match_count == 2
    assert resolution.start_line is None


def test_repeated_target_still_resolves_while_the_blob_is_unchanged() -> None:
    """The blob hashes to what the operator saw, so the recorded range is theirs."""
    blob = b"x\nTARGET\ny\nTARGET\nz\n"
    anchor = derive_anchor(blob_bytes=blob, start_line=4, end_line=4, comment="here")

    resolution = resolve_anchor(anchor, blob)

    assert resolution.status == "resolved"
    assert (resolution.start_line, resolution.end_line) == (4, 4)


def test_derive_anchor_rejects_a_range_outside_the_blob() -> None:
    with pytest.raises(ValueError, match="outside"):
        derive_anchor(blob_bytes=BASE, start_line=6, end_line=99, comment="c")


def test_derive_anchor_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError, match="start_line"):
        derive_anchor(blob_bytes=BASE, start_line=5, end_line=3, comment="c")


def test_anchor_round_trips_through_its_canonical_mapping() -> None:
    anchor = _anchor()

    restored = AnnotationAnchor.from_dict(anchor.to_canonical_dict())

    assert restored == anchor
    assert restored.canonical_bytes() == anchor.canonical_bytes()


def test_resolution_round_trips_through_its_mapping() -> None:
    anchor = _anchor()
    resolution = resolve_anchor(anchor, BASE)

    assert Resolution.from_dict(resolution.to_dict()) == resolution
