"""The ``allowed_files`` axis on ``DelegationScope`` (#5351, graded since #5418).

``allowed_files`` is a glob field: a pattern is not a path prefix, so it could
not be decided by the ancestry primitive ``path_prefixes`` uses, and
translating one into the other would report "narrowing checked and held" for
an axis where only the patterns that happened to have a prefix form were
checked. Before #5418 the axis was therefore recorded verbatim on the receipt
but never compared, and graded ``comparison_axis_unsupported`` whenever a
hop's own body carried the key. #5418 added
:func:`~bernstein.core.security.capability_tokens.glob_narrows`, a
subsumption primitive over the pattern grammar in
:mod:`bernstein.core.path_scope` (not the ancestry one), so the axis is now
graded like every other axis: ``pass`` when it narrows, ``axis_widened`` when
it does not.

The compatibility group pins the SERIALIZED BODY against the implementation
exactly as it stood before #5418 -- that part is unaffected by this change.
``scope_ref()`` digests the canonical bytes of ``to_body()``, so emitting a
new key unconditionally would move every stored reference and stop sealed
records from replaying byte-identically. The literals below were computed on
the pre-#5418 class and remain the oracle for the *serialization*: a scope
that does not use the axis must still produce these exact bytes and this
exact digest, whatever the *grading* rules do.

The presence matrix records what the module produces for the three ways two
hops can carry the axis, now that the axis is actually compared.
"""

from __future__ import annotations

from dataclasses import replace

from bernstein.core.identity import delegation
from bernstein.core.identity.delegation_scope import (
    REASON_AXIS_WIDENED,
    REASON_ROOT_STRUCTURAL_ONLY,
    VERDICT_FAIL,
    VERDICT_PASS,
    ChainVerdict,
    DelegationScope,
    HopVerdict,
    grade_chain,
)
from bernstein.core.security.agent_card_signer import canonicalize_jcs

#: A scope exercising every axis that existed before ``allowed_files`` did.
REPRESENTATIVE = DelegationScope(
    permissions=frozenset({"repo.read"}),
    duties=frozenset({"spawn"}),
    task_ids=frozenset({"t1"}),
    path_prefixes=frozenset({"src"}),
    not_after=1.0,
    max_uses=2,
    max_depth=3,
)

#: ``REPRESENTATIVE.to_body()`` on the pre-change class.
PRE_CHANGE_BODY = {
    "duties": ["spawn"],
    "max_depth": 3,
    "max_uses": 2,
    "not_after": 1.0,
    "path_prefixes": ["src"],
    "permissions": ["repo.read"],
    "task_ids": ["t1"],
}

#: The canonical bytes ``scope_ref()`` hashes, on the pre-change class.
#:
#: Frozen under ``JCS_CANONICALIZATION_VERSION`` 3. The literal moved once, when
#: #5494 made integral floats follow RFC 8785 3.2.2.3 (`not_after` serialises as
#: ``1``, not ``1.0``); that bump is versioned and deliberate. If these two
#: literals fail, decide which happened before touching them: a member was added
#: to the body, which is the thing this group exists to catch, or the number
#: grammar moved again, which shows up only in a payload carrying a float.
PRE_CHANGE_JCS = (
    b'{"duties":["spawn"],"max_depth":3,"max_uses":2,"not_after":1,'
    b'"path_prefixes":["src"],"permissions":["repo.read"],"task_ids":["t1"]}'
)

#: ``REPRESENTATIVE.scope_ref()`` on the pre-change class.
PRE_CHANGE_REF = "sha256:82809a0d877bc91da391c41b349a8a5ec405fdb1bc92613adf5a4654b1af6d58"

#: ``DelegationScope().scope_ref()`` on the pre-change class.
PRE_CHANGE_EMPTY_REF = "sha256:0215f133c471eb217982555a27f131a3d94f1b3764ee4679a79c5a2021e875b1"


def _receipt(
    index: int,
    *,
    scope: DelegationScope | None = None,
) -> delegation.DelegationReceipt:
    """Build one receipt directly; the reference is the body's own address."""
    return delegation.DelegationReceipt(
        run_id="run",
        hop_index=index,
        issuer=f"p{index}",
        subject=f"p{index}",
        audience=f"p{index + 1}",
        act="task.delegate",
        created=1_700_000_000 + index,
        hmac=f"{index + 1:064x}",
        scope_ref=None if scope is None else scope.scope_ref(),
        scope=None if scope is None else scope.to_body(),
    )


def _rows(verdict: ChainVerdict) -> dict[int, HopVerdict]:
    return {row.hop_index: row for row in verdict.hops}


CEILING_WITH = DelegationScope(task_ids=frozenset({"t1", "t2"}), allowed_files=frozenset({"src/**"}))
CEILING_WITHOUT = DelegationScope(task_ids=frozenset({"t1", "t2"}))
CHILD_WITH = DelegationScope(task_ids=frozenset({"t1"}), allowed_files=frozenset({"src/core"}))
CHILD_WITHOUT = DelegationScope(task_ids=frozenset({"t1"}))


class TestAScopeThatDoesNotUseTheAxisHashesUnchanged:
    """The trap: a new key that is always emitted re-addresses every scope."""

    def test_the_body_carries_no_allowed_files_key(self):
        assert "allowed_files" not in REPRESENTATIVE.to_body()
        assert REPRESENTATIVE.to_body() == PRE_CHANGE_BODY

    def test_the_canonical_bytes_are_byte_identical(self):
        assert canonicalize_jcs(REPRESENTATIVE.to_body()) == PRE_CHANGE_JCS

    def test_the_reference_is_the_literal_pre_change_digest(self):
        assert REPRESENTATIVE.scope_ref() == PRE_CHANGE_REF

    def test_the_default_scope_reference_is_unchanged_too(self):
        assert "allowed_files" not in DelegationScope().to_body()
        assert DelegationScope().scope_ref() == PRE_CHANGE_EMPTY_REF

    def test_using_the_axis_is_serialized_and_moves_the_reference(self):
        used = replace(REPRESENTATIVE, allowed_files=frozenset({"x"}))
        assert used.to_body()["allowed_files"] == ["x"]
        assert used.scope_ref() != PRE_CHANGE_REF


class TestTheAxisSurvivesTheRoundTrip:
    """``from_body`` is an explicit constructor: an unlisted key is dropped."""

    def test_a_present_value_comes_back(self):
        scope = replace(REPRESENTATIVE, allowed_files=frozenset({"src/**", "docs/*.md"}))
        assert DelegationScope.from_body(scope.to_body()).allowed_files == scope.allowed_files
        assert DelegationScope.from_body(scope.to_body()) == scope

    def test_an_absent_key_reads_as_the_widest_value(self):
        assert DelegationScope.from_body(REPRESENTATIVE.to_body()).allowed_files is None

    def test_the_round_trip_reproduces_the_reference(self):
        """What a dropped key would cost: the grader re-derives the body's address.

        A recorded reference that is not the content address of the inline scope
        is read as one signed body contradicting itself, which fails the hop -
        so a receipt recording this axis would grade ``fail`` rather than
        ``pass`` if the constructor could not read the key back. A lone root
        hop has nothing to narrow against, so it reads pass (structural only),
        not unproven -- ``allowed_files`` is a recognized, gradable axis now.
        """
        scope = replace(REPRESENTATIVE, allowed_files=frozenset({"src/**"}))
        assert DelegationScope.from_body(scope.to_body()).scope_ref() == scope.scope_ref()
        assert grade_chain([_receipt(0, scope=scope)]).hops[0].verdict == VERDICT_PASS


class TestPresenceMatrix:
    """What the module produces when one, both, or neither hop records the axis.

    ``CHILD_WITH``'s ``src/core`` narrows correctly under ``CEILING_WITH``'s
    ``src/**`` (``pattern_subsumes("src/**", "src/core")`` holds), so the
    narrowing cases below read pass. The one genuine widening -- a child that
    drops the file restriction its parent imposed -- is the case #5418 exists
    to catch, and now does.
    """

    def test_both_sides_record_it(self):
        """A child pattern that narrows under the ceiling's grades pass on the axis."""
        verdict = grade_chain([_receipt(0, scope=CEILING_WITH), _receipt(1, scope=CHILD_WITH)])
        rows = _rows(verdict)
        assert verdict.verdict == VERDICT_PASS
        assert verdict.unproven_hops == 0
        assert rows[0].verdict == VERDICT_PASS
        assert rows[0].axes == ()
        assert rows[0].reasons == (REASON_ROOT_STRUCTURAL_ONLY,)
        assert rows[1].verdict == VERDICT_PASS
        assert rows[1].axes == ()
        assert rows[1].reasons == ()

    def test_the_child_records_it_and_the_ceiling_does_not(self):
        """A ceiling that never restricted files subsumes anything the child records."""
        verdict = grade_chain([_receipt(0, scope=CEILING_WITHOUT), _receipt(1, scope=CHILD_WITH)])
        rows = _rows(verdict)
        assert verdict.verdict == VERDICT_PASS
        assert verdict.unproven_hops == 0
        assert rows[0].verdict == VERDICT_PASS
        assert rows[0].axes == ()
        assert rows[1].verdict == VERDICT_PASS
        assert rows[1].axes == ()
        assert rows[1].reasons == ()

    def test_the_ceiling_records_it_and_the_child_does_not(self):
        """The genuine widening: a child that drops the parent's file restriction fails.

        Before #5418 this axis was never compared at all, so this exact
        widening -- a sub-agent credential that names no file scope where its
        parent named one -- read as a clean pass. That is the defect #5418
        closes: it is not merely "unproven, say so"; it is a widening that
        went completely uncaught.
        """
        verdict = grade_chain([_receipt(0, scope=CEILING_WITH), _receipt(1, scope=CHILD_WITHOUT)])
        rows = _rows(verdict)
        assert verdict.verdict == VERDICT_FAIL
        assert verdict.unproven_hops == 0
        assert rows[0].verdict == VERDICT_PASS
        assert rows[0].axes == ()
        assert rows[1].verdict == VERDICT_FAIL
        assert rows[1].axes == ("allowed_files",)
        assert rows[1].reasons == (REASON_AXIS_WIDENED,)
