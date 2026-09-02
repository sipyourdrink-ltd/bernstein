"""Tests for remediation collection over govern findings (issue #5079).

A failed finding should produce something an operator can execute, not a
sentence. These tests pin the four properties the collection must hold:

- the declared remedy lives in the playbook schema and is part of its digest;
- a finding whose clause declares no remedy is reported as such, never dropped;
- the collected artifact is an unsigned draft that cannot apply itself;
- the collection is a deterministic projection of the plan and the playbook.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import governance_group
from bernstein.core.govern import compute_plan
from bernstein.core.govern.playbook_models import Playbook, PlaybookClause, RemediationAction
from bernstein.core.govern.proposal import ProposalStatus
from bernstein.core.govern.remediation import (
    RemediationProposal,
    RemediationStep,
    collect_remediation,
)

_PLAYBOOK: dict[str, object] = {
    "forbidden": [
        {
            "surface": "arn:aws:s3:::public-bucket",
            "clause": "3.2: no public buckets",
            "remediation_plan": [
                {"action": "set", "target": "arn:aws:s3:::public-bucket/acl", "value": "private"},
                {"action": "remove", "target": "arn:aws:s3:::public-bucket/policy"},
            ],
        }
    ],
    "permitted": [
        {
            "surface": "principal/agent-a",
            "clause": "4.1: capability ceiling",
            "declared_ceiling": "3",
        }
    ],
    "required": [
        {
            "surface": "lineage/trace-export",
            "clause": "5.0: trace export is required",
            "declared_value": "enabled",
            "remediation_plan": [
                {"action": "set", "target": "lineage/trace-export", "value": "enabled"},
            ],
        }
    ],
}

_INVENTORY: dict[str, object] = {
    "surfaces": [
        {
            "surface": "arn:aws:s3:::public-bucket",
            "observed_value": "public-read",
            "evidence_ref": "query-1",
        },
        {
            "surface": "principal/agent-a",
            "observed_value": "7",
            "evidence_ref": "query-2",
        },
    ]
}


def _plan(timestamp: int = 1_700_000_000):
    return compute_plan(
        playbook=_PLAYBOOK,
        inventory=_INVENTORY,
        run_id="govern-plan",
        timestamp=timestamp,
    )


def _proposal(timestamp: int = 1_700_000_000) -> RemediationProposal:
    return collect_remediation(
        plan=_plan(timestamp),
        playbook=_PLAYBOOK,
        timestamp=timestamp,
    )


class TestPlaybookSchema:
    """The declared remedy is a field of the playbook, not an annotation beside it."""

    def test_clause_without_remediation_plan_reports_absence_explicitly(self) -> None:
        """Test 1: the field is optional, and its absence is a readable None."""
        clause = PlaybookClause(surface="s", clause="c", kind="forbidden")

        assert clause.remediation_plan is None
        assert "remediation_plan" not in clause.to_dict()
        assert PlaybookClause.from_dict(clause.to_dict()).remediation_plan is None

    def test_remediation_plan_round_trips_through_the_playbook_schema(self) -> None:
        """Test 2: an ordered change set survives serialization in declared order."""
        actions = (
            RemediationAction(action="set", target="a", value="1"),
            RemediationAction(action="remove", target="b"),
        )
        clause = PlaybookClause(
            surface="s",
            clause="c",
            kind="forbidden",
            remediation_plan=actions,
        )

        rebuilt = PlaybookClause.from_dict(clause.to_dict())

        assert rebuilt.remediation_plan == actions
        assert clause.to_dict()["remediation_plan"] == [
            {"action": "set", "target": "a", "value": "1"},
            {"action": "remove", "target": "b"},
        ]

    def test_remediation_plan_changes_the_playbook_content_hash(self) -> None:
        """Test 3: swapping the declared remedy cannot hide behind an unchanged digest."""
        bare = Playbook(clauses=(PlaybookClause(surface="s", clause="c", kind="forbidden"),))
        remedied = Playbook(
            clauses=(
                PlaybookClause(
                    surface="s",
                    clause="c",
                    kind="forbidden",
                    remediation_plan=(RemediationAction(action="set", target="s", value="private"),),
                ),
            )
        )

        assert bare.content_hash() != remedied.content_hash()


class TestCollection:
    """Collecting the declared remedies for one plan into one proposal."""

    def test_findings_without_a_declared_plan_are_listed_as_unremediated(self) -> None:
        """Test 4: the ceiling breach has no remedy, and the proposal says so."""
        proposal = _proposal()

        unremediated = {u.surface for u in proposal.unremediated}
        assert unremediated == {"principal/agent-a"}
        assert proposal.unremediated[0].finding_kind == "wider_ceiling"
        assert proposal.unremediated[0].reason == "no remediation_plan declared"

    def test_collected_proposal_covers_every_finding_exactly_once(self) -> None:
        """Test 5: no finding falls between the remedied and the unremediated set."""
        plan = _plan()
        proposal = collect_remediation(plan=plan, playbook=_PLAYBOOK, timestamp=1_700_000_000)

        findings = {(e.surface, e.kind.value) for e in plan.entries}
        remedied = {(s.surface, s.finding_kind) for s in proposal.steps}
        unremediated = {(u.surface, u.finding_kind) for u in proposal.unremediated}

        assert remedied | unremediated == findings
        assert remedied & unremediated == set()

    def test_every_step_carries_the_finding_and_clause_that_produced_it(self) -> None:
        """Test 6: a step is traceable back to the finding it answers."""
        proposal = _proposal()

        bucket_steps = [s for s in proposal.steps if s.surface == "arn:aws:s3:::public-bucket"]
        assert [(s.action, s.target, s.value) for s in bucket_steps] == [
            ("set", "arn:aws:s3:::public-bucket/acl", "private"),
            ("remove", "arn:aws:s3:::public-bucket/policy", None),
        ]
        assert {s.playbook_clause for s in bucket_steps} == {"3.2: no public buckets"}
        assert {s.finding_kind for s in bucket_steps} == {"forbidden"}

    def test_a_malformed_remediation_plan_is_rejected_rather_than_ignored(self) -> None:
        """Test 7: an unparseable remedy is a failed collection, not an empty one."""
        playbook = {
            "forbidden": [
                {
                    "surface": "arn:aws:s3:::public-bucket",
                    "clause": "3.2: no public buckets",
                    "remediation_plan": [{"target": "acl"}],
                }
            ]
        }
        plan = compute_plan(
            playbook=playbook,
            inventory=_INVENTORY,
            run_id="govern-plan",
            timestamp=1,
        )

        with pytest.raises(ValueError, match="action"):
            collect_remediation(plan=plan, playbook=playbook, timestamp=1)

    def test_collection_is_deterministic_over_the_same_plan_and_playbook(self) -> None:
        """Test 8: two operators reach a byte-identical proposal."""
        first = _proposal()
        second = _proposal()

        assert first.to_canonical_bytes() == second.to_canonical_bytes()
        assert first.content_hash() == second.content_hash()

    def test_proposal_binds_the_plan_and_playbook_it_was_collected_from(self) -> None:
        """Test 9: a proposal cannot be re-pointed at a different world."""
        plan = _plan()
        proposal = collect_remediation(plan=plan, playbook=_PLAYBOOK, timestamp=1_700_000_000)

        other = collect_remediation(
            plan=compute_plan(
                playbook=_PLAYBOOK,
                inventory={"surfaces": []},
                run_id="govern-plan",
                timestamp=1_700_000_000,
            ),
            playbook=_PLAYBOOK,
            timestamp=1_700_000_000,
        )

        assert proposal.plan_hash != other.plan_hash
        assert proposal.playbook_hash == other.playbook_hash
        assert proposal.plan_hash.startswith("sha256:")
        assert proposal.playbook_hash.startswith("sha256:")


class TestProposalCannotApplyItself:
    """The collected artifact is a proposal, recorded as one."""

    def test_collected_proposal_is_an_unsigned_draft_that_cannot_apply_itself(self) -> None:
        """Test 10 (load-bearing): nothing on the proposal executes it."""
        proposal = _proposal()

        assert proposal.status is ProposalStatus.DRAFT
        assert proposal.human_signature is None
        assert proposal.is_signed() is False
        for name in ("apply", "execute", "run", "__call__"):
            assert not callable(getattr(proposal, name, None)), f"{name} would let the proposal apply itself"

    def test_signing_returns_a_new_proposal_and_leaves_the_draft_unsigned(self) -> None:
        """Test 11: the signature is a human's act on a copy, not a mutation."""
        proposal = _proposal()

        signed = proposal.sign("hmac-deadbeef")

        assert signed.is_signed() is True
        assert signed.status is ProposalStatus.SIGNED
        assert signed.steps == proposal.steps
        assert proposal.status is ProposalStatus.DRAFT
        assert proposal.is_signed() is False


class TestCli:
    """``bernstein governance plan --remediation-plan`` over real files."""

    def test_cli_writes_an_unsigned_proposal_and_names_findings_without_a_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test 12: the operator sees the proposal path and the uncovered findings."""
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
        (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
        playbook_file = tmp_path / "playbook.json"
        playbook_file.write_text(json.dumps(_PLAYBOOK), encoding="utf-8")
        inventory_file = tmp_path / "inventory.json"
        inventory_file.write_text(json.dumps(_INVENTORY), encoding="utf-8")
        out = tmp_path / "remediation.json"

        result = CliRunner().invoke(
            governance_group,
            [
                "plan",
                "--playbook",
                str(playbook_file),
                "--inventory",
                str(inventory_file),
                "--workdir",
                str(tmp_path),
                "--remediation-plan",
                str(out),
            ],
        )

        assert result.exit_code == 0, result.output
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["status"] == "draft"
        assert written["human_signature"] is None
        assert len(written["steps"]) == 3
        assert [u["surface"] for u in written["unremediated"]] == ["principal/agent-a"]
        assert "1 finding" in result.output or "1 findings" in result.output

    def test_cli_without_the_flag_writes_no_proposal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test 13: collection is opt-in; the existing plan output is unchanged in kind."""
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
        (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
        playbook_file = tmp_path / "playbook.json"
        playbook_file.write_text(json.dumps(_PLAYBOOK), encoding="utf-8")
        inventory_file = tmp_path / "inventory.json"
        inventory_file.write_text(json.dumps(_INVENTORY), encoding="utf-8")

        result = CliRunner().invoke(
            governance_group,
            [
                "plan",
                "--playbook",
                str(playbook_file),
                "--inventory",
                str(inventory_file),
                "--workdir",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert not (tmp_path / "remediation.json").exists()


def test_step_is_immutable() -> None:
    """Test 14: a collected step cannot be edited after the proposal is addressed."""
    step = RemediationStep(
        surface="s",
        playbook_clause="c",
        finding_kind="forbidden",
        action="set",
        target="t",
        value="v",
    )

    with pytest.raises(AttributeError):
        step.action = "remove"  # type: ignore[misc]
