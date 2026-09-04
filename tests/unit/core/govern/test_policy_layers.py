"""Issue #5117: policy composes in a fixed order and says which layer wrote what.

`PlaybookClause` declared posture as a flat list, with no notion of layering and
no record of where a clause came from. An operator staring at one target's
effective policy needs to know not just what applies but WHY.
"""

from __future__ import annotations

import pytest

from bernstein.core.govern.playbook_models import PlaybookClause
from bernstein.core.govern.policy_layers import (
    COMPOSITION_ORDER,
    LayerKind,
    PolicyCompositionError,
    PolicyLayer,
    PolicySet,
)


def _clause(surface: str, text: str = "must hold", kind: str = "required") -> PlaybookClause:
    return PlaybookClause(surface=surface, clause=text, kind=kind)


def _set(*layers: PolicyLayer) -> PolicySet:
    return PolicySet(layers=tuple(layers))


BASELINE = PolicyLayer(LayerKind.BASELINE, "common", (_clause("ssh"), _clause("audit")))
INSTRUMENTATION = PolicyLayer(LayerKind.INSTRUMENTATION, "telemetry", (_clause("audit", kind="forbidden"),))
PROD = PolicyLayer(LayerKind.CLASS_OVERLAY, "prod", (_clause("ssh", kind="forbidden"),), applies_to=("prod",))
LAB = PolicyLayer(LayerKind.CLASS_OVERLAY, "lab", (_clause("ssh"),), applies_to=("lab",))


# ---------------------------------------------------------------------------
# The fixed order
# ---------------------------------------------------------------------------


def test_the_composition_order_is_the_one_the_issue_names() -> None:
    assert COMPOSITION_ORDER == (
        LayerKind.CLASSIFICATION,
        LayerKind.BASELINE,
        LayerKind.INSTRUMENTATION,
        LayerKind.CLASS_OVERLAY,
    )


def test_a_later_layer_wins() -> None:
    policy = _set(BASELINE, INSTRUMENTATION).compose("h1")
    audit = next(entry for entry in policy.clauses if entry.clause.surface == "audit")

    assert audit.layer is LayerKind.INSTRUMENTATION
    assert audit.source == "telemetry"
    assert audit.clause.kind == "forbidden"


def test_the_class_overlay_wins_over_everything() -> None:
    policy = _set(BASELINE, INSTRUMENTATION, PROD).compose("h1", ["prod"])
    ssh = next(entry for entry in policy.clauses if entry.clause.surface == "ssh")

    assert ssh.layer is LayerKind.CLASS_OVERLAY
    assert ssh.clause.kind == "forbidden"


def test_declaration_order_inside_a_layer_kind_decides() -> None:
    first = PolicyLayer(LayerKind.BASELINE, "first", (_clause("ssh"),))
    second = PolicyLayer(LayerKind.BASELINE, "second", (_clause("ssh", kind="forbidden"),))

    policy = _set(first, second).compose("h1")
    assert policy.clauses[0].source == "second"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_every_clause_names_the_layer_and_the_source() -> None:
    """The tier alone does not answer "which baseline entry"."""
    rows, reason = _set(BASELINE, INSTRUMENTATION, PROD).compose("h1", ["prod"]).explain()

    assert ("audit", "must hold", "instrumentation:telemetry") in rows
    assert ("ssh", "must hold", "class_overlay:prod") in rows
    assert reason is None


def test_an_overridden_clause_records_what_it_beat() -> None:
    """ "From the overlay" and "from the overlay, over the baseline" are different answers."""
    policy = _set(BASELINE, INSTRUMENTATION, PROD).compose("h1", ["prod"])
    ssh = next(entry for entry in policy.clauses if entry.clause.surface == "ssh")

    assert ssh.overridden == ("baseline:common",)


def test_a_clause_nothing_overrode_records_nothing() -> None:
    policy = _set(BASELINE).compose("h1")
    assert all(entry.overridden == () for entry in policy.clauses)


def test_clauses_are_ordered_so_two_runs_print_identically() -> None:
    layer = PolicyLayer(LayerKind.BASELINE, "b", (_clause("zulu"), _clause("alpha"), _clause("mike")))
    policy = _set(layer).compose("h1")
    assert [entry.clause.surface for entry in policy.clauses] == ["alpha", "mike", "zulu"]


# ---------------------------------------------------------------------------
# Exactly one overlay
# ---------------------------------------------------------------------------


def test_two_matching_overlays_are_a_finding_not_a_default() -> None:
    """`kind` has no way to express "ambiguous", so this had to be new.

    Taking whichever the iteration reached last is an answer that depends on
    file layout and changes when somebody adds an unrelated overlay above it.
    """
    policy = _set(BASELINE, PROD, LAB).compose("h1", ["prod", "lab"])

    assert policy.is_ambiguous is True
    assert policy.finding is not None
    assert policy.finding.matched == ("prod", "lab")
    assert "2 class overlays" in policy.finding.reason


def test_no_matching_overlay_is_also_a_finding() -> None:
    policy = _set(BASELINE, PROD).compose("h1", ["staging"])

    assert policy.is_ambiguous is True
    assert policy.finding is not None
    assert policy.finding.matched == ()
    assert "no class overlay" in policy.finding.reason


def test_an_ambiguous_target_still_composes_everything_below_the_overlay() -> None:
    """The baseline applies whatever the class turns out to be.

    Reporting nothing would hide posture that is not in doubt.
    """
    policy = _set(BASELINE, INSTRUMENTATION, PROD, LAB).compose("h1", ["prod", "lab"])

    assert [entry.clause.surface for entry in policy.clauses] == ["audit", "ssh"]
    ssh = next(entry for entry in policy.clauses if entry.clause.surface == "ssh")
    # From the baseline, NOT from either candidate overlay.
    assert ssh.layer is LayerKind.BASELINE


def test_exactly_one_overlay_is_clean() -> None:
    policy = _set(BASELINE, PROD, LAB).compose("h1", ["prod"])
    assert policy.is_ambiguous is False
    assert policy.finding is None


# ---------------------------------------------------------------------------
# Layer validation
# ---------------------------------------------------------------------------


def test_an_unnamed_layer_is_refused() -> None:
    with pytest.raises(PolicyCompositionError, match="must be named"):
        PolicyLayer(LayerKind.BASELINE, "   ")


def test_a_class_overlay_with_no_applies_to_is_refused() -> None:
    """It could never be selected, so declaring it is declaring nothing."""
    with pytest.raises(PolicyCompositionError, match="never be selected"):
        PolicyLayer(LayerKind.CLASS_OVERLAY, "orphan")


def test_a_non_overlay_carrying_applies_to_is_refused() -> None:
    """A baseline that silently does not apply somewhere is not a baseline."""
    with pytest.raises(PolicyCompositionError, match="cannot declare"):
        PolicyLayer(LayerKind.BASELINE, "common", applies_to=("prod",))


# ---------------------------------------------------------------------------
# The hash sees a reorder
# ---------------------------------------------------------------------------


def test_reordering_the_baseline_moves_the_hash() -> None:
    """The one edit that changes what WINS without changing what is DECLARED.

    Without position in the hash it would be the one edit a desired-state diff
    cannot see.
    """
    first = PolicyLayer(LayerKind.BASELINE, "first", (_clause("ssh"),))
    second = PolicyLayer(LayerKind.BASELINE, "second", (_clause("ssh", kind="forbidden"),))

    assert _set(first, second).content_hash() != _set(second, first).content_hash()


def test_the_same_document_hashes_the_same() -> None:
    assert _set(BASELINE, PROD).content_hash() == _set(BASELINE, PROD).content_hash()


def test_changing_a_clause_moves_the_hash() -> None:
    other = PolicyLayer(LayerKind.BASELINE, "common", (_clause("ssh"), _clause("audit", "changed")))
    assert _set(other).content_hash() != _set(BASELINE).content_hash()


def test_the_hash_is_prefixed_like_the_playbooks() -> None:
    assert _set(BASELINE).content_hash().startswith("sha256:")
