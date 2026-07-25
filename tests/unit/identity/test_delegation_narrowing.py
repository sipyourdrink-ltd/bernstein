"""Recomputed authority narrowing over delegation receipts (issue #2554).

The checks under test only mean something if they can fail, so the adversarial
cases come first: a chain whose second hop *widens* its parent, and a chain in
which one principal both spawns work and approves it. Both are sealed with the
correct HMAC key - the party that writes a widened hop also holds the key that
seals it - so a check that only re-verified HMACs would pass every one of them.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.identity import delegation
from bernstein.core.identity.delegation_scope import (
    CHECK_BINDING,
    CHECK_DUTIES,
    CHECK_NARROWING,
    DUTY_APPROVE,
    DUTY_MERGE,
    DUTY_SPAWN,
    DecisionBinding,
    DelegationScope,
    check_duties,
    check_narrowing,
    duties_for_act,
    verify_authority,
)

KEY = b"k" * 32

PARENT_SCOPE = DelegationScope(
    permissions=frozenset({"files.read", "files.write", "tasks.claim"}),
    duties=frozenset({DUTY_SPAWN}),
    task_ids=frozenset({"t-1", "t-2"}),
    path_prefixes=frozenset({"/repo/src"}),
    not_after=2_000.0,
    max_uses=10,
    max_depth=3,
)


@pytest.fixture
def ledger(tmp_path):
    return delegation.DelegationLedger(root=tmp_path, key=KEY)


def _axes(result, check: str) -> set[str]:
    return {v.axis for v in result.violations if v.check == check}


# ---------------------------------------------------------------------------
# Adversarial: a widening hop must be rejected
# ---------------------------------------------------------------------------


class TestWideningIsRejected:
    @pytest.mark.parametrize(
        ("axis", "child_scope"),
        [
            (
                "permissions",
                DelegationScope(
                    permissions=frozenset({"files.read", "agents.spawn"}),
                    duties=frozenset({DUTY_SPAWN}),
                    task_ids=frozenset({"t-1"}),
                    path_prefixes=frozenset({"/repo/src"}),
                    not_after=1_000.0,
                    max_uses=2,
                    max_depth=1,
                ),
            ),
            (
                "duties",
                DelegationScope(
                    permissions=frozenset({"files.read"}),
                    duties=frozenset({DUTY_SPAWN, DUTY_APPROVE}),
                    task_ids=frozenset({"t-1"}),
                    path_prefixes=frozenset({"/repo/src"}),
                    not_after=1_000.0,
                    max_uses=2,
                    max_depth=1,
                ),
            ),
            (
                "task_ids",
                DelegationScope(
                    permissions=frozenset({"files.read"}),
                    duties=frozenset({DUTY_SPAWN}),
                    task_ids=frozenset({"t-1", "t-9"}),
                    path_prefixes=frozenset({"/repo/src"}),
                    not_after=1_000.0,
                    max_uses=2,
                    max_depth=1,
                ),
            ),
            (
                "path_prefixes",
                DelegationScope(
                    permissions=frozenset({"files.read"}),
                    duties=frozenset({DUTY_SPAWN}),
                    task_ids=frozenset({"t-1"}),
                    path_prefixes=frozenset({"/repo"}),
                    not_after=1_000.0,
                    max_uses=2,
                    max_depth=1,
                ),
            ),
            (
                "not_after",
                DelegationScope(
                    permissions=frozenset({"files.read"}),
                    duties=frozenset({DUTY_SPAWN}),
                    task_ids=frozenset({"t-1"}),
                    path_prefixes=frozenset({"/repo/src"}),
                    not_after=9_999.0,
                    max_uses=2,
                    max_depth=1,
                ),
            ),
            (
                "max_uses",
                DelegationScope(
                    permissions=frozenset({"files.read"}),
                    duties=frozenset({DUTY_SPAWN}),
                    task_ids=frozenset({"t-1"}),
                    path_prefixes=frozenset({"/repo/src"}),
                    not_after=1_000.0,
                    max_uses=99,
                    max_depth=1,
                ),
            ),
            (
                "max_depth",
                DelegationScope(
                    permissions=frozenset({"files.read"}),
                    duties=frozenset({DUTY_SPAWN}),
                    task_ids=frozenset({"t-1"}),
                    path_prefixes=frozenset({"/repo/src"}),
                    not_after=1_000.0,
                    max_uses=2,
                    max_depth=7,
                ),
            ),
        ],
    )
    def test_widening_hop_fails_verification_naming_the_axis(self, ledger, axis, child_scope):
        """A child that gains authority its parent lacked fails, axis named."""
        ledger.record_hop(
            run_id="run-widen",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
        )
        ledger.record_hop(
            run_id="run-widen",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
            scope=child_scope,
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-widen", key=KEY)

        # The HMAC chain itself is perfectly intact - the widening writer held
        # the key. Only the recomputed subset relation catches it.
        assert result.chain_ok is True
        assert result.valid is False
        assert result.authority.narrowing_ok is False
        assert axis in _axes(result, CHECK_NARROWING)
        offending = next(v for v in result.violations if v.axis == axis)
        assert offending.hop_index == 1
        assert offending.parent_hop_index == 0

    def test_dropping_a_bound_the_parent_imposed_is_widening(self, ledger):
        """Omitting a constraint is not narrowing; ``None`` is the widest value."""
        ledger.record_hop(
            run_id="run-drop",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
        )
        ledger.record_hop(
            run_id="run-drop",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
            scope=DelegationScope(permissions=frozenset({"files.read"}), duties=frozenset({DUTY_SPAWN})),
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-drop", key=KEY)

        assert result.valid is False
        assert _axes(result, CHECK_NARROWING) == {"task_ids", "path_prefixes", "not_after", "max_uses", "max_depth"}

    def test_widening_survives_a_rewritten_and_resealed_chain(self, ledger, tmp_path):
        """Re-sealing a widened hop with the real key does not launder it.

        The strongest form of the attack: an insider edits a recorded scope to
        grant itself a permission its parent never held, then recomputes the
        HMAC so the file is internally consistent. Linkage and HMAC both pass;
        the recomputed subset relation is what refuses.
        """
        ledger.record_hop(
            run_id="run-reseal",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
        )
        ledger.record_hop(
            run_id="run-reseal",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
            scope=DelegationScope(
                permissions=frozenset({"files.read"}),
                duties=frozenset({DUTY_SPAWN}),
                task_ids=frozenset({"t-1"}),
                path_prefixes=frozenset({"/repo/src"}),
                not_after=1_000.0,
                max_uses=2,
                max_depth=1,
            ),
        )

        path = ledger.receipt_path("run-reseal")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        forged = DelegationScope(
            permissions=frozenset({"files.read", "gate.approve"}),
            duties=frozenset({DUTY_SPAWN}),
            task_ids=frozenset({"t-1"}),
            path_prefixes=frozenset({"/repo/src"}),
            not_after=1_000.0,
            max_uses=2,
            max_depth=1,
        )
        rows[1]["scope"] = forged.to_body()
        rows[1]["scope_ref"] = forged.scope_ref()
        body = {k: v for k, v in rows[1].items() if k != "hmac"}
        rows[1]["hmac"] = delegation._compute_hmac(KEY, rows[1]["prev_hmac"], body)
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-reseal", key=KEY)

        assert result.chain_ok is True, "the forged chain is HMAC-consistent by construction"
        assert result.valid is False
        assert "permissions" in _axes(result, CHECK_NARROWING)

    def test_scope_ref_that_disagrees_with_the_inline_scope_is_caught(self, ledger):
        """A content address must address the body it travels with."""
        ledger.record_hop(
            run_id="run-ref",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
        )
        path = ledger.receipt_path("run-ref")
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        row["scope_ref"] = "sha256:" + "0" * 64
        body = {k: v for k, v in row.items() if k != "hmac"}
        row["hmac"] = delegation._compute_hmac(KEY, row["prev_hmac"], body)
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-ref", key=KEY)

        assert result.valid is False
        assert "scope_ref_mismatch" in _axes(result, CHECK_NARROWING)

    def test_scoped_hop_under_an_unscoped_parent_is_unprovable_not_assumed_ok(self, ledger):
        """A missing parent ceiling makes narrowing unprovable, so it fails."""
        ledger.record_hop(
            run_id="run-mixed",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
        )
        ledger.record_hop(
            run_id="run-mixed",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
            scope=PARENT_SCOPE,
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-mixed", key=KEY)

        assert result.valid is False
        assert "unscoped_parent" in _axes(result, CHECK_NARROWING)

    def test_parent_ref_naming_no_preceding_hop_is_caught(self, ledger):
        """A dangling parent reference cannot be resolved, so it cannot pass."""
        ledger.record_hop(
            run_id="run-dangle",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
            parent_ref="f" * 64,
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-dangle", key=KEY)

        assert result.valid is False
        assert "unresolved_parent" in _axes(result, CHECK_NARROWING)

    def test_unresolvable_scope_reference_is_caught(self, ledger):
        """A by-reference scope with no resolver cannot be recomputed."""
        ledger.record_hop(
            run_id="run-byref",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
            inline_scope=False,
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-byref", key=KEY)

        assert result.valid is False
        assert "unresolved_scope" in _axes(result, CHECK_NARROWING)


# ---------------------------------------------------------------------------
# Adversarial: separation of duties is a different question from narrowing
# ---------------------------------------------------------------------------


class TestSeparationOfDuties:
    def test_one_principal_spawning_and_approving_is_caught(self, ledger):
        """The worker that asked for the work does not get to bless it."""
        ledger.record_hop(
            run_id="run-sod",
            issuer="principal:alex",
            subject="worker:7",
            audience="sub-agent:backend",
            act="task.spawn",
        )
        ledger.record_hop(
            run_id="run-sod",
            issuer="principal:alex",
            subject="worker:7",
            audience="gate:merge",
            act="gate.approve",
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-sod", key=KEY)

        assert result.chain_ok is True
        assert result.valid is False
        assert result.authority.duties_ok is False
        violation = next(v for v in result.violations if v.check == CHECK_DUTIES)
        assert violation.axis == "approve|spawn"
        assert violation.principal == "worker:7"
        assert violation.parent_hop_index == 0

    def test_separated_duties_across_two_principals_pass(self, ledger):
        """The same two powers, held by different principals, are fine."""
        ledger.record_hop(
            run_id="run-sod-ok",
            issuer="principal:alex",
            subject="worker:7",
            audience="sub-agent:backend",
            act="task.spawn",
        )
        ledger.record_hop(
            run_id="run-sod-ok",
            issuer="principal:alex",
            subject="reviewer:9",
            audience="gate:merge",
            act="gate.approve",
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-sod-ok", key=KEY)

        assert result.valid is True
        assert result.authority.duties_ok is True

    def test_narrowing_can_pass_while_duties_fail(self, ledger):
        """The two checks are distinct: a perfectly narrowing chain can still
        concentrate incompatible powers in one principal."""
        root_scope = DelegationScope(
            permissions=frozenset({"tasks.write"}),
            duties=frozenset({DUTY_SPAWN, DUTY_APPROVE}),
            max_depth=5,
        )
        ledger.record_hop(
            run_id="run-distinct",
            issuer="principal:alex",
            subject="worker:7",
            audience="sub-agent:backend",
            act="task.spawn",
            scope=root_scope,
        )
        ledger.record_hop(
            run_id="run-distinct",
            issuer="worker:7",
            subject="worker:7",
            audience="gate:merge",
            act="gate.approve",
            scope=DelegationScope(
                permissions=frozenset({"tasks.write"}),
                duties=frozenset({DUTY_APPROVE}),
                max_depth=4,
            ),
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-distinct", key=KEY)

        assert result.authority.narrowing_ok is True, "every hop is a strict subset of its parent"
        assert result.authority.duties_ok is False, "but one principal holds spawn and approve"
        assert result.valid is False

    def test_duties_come_from_the_recorded_scope_when_present(self):
        """An explicit scope beats act-name inference."""
        receipts = [
            _receipt(0, subject="worker:7", act="opaque.step", scope=DelegationScope(duties=frozenset({DUTY_SPAWN}))),
            _receipt(1, subject="worker:7", act="opaque.step", scope=DelegationScope(duties=frozenset({DUTY_MERGE}))),
        ]
        violations = check_duties(receipts)
        assert [v.axis for v in violations] == ["merge|spawn"]

    def test_act_inference_matches_whole_segments_only(self):
        """``spawner.status`` must not be read as the spawn duty."""
        assert duties_for_act("task.spawn") == frozenset({DUTY_SPAWN})
        assert duties_for_act("gate.approve") == frozenset({DUTY_APPROVE})
        assert duties_for_act("pr_merge") == frozenset({DUTY_MERGE})
        assert duties_for_act("spawner.status") == frozenset()
        assert duties_for_act("approver_pool.read") == frozenset()

    def test_custom_separated_pairs_are_honoured(self):
        """The rule set is an argument, not a hard-coded policy."""
        receipts = [
            _receipt(0, subject="worker:7", act="task.spawn"),
            _receipt(1, subject="worker:7", act="pr.merge"),
        ]
        assert check_duties(receipts) != []
        assert check_duties(receipts, separated=[{DUTY_APPROVE, DUTY_MERGE}]) == []


# ---------------------------------------------------------------------------
# Adversarial: decision-time version binding
# ---------------------------------------------------------------------------


class TestDecisionBinding:
    def test_gated_decision_without_a_binding_is_caught(self, ledger):
        """An approval that cites no charter version floats free of it."""
        ledger.record_hop(
            run_id="run-bind",
            issuer="principal:alex",
            subject="orchestrator",
            audience="sub-agent:backend",
            act="task.spawn",
            binding=DecisionBinding(charter_hash="sha256:aaa", certificate_version="3"),
        )
        ledger.record_hop(
            run_id="run-bind",
            issuer="orchestrator",
            subject="reviewer:9",
            audience="gate:merge",
            act="gate.approve",
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-bind", key=KEY)

        assert result.chain_ok is True
        assert result.valid is False
        assert result.authority.binding_ok is False
        assert "binding_missing" in _axes(result, CHECK_BINDING)

    def test_a_hop_citing_a_different_charter_version_than_its_parent_is_caught(self, ledger):
        """One authority chain is evaluated under one charter version."""
        ledger.record_hop(
            run_id="run-drift",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            binding=DecisionBinding(charter_hash="sha256:aaa", certificate_version="3"),
        )
        ledger.record_hop(
            run_id="run-drift",
            issuer="orchestrator",
            subject="reviewer:9",
            audience="gate:merge",
            act="gate.approve",
            binding=DecisionBinding(charter_hash="sha256:bbb", certificate_version="4"),
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-drift", key=KEY)

        assert result.valid is False
        assert "binding_drift" in _axes(result, CHECK_BINDING)

    def test_a_consistently_bound_chain_passes(self, ledger):
        binding = DecisionBinding(
            charter_hash="sha256:aaa",
            certificate_hash="sha256:ccc",
            certificate_version="3",
        )
        ledger.record_hop(
            run_id="run-bound",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            binding=binding,
        )
        ledger.record_hop(
            run_id="run-bound",
            issuer="orchestrator",
            subject="reviewer:9",
            audience="gate:merge",
            act="gate.approve",
            binding=binding,
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-bound", key=KEY)

        assert result.valid is True
        assert result.authority.binding_ok is True
        assert result.receipts[1].binding == {
            "certificate_hash": "sha256:ccc",
            "certificate_version": "3",
            "charter_hash": "sha256:aaa",
        }

    def test_binding_is_covered_by_the_receipt_hmac(self, ledger):
        """Rewriting a recorded charter version breaks tamper evidence."""
        ledger.record_hop(
            run_id="run-hmac",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            binding=DecisionBinding(charter_hash="sha256:aaa", certificate_version="3"),
        )
        path = ledger.receipt_path("run-hmac")
        path.write_text(
            path.read_text(encoding="utf-8").replace('"certificate_version": "3"', '"certificate_version": "4"'),
            encoding="utf-8",
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-hmac", key=KEY)

        assert result.chain_ok is False
        assert any("HMAC mismatch" in e for e in result.errors)


# ---------------------------------------------------------------------------
# The happy path, and the promise made to receipts written before all this
# ---------------------------------------------------------------------------


class TestNarrowingChainsPass:
    def test_a_strictly_narrowing_chain_verifies(self, ledger):
        ledger.record_hop(
            run_id="run-ok",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
        )
        ledger.record_hop(
            run_id="run-ok",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
            scope=DelegationScope(
                permissions=frozenset({"files.read"}),
                duties=frozenset({DUTY_SPAWN}),
                task_ids=frozenset({"t-1"}),
                path_prefixes=frozenset({"/repo/src/api"}),
                not_after=1_500.0,
                max_uses=3,
                max_depth=2,
            ),
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-ok", key=KEY)

        assert result.valid is True
        assert result.authority.ok is True
        assert result.authority.scope_coverage == "full"
        assert result.violations == []

    def test_scope_reference_resolves_through_a_resolver(self, ledger):
        """A receipt may carry the reference alone when the body is fetchable."""
        child = DelegationScope(
            permissions=frozenset({"files.read"}),
            duties=frozenset({DUTY_SPAWN}),
            task_ids=frozenset({"t-1"}),
            path_prefixes=frozenset({"/repo/src"}),
            not_after=1_000.0,
            max_uses=2,
            max_depth=1,
        )
        store = {PARENT_SCOPE.scope_ref(): PARENT_SCOPE, child.scope_ref(): child}
        ledger.record_hop(
            run_id="run-res",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
            inline_scope=False,
        )
        ledger.record_hop(
            run_id="run-res",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
            scope=child,
            inline_scope=False,
        )

        result = delegation.verify_run_chain(
            root=ledger.root,
            run_id="run-res",
            key=KEY,
            scope_resolver=store.get,
        )

        assert result.valid is True
        assert result.authority.scope_coverage == "full"

    def test_scope_ref_is_a_pure_function_of_the_scope(self):
        """Two operators describing the same authority mint the same reference."""
        a = DelegationScope(permissions=frozenset({"b", "a"}), task_ids=frozenset({"t-2", "t-1"}))
        b = DelegationScope(permissions=frozenset({"a", "b"}), task_ids=frozenset({"t-1", "t-2"}))
        assert a.scope_ref() == b.scope_ref()
        assert a.scope_ref().startswith("sha256:")
        assert DelegationScope.from_body(a.to_body()) == a


class TestLegacyReceiptsAreUnaffected:
    def test_receipts_without_scope_fields_still_validate(self, ledger):
        """The promise to chains written before narrowing existed."""
        ledger.record_hop(
            run_id="run-legacy",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
        )
        ledger.record_hop(
            run_id="run-legacy",
            issuer="orchestrator",
            subject="sub-agent:backend",
            audience="sub-agent:backend",
            act="task.delegate",
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-legacy", key=KEY)

        assert result.valid is True
        assert result.chain_ok is True
        assert result.authority.scope_coverage == "none"
        assert result.violations == []

    def test_unscoped_receipt_bytes_are_unchanged_by_the_new_fields(self, ledger):
        """No optional key is emitted, so old and new writers hash alike."""
        receipt = ledger.record_hop(
            run_id="run-bytes",
            issuer="principal:alex",
            subject="orchestrator",
            audience="sub-agent:backend",
            act="task.spawn",
            created=1_730_000_000,
        )
        row = json.loads(ledger.receipt_path("run-bytes").read_text(encoding="utf-8").splitlines()[0])
        assert set(row) == {
            "act",
            "audience",
            "created",
            "hmac",
            "hop_index",
            "issuer",
            "prev_hmac",
            "run_id",
            "subject",
        }
        expected = delegation._compute_hmac(
            KEY,
            delegation.GENESIS_HMAC,
            {
                "act": "task.spawn",
                "audience": "sub-agent:backend",
                "created": 1_730_000_000,
                "hop_index": 0,
                "issuer": "principal:alex",
                "prev_hmac": delegation.GENESIS_HMAC,
                "run_id": "run-bytes",
                "subject": "orchestrator",
            },
        )
        assert receipt.hmac == expected
        assert receipt.body() == {k: v for k, v in row.items() if k != "hmac"}

    def test_an_empty_chain_reports_no_authority_violations(self):
        report = verify_authority([])
        assert report.ok is True
        assert report.scope_coverage == "none"


class TestParentReferenceForms:
    def test_a_tree_shaped_run_narrows_against_the_named_parent(self, ledger):
        """Two siblings both narrow hop 0, not the line above them."""
        root_receipt = ledger.record_hop(
            run_id="run-tree",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
            scope=PARENT_SCOPE,
        )
        narrow = DelegationScope(
            permissions=frozenset({"files.read"}),
            duties=frozenset({DUTY_SPAWN}),
            task_ids=frozenset({"t-1"}),
            path_prefixes=frozenset({"/repo/src"}),
            not_after=1_000.0,
            max_uses=1,
            max_depth=1,
        )
        ledger.record_hop(
            run_id="run-tree",
            issuer="orchestrator",
            subject="sub-agent:a",
            audience="sub-agent:a",
            act="task.delegate",
            scope=narrow,
            parent_ref=root_receipt.hmac,
        )
        # A sibling: its parent is the root, not the hop recorded just before it.
        # Narrowing against the sibling would fail on max_uses; against the root
        # it passes, which is the point of naming the parent explicitly.
        ledger.record_hop(
            run_id="run-tree",
            issuer="orchestrator",
            subject="sub-agent:b",
            audience="sub-agent:b",
            act="task.delegate",
            scope=DelegationScope(
                permissions=frozenset({"files.write"}),
                duties=frozenset({DUTY_SPAWN}),
                task_ids=frozenset({"t-2"}),
                path_prefixes=frozenset({"/repo/src"}),
                not_after=1_200.0,
                max_uses=4,
                max_depth=2,
            ),
            parent_ref=root_receipt.hmac,
        )

        result = delegation.verify_run_chain(root=ledger.root, run_id="run-tree", key=KEY)

        assert result.valid is True, [str(v) for v in result.violations]

    def test_check_narrowing_returns_coverage_alongside_violations(self):
        receipts = [
            _receipt(0, subject="a", act="run.authorize", scope=PARENT_SCOPE),
            _receipt(1, subject="b", act="task.delegate"),
        ]
        violations, coverage = check_narrowing(receipts)
        assert coverage == "partial"
        assert violations == []


def _receipt(
    hop_index: int,
    *,
    subject: str,
    act: str,
    scope: DelegationScope | None = None,
) -> delegation.DelegationReceipt:
    """Build an in-memory receipt (no ledger, no HMAC) for pure-check tests."""
    return delegation.DelegationReceipt(
        run_id="run",
        hop_index=hop_index,
        issuer="principal",
        subject=subject,
        audience="audience",
        act=act,
        created=0,
        hmac=f"h{hop_index}",
        scope_ref=scope.scope_ref() if scope is not None else None,
        scope=scope.to_body() if scope is not None else None,
    )
