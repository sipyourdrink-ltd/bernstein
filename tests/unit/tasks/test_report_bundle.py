"""Report bundle + figures_grounded pure evaluator (issue #2888).

The bundle carries the report body and its ``figures.json`` sidecar inside one
canonical byte string, so editing a figure changes the artifact ``content_hash``
(AC4). The pure evaluator resolves anchors through an injected callable and
names each unanchored figure with its location (AC1), independent of lineage.
"""

from __future__ import annotations

import pytest

from bernstein.core.tasks.artifacts import CanonicalisationError, content_hash
from bernstein.core.tasks.figures import (
    AnchorResolution,
    Figure,
    FigureAnchor,
    ReportBundle,
    canonicalise_report_bundle,
    evaluate_figures_grounded,
    is_report_bundle,
    parse_report_bundle,
)


def _anchor(ref: str = "sha256:" + "a" * 64) -> FigureAnchor:
    return FigureAnchor(kind="artifact", ref=ref)


def _bundle(body: str, figures: tuple[Figure, ...]) -> ReportBundle:
    return ReportBundle(body=body, figures=figures)


# ---------------------------------------------------------------------------
# Anchor validation
# ---------------------------------------------------------------------------


def test_anchor_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown figure anchor kind"):
        FigureAnchor(kind="telepathy", ref="sha256:" + "a" * 64)


def test_hash_anchor_requires_sha256_ref() -> None:
    with pytest.raises(ValueError, match="content hash"):
        FigureAnchor(kind="attachment", ref="not-a-hash")


def test_receipt_anchor_accepts_opaque_ref() -> None:
    # The receipt anchor kind (issue #2887) plugs in without a hash constraint.
    a = FigureAnchor(kind="receipt", ref="rcpt-2f8c")
    assert a.ref == "rcpt-2f8c"


# ---------------------------------------------------------------------------
# Canonicalisation + parse + content_hash covers the sidecar (AC4)
# ---------------------------------------------------------------------------


def test_bundle_round_trips_through_canonical_bytes() -> None:
    fig = Figure(value="1,234", unit="users", label="migrated users", anchor=_anchor())
    bundle = _bundle("We migrated 1,234 users.\n", (fig,))
    parsed = parse_report_bundle(canonicalise_report_bundle(bundle))
    assert parsed.body == "We migrated 1,234 users.\n"
    assert parsed.figures[0].value == "1,234"
    assert parsed.figures[0].anchor.ref == fig.anchor.ref


def test_editing_a_figure_value_changes_content_hash() -> None:
    fig = Figure(value="1,234", unit="users", label="migrated", anchor=_anchor())
    base = _bundle("We migrated 1,234 users.\n", (fig,))
    edited = _bundle(
        "We migrated 1,234 users.\n",
        (Figure(value="9,999", unit="users", label="migrated", anchor=_anchor()),),
    )
    h_base = content_hash(canonicalise_report_bundle(base))
    h_edited = content_hash(canonicalise_report_bundle(edited))
    assert h_base != h_edited


def test_editing_an_anchor_changes_content_hash() -> None:
    body = "We migrated 1,234 users.\n"
    base = _bundle(body, (Figure("1,234", "users", "m", _anchor("sha256:" + "a" * 64)),))
    moved = _bundle(body, (Figure("1,234", "users", "m", _anchor("sha256:" + "b" * 64)),))
    assert content_hash(canonicalise_report_bundle(base)) != content_hash(canonicalise_report_bundle(moved))


def test_canonical_bytes_are_deterministic() -> None:
    fig = Figure("1,234", "users", "m", _anchor())
    b1 = canonicalise_report_bundle(_bundle("body 1,234\n", (fig,)))
    b2 = canonicalise_report_bundle(_bundle("body 1,234\n", (Figure("1,234", "users", "m", _anchor()),)))
    assert b1 == b2


def test_bundle_body_must_be_nfc() -> None:
    # A decomposed 'é' (e + combining acute) is rejected, not repaired.
    with pytest.raises(CanonicalisationError, match="NFC"):
        canonicalise_report_bundle(_bundle("café 1,234\n", (Figure("1,234", "", "", _anchor()),)))


def test_plain_report_text_is_not_a_bundle() -> None:
    assert is_report_bundle(b"just prose, no bundle") is False
    assert is_report_bundle(b'{"body":"x","figures":[]}') is True


# ---------------------------------------------------------------------------
# figures_grounded pure evaluation
# ---------------------------------------------------------------------------


def _all_ok(_a: FigureAnchor) -> AnchorResolution:
    return AnchorResolution(ok=True, statement="traces to artifact sha256:aaaa, recorded at chain position 1")


def _all_broken(_a: FigureAnchor) -> AnchorResolution:
    return AnchorResolution(ok=False, statement="resolves to no verifying lineage record")


def test_fully_grounded_report_passes() -> None:
    fig = Figure("1,234", "users", "migrated users", _anchor())
    bundle = _bundle(
        "We migrated 1,234 users at 9.9% cost.\n",
        (
            fig,
            Figure("9.9", "%", "cost ratio", _anchor("sha256:" + "c" * 64)),
        ),
    )
    verdict = evaluate_figures_grounded(bundle, resolve_anchor=_all_ok)
    assert verdict.ok, verdict.failures
    assert verdict.has_figures
    assert len(verdict.provenances) == 2
    assert all(p.ok for p in verdict.provenances)


def test_removing_one_anchor_fails_naming_that_figure_and_location() -> None:
    # Same report, but the 9.9% figure is no longer declared in the sidecar.
    bundle = _bundle(
        "We migrated 1,234 users at 9.9% cost.\n",
        (Figure("1,234", "users", "migrated users", _anchor()),),
    )
    verdict = evaluate_figures_grounded(bundle, resolve_anchor=_all_ok)
    assert not verdict.ok
    assert len(verdict.unanchored) == 1
    un = verdict.unanchored[0]
    assert un.surface == "9.9%"
    assert un.category == "percentage"
    assert un.line == 1
    # The failure names the exact number and its location.
    assert any("9.9%" in f and "line 1" in f for f in verdict.failures)


def test_figure_with_unresolvable_anchor_fails_naming_the_figure() -> None:
    fig = Figure("1,234", "users", "migrated users", _anchor())
    bundle = _bundle("We migrated 1,234 users.\n", (fig,))
    verdict = evaluate_figures_grounded(bundle, resolve_anchor=_all_broken)
    assert not verdict.ok
    assert any("migrated users" in f and "not grounded" in f for f in verdict.failures)
    assert verdict.provenances[0].ok is False


def test_report_with_no_figures_and_no_material_numbers_passes() -> None:
    bundle = _bundle("Section 4 has 3 steps, per §2.\n", ())
    verdict = evaluate_figures_grounded(bundle, resolve_anchor=_all_broken)
    assert verdict.ok
    assert verdict.has_figures is False
