"""Graded chain verdict over delegation receipts (issue #2554).

``ChainResult.valid`` cannot tell a chain whose narrowing was checked and held
from a chain that recorded no scope to check: both reach the caller as True.
The cases below are grouped by the invariant each one pins, and the first
group asserts that those two chains stop looking alike.

Most cases build receipts directly and call :func:`grade_chain`, because the
interesting inputs (a reference that contradicts its own body, a scope axis the
comparator cannot read) cannot be produced through the ledger writer. The
end-to-end group goes through the ledger so the wiring is covered too.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity import delegation
from bernstein.core.identity.delegation_scope import (
    DIAGNOSTIC_SCOPE_REF_ONLY_RESOLVED,
    DIAGNOSTIC_SCOPE_REF_UNRESOLVED,
    REASON_AXIS_WIDENED,
    REASON_AXIS_WIDENED_VS_ANCESTOR,
    REASON_CHAIN_INVALID,
    REASON_COMPARISON_AXIS_UNSUPPORTED,
    REASON_NO_SCOPE_RECORDED,
    REASON_PARENT_RECEIPT_UNAVAILABLE,
    REASON_PARENT_SCOPE_UNAVAILABLE,
    REASON_ROOT_CLAIMED_MID_CHAIN,
    REASON_ROOT_STRUCTURAL_ONLY,
    REASON_SCOPE_MISSING,
    REASON_SCOPE_REF_CONFLICT,
    REASON_SCOPE_REF_ONLY_UNRESOLVED,
    VERDICT_DIAGNOSTICS,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_REASONS,
    VERDICT_UNPROVEN,
    ChainVerdict,
    DelegationScope,
    HopVerdict,
    grade_chain,
)

KEY = b"k" * 32
GENESIS = delegation.GENESIS_HMAC

WIDE = DelegationScope(
    permissions=frozenset({"files.read", "files.write"}),
    task_ids=frozenset({"t-1", "t-2"}),
    max_depth=3,
)
NARROW = DelegationScope(
    permissions=frozenset({"files.read"}),
    task_ids=frozenset({"t-1"}),
    max_depth=2,
)
WIDER = DelegationScope(
    permissions=frozenset({"files.read", "files.write", "agents.spawn"}),
    task_ids=frozenset({"t-1"}),
    max_depth=1,
)


def _receipt(
    index: int,
    *,
    scope: DelegationScope | None = None,
    scope_ref: str | None = None,
    scope_body: dict | None = None,
    parent_ref: str | None = None,
) -> delegation.DelegationReceipt:
    """Build one receipt without going through the HMAC writer.

    ``scope_ref`` overrides the content address so a body can be made to
    contradict its own reference, which the ledger writer will never emit.
    """
    body = scope_body if scope_body is not None else (scope.to_body() if scope else None)
    ref = scope_ref
    if ref is None and scope is not None:
        ref = scope.scope_ref()
    return delegation.DelegationReceipt(
        run_id="run",
        hop_index=index,
        issuer=f"p{index}",
        subject=f"p{index}",
        audience=f"p{index + 1}",
        act="task.delegate",
        created=1_700_000_000 + index,
        hmac=f"{index + 1:064x}",
        parent_ref=parent_ref,
        scope_ref=ref,
        scope=body,
    )


def _rows(verdict: ChainVerdict) -> dict[int, HopVerdict]:
    return {row.hop_index: row for row in verdict.hops}


# ---------------------------------------------------------------------------
# D: a legacy chain carrying no scope at all
# ---------------------------------------------------------------------------


class TestLegacyChainIsUnproven:
    """The case ``valid`` gets wrong, and the reason this surface exists."""

    LEGACY = [_receipt(0), _receipt(1)]

    def test_the_graded_verdict_is_unproven_not_pass(self):
        verdict = grade_chain(self.LEGACY)
        assert verdict.verdict == VERDICT_UNPROVEN
        assert verdict.ok is False

    def test_it_carries_its_own_named_reason(self):
        assert REASON_NO_SCOPE_RECORDED in grade_chain(self.LEGACY).reasons

    def test_every_hop_says_why_it_could_not_be_checked(self):
        verdict = grade_chain(self.LEGACY)
        assert [row.verdict for row in verdict.hops] == [VERDICT_UNPROVEN] * 2
        for row in verdict.hops:
            assert REASON_SCOPE_MISSING in row.reasons

    def test_a_checked_pass_is_distinguishable_from_it(self):
        checked = grade_chain([_receipt(0, scope=WIDE), _receipt(1, scope=NARROW)])
        assert checked.verdict == VERDICT_PASS
        assert grade_chain(self.LEGACY).verdict != checked.verdict

    def test_the_legacy_chain_still_reaches_the_caller_as_valid(self, tmp_path):
        ledger = delegation.DelegationLedger(root=tmp_path, key=KEY)
        for i in range(2):
            ledger.record_hop(
                run_id="legacy",
                issuer=f"p{i}",
                subject=f"p{i}",
                audience=f"p{i + 1}",
                act="task.delegate",
            )
        result = delegation.verify_run_chain(root=tmp_path, run_id="legacy", key=KEY)
        assert result.valid is True
        assert result.verdict.verdict == VERDICT_UNPROVEN
        assert REASON_NO_SCOPE_RECORDED in result.verdict.reasons


# ---------------------------------------------------------------------------
# A: three-valued rows, composition, and no short-circuit
# ---------------------------------------------------------------------------


class TestCompositionAndRows:
    def test_every_hop_gets_exactly_one_row(self):
        verdict = grade_chain([_receipt(i, scope=NARROW if i else WIDE) for i in range(4)])
        assert [row.hop_index for row in verdict.hops] == [0, 1, 2, 3]

    def test_fail_dominates_unproven(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1), _receipt(2, scope=WIDER)])
        assert verdict.verdict == VERDICT_FAIL
        assert verdict.unproven_hops == 1

    def test_the_unproven_count_is_carried_at_the_top_level(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1), _receipt(2), _receipt(3)])
        assert verdict.unproven_hops == 3
        assert verdict.verdict == VERDICT_UNPROVEN

    def test_a_late_widening_is_found_behind_an_early_unproven_hop(self):
        """Nothing short-circuits, so hop 3 is still compared."""
        verdict = grade_chain([_receipt(0), _receipt(1), _receipt(2, scope=NARROW), _receipt(3, scope=WIDER)])
        row = _rows(verdict)[3]
        assert row.verdict == VERDICT_FAIL
        assert REASON_AXIS_WIDENED in row.reasons
        assert verdict.verdict == VERDICT_FAIL

    def test_an_unverifiable_chain_fails_because_it_establishes_nothing(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1, scope=NARROW)], chain_ok=False)
        assert verdict.verdict == VERDICT_FAIL
        assert REASON_CHAIN_INVALID in verdict.reasons


# ---------------------------------------------------------------------------
# B: axis names come from narrowing_violations, not a second vocabulary
# ---------------------------------------------------------------------------


class TestAxisNames:
    def test_widened_axes_use_the_existing_stable_names(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1, scope=WIDER)])
        assert _rows(verdict)[1].axes == ("permissions",)

    def test_every_widened_axis_is_named_not_just_the_first(self):
        parent = DelegationScope(permissions=frozenset({"a"}), max_depth=1, max_uses=1)
        child = DelegationScope(permissions=frozenset({"a", "b"}), max_depth=9, max_uses=9)
        verdict = grade_chain([_receipt(0, scope=parent), _receipt(1, scope=child)])
        assert set(_rows(verdict)[1].axes) == {"max_depth", "max_uses", "permissions"}

    def test_a_scope_key_the_comparator_cannot_read_is_unproven(self):
        body = dict(WIDE.to_body())
        body["quota_bytes"] = 1024
        verdict = grade_chain([_receipt(0, scope_body=body, scope_ref=WIDE.scope_ref())])
        row = _rows(verdict)[0]
        assert row.verdict == VERDICT_UNPROVEN
        assert REASON_COMPARISON_AXIS_UNSUPPORTED in row.reasons
        assert "quota_bytes" in row.axes


# ---------------------------------------------------------------------------
# C: scope and scope_ref adjudication, both branches
# ---------------------------------------------------------------------------


class TestScopeRefAdjudication:
    def test_a_reference_contradicting_the_inline_scope_fails(self):
        verdict = grade_chain([_receipt(0, scope=WIDE, scope_ref="sha256:" + "0" * 64)])
        row = _rows(verdict)[0]
        assert row.verdict == VERDICT_FAIL
        assert REASON_SCOPE_REF_CONFLICT in row.reasons

    def test_a_resolver_that_disagrees_with_the_inline_scope_fails(self):
        verdict = grade_chain([_receipt(0, scope=WIDE)], scope_resolver=lambda _ref: NARROW)
        row = _rows(verdict)[0]
        assert row.verdict == VERDICT_FAIL
        assert REASON_SCOPE_REF_CONFLICT in row.reasons

    def test_a_reference_alone_with_no_resolver_is_unproven(self):
        verdict = grade_chain([_receipt(0, scope_ref=WIDE.scope_ref())])
        row = _rows(verdict)[0]
        assert row.verdict == VERDICT_UNPROVEN
        assert REASON_SCOPE_REF_ONLY_UNRESOLVED in row.reasons

    def test_an_unresolved_reference_beside_an_inline_scope_is_only_a_diagnostic(self):
        row = _rows(grade_chain([_receipt(0, scope=WIDE)]))[0]
        assert row.verdict == VERDICT_PASS
        assert row.diagnostics == (DIAGNOSTIC_SCOPE_REF_UNRESOLVED,)

    def test_a_resolver_that_cannot_resolve_leaves_the_inline_scope_governing(self):
        """The diagnostic branch, on a hop where a comparison actually happens."""
        verdict = grade_chain(
            [_receipt(0, scope=WIDE), _receipt(1, scope=NARROW)],
            scope_resolver=lambda _ref: None,
        )
        row = _rows(verdict)[1]
        assert row.verdict == VERDICT_PASS
        assert row.diagnostics == (DIAGNOSTIC_SCOPE_REF_UNRESOLVED,)
        assert verdict.verdict == VERDICT_PASS

    def test_an_unresolvable_reference_does_not_hide_a_widening(self):
        verdict = grade_chain(
            [_receipt(0, scope=WIDE), _receipt(1, scope=WIDER)],
            scope_resolver=lambda _ref: None,
        )
        row = _rows(verdict)[1]
        assert row.verdict == VERDICT_FAIL
        assert REASON_AXIS_WIDENED in row.reasons
        assert DIAGNOSTIC_SCOPE_REF_UNRESOLVED in row.diagnostics

    def test_a_resolved_reference_alone_records_that_it_was_resolved(self):
        verdict = grade_chain([_receipt(0, scope_ref=WIDE.scope_ref())], scope_resolver=lambda _ref: WIDE)
        row = _rows(verdict)[0]
        assert row.verdict == VERDICT_PASS
        assert DIAGNOSTIC_SCOPE_REF_ONLY_RESOLVED in row.diagnostics
        assert row.reasons == (REASON_ROOT_STRUCTURAL_ONLY,)


# ---------------------------------------------------------------------------
# E: subset of the nearest scoped ancestor across a gap
# ---------------------------------------------------------------------------


class TestNearestScopedAncestor:
    def test_widening_across_one_unscoped_gap_fails(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1), _receipt(2, scope=WIDER)])
        row = _rows(verdict)[2]
        assert row.verdict == VERDICT_FAIL
        assert REASON_AXIS_WIDENED_VS_ANCESTOR in row.reasons
        assert row.ancestor_hop_index == 0
        assert "permissions" in row.axes

    def test_a_direct_parent_widening_is_not_labelled_as_an_ancestor_one(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1, scope=WIDER)])
        row = _rows(verdict)[1]
        assert REASON_AXIS_WIDENED in row.reasons
        assert REASON_AXIS_WIDENED_VS_ANCESTOR not in row.reasons
        assert row.ancestor_hop_index is None

    def test_narrowing_across_one_unscoped_gap_passes(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1), _receipt(2, scope=NARROW)])
        assert _rows(verdict)[2].verdict == VERDICT_PASS
        assert verdict.verdict == VERDICT_UNPROVEN

    def test_no_scoped_ancestor_anywhere_is_unproven_not_fail(self):
        verdict = grade_chain([_receipt(0), _receipt(1, scope=NARROW)])
        row = _rows(verdict)[1]
        assert row.verdict == VERDICT_UNPROVEN
        assert REASON_PARENT_SCOPE_UNAVAILABLE in row.reasons

    def test_a_legacy_chain_never_triggers_it(self):
        verdict = grade_chain([_receipt(i) for i in range(3)])
        emitted = {reason for row in verdict.hops for reason in row.reasons}
        assert REASON_AXIS_WIDENED_VS_ANCESTOR not in emitted
        assert REASON_PARENT_SCOPE_UNAVAILABLE not in emitted


# ---------------------------------------------------------------------------
# H: the root
# ---------------------------------------------------------------------------


class TestRoot:
    def test_the_root_is_reported_as_root_not_as_a_narrowing_pass(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1, scope=NARROW)])
        row = _rows(verdict)[0]
        assert row.is_root is True
        assert row.axes == ()
        assert REASON_ROOT_STRUCTURAL_ONLY in row.reasons
        assert _rows(verdict)[1].is_root is False

    def test_a_single_scoped_root_composes_to_a_chain_pass(self):
        """A one-receipt chain contains no delegation, so there is no
        narrowing relation to evaluate. The chain passes on the vacuous
        reading of "every delegation narrows", and ``root_structural_only``
        keeps the structural nature of that pass machine-visible rather
        than letting it read as a checked narrowing."""
        verdict = grade_chain([_receipt(0, scope=WIDE)])
        assert verdict.verdict == VERDICT_PASS
        row = _rows(verdict)[0]
        assert row.is_root is True
        assert REASON_ROOT_STRUCTURAL_ONLY in row.reasons

    def test_an_unscoped_root_does_not_fail_for_that_alone(self):
        row = _rows(grade_chain([_receipt(0), _receipt(1, scope=NARROW)]))[0]
        assert row.verdict == VERDICT_UNPROVEN
        assert REASON_SCOPE_MISSING in row.reasons

    def test_children_of_an_unscoped_root_are_unproven(self):
        row = _rows(grade_chain([_receipt(0), _receipt(1, scope=NARROW)]))[1]
        assert row.verdict == VERDICT_UNPROVEN

    def test_a_hop_cannot_declare_itself_a_root_to_escape_its_ceiling(self):
        """``parent_ref`` is signed by the party being audited.

        ``_parent_index`` reports "no parent" for any hop naming the genesis
        anchor. Reading that as root would let a widening hop opt out of the
        comparison and take a pass row with it, which is the one positive claim
        this surface exists to make.
        """
        chain = [
            _receipt(0, scope=NARROW),
            _receipt(1, scope=WIDER, parent_ref=GENESIS),
        ]
        verdict = grade_chain(chain)
        row = _rows(verdict)[1]
        assert row.is_root is False
        assert row.verdict == VERDICT_UNPROVEN
        assert REASON_ROOT_CLAIMED_MID_CHAIN in row.reasons
        assert REASON_ROOT_STRUCTURAL_ONLY not in row.reasons
        assert verdict.verdict != VERDICT_PASS

    def test_only_the_first_receipt_is_ever_a_root(self):
        verdict = grade_chain([_receipt(i, scope=NARROW, parent_ref=GENESIS) for i in range(3)])
        assert [row.is_root for row in verdict.hops] == [True, False, False]

    def test_the_genesis_claim_cannot_make_the_chain_read_green(self):
        """A widened scope reached by a root claim still taints the chain.

        Hop 2 legitimately holds no more than the scope hop 1 recorded, so its
        own row passes. What must not happen is the chain reading pass: hop 1
        stays unproven and carries the summary with it.
        """
        verdict = grade_chain(
            [
                _receipt(0, scope=NARROW),
                _receipt(1, scope=WIDER, parent_ref=GENESIS),
                _receipt(2, scope=WIDER),
            ]
        )
        assert verdict.verdict == VERDICT_UNPROVEN
        assert verdict.unproven_hops == 1
        assert REASON_ROOT_CLAIMED_MID_CHAIN in _rows(verdict)[1].reasons


# ---------------------------------------------------------------------------
# F: the reason set is closed
# ---------------------------------------------------------------------------


class TestClosedReasonSet:
    def test_a_named_parent_absent_from_the_set_is_unproven(self):
        verdict = grade_chain([_receipt(0, scope=WIDE), _receipt(1, scope=NARROW, parent_ref="f" * 64)])
        row = _rows(verdict)[1]
        assert row.verdict == VERDICT_UNPROVEN
        assert REASON_PARENT_RECEIPT_UNAVAILABLE in row.reasons

    def test_nothing_outside_the_closed_set_is_ever_emitted(self):
        unreadable = dict(WIDE.to_body())
        unreadable["quota_bytes"] = 1
        verdict = grade_chain(
            [
                _receipt(0, scope=WIDE),
                _receipt(1),
                _receipt(2, scope=WIDER),
                _receipt(3, scope=NARROW, parent_ref="f" * 64),
                _receipt(4, scope_body=unreadable, scope_ref=WIDE.scope_ref()),
                _receipt(5, scope_ref=WIDE.scope_ref()),
            ]
        )
        for row in verdict.hops:
            assert set(row.reasons) <= VERDICT_REASONS
            assert set(row.diagnostics) <= VERDICT_DIAGNOSTICS
        assert set(verdict.reasons) <= VERDICT_REASONS

    def test_grade_chain_over_no_receipts_is_unproven(self):
        verdict = grade_chain([])
        assert verdict.verdict == VERDICT_UNPROVEN
        assert verdict.hops == ()
        assert REASON_NO_SCOPE_RECORDED in verdict.reasons


# ---------------------------------------------------------------------------
# G: the surface is additive
# ---------------------------------------------------------------------------


class TestAdditiveSurface:
    @pytest.fixture
    def widening(self, tmp_path):
        ledger = delegation.DelegationLedger(root=tmp_path, key=KEY)
        for i, scope in enumerate((WIDE, WIDER)):
            ledger.record_hop(
                run_id="widen",
                issuer=f"p{i}",
                subject=f"p{i}",
                audience=f"p{i + 1}",
                act="task.delegate",
                scope=scope,
            )
        return delegation.verify_run_chain(root=tmp_path, run_id="widen", key=KEY)

    def test_valid_is_unchanged_for_a_widening_chain(self, widening):
        assert widening.valid is False
        assert widening.authority.narrowing_ok is False
        assert widening.verdict.verdict == VERDICT_FAIL

    def test_the_authority_report_is_untouched_by_grading(self, tmp_path):
        ledger = delegation.DelegationLedger(root=tmp_path, key=KEY)
        for i in range(2):
            ledger.record_hop(
                run_id="plain",
                issuer=f"p{i}",
                subject=f"p{i}",
                audience=f"p{i + 1}",
                act="task.delegate",
            )
        result = delegation.verify_run_chain(root=tmp_path, run_id="plain", key=KEY)
        assert result.authority.violations == []
        assert result.authority.scope_coverage == "none"
        assert result.verdict.verdict == VERDICT_UNPROVEN

    def test_the_verdict_serializes(self):
        payload = grade_chain([_receipt(0, scope=WIDE), _receipt(1, scope=NARROW)]).to_dict()
        assert payload["verdict"] == VERDICT_PASS
        assert payload["unproven_hops"] == 0
        assert payload["hops"][0]["is_root"] is True
        assert payload["hops"][1]["parent_hop_index"] == 0
