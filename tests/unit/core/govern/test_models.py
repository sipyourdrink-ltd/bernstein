"""Tests for inventory and playbook data models in govern plan subsystem."""

from __future__ import annotations

import pytest

from bernstein.core.govern import compute_plan
from bernstein.core.govern.inventory_models import Inventory, Surface
from bernstein.core.govern.plan_models import PlanEntryKind
from bernstein.core.govern.playbook_models import (
    Playbook,
    PlaybookClause,
    PlaybookValidationError,
)


class TestSurface:
    """Tests for Surface dataclass."""

    def test_surface_creation(self) -> None:
        s = Surface(
            surface="arn:aws:s3:::my-bucket",
            observed_value="public-read",
            evidence_ref="query-123",
        )
        assert s.surface == "arn:aws:s3:::my-bucket"
        assert s.observed_value == "public-read"
        assert s.evidence_ref == "query-123"

    def test_surface_to_dict(self) -> None:
        s = Surface(
            surface="arn:aws:s3:::my-bucket",
            observed_value="public-read",
            evidence_ref="query-123",
        )
        d = s.to_dict()
        assert d == {
            "surface": "arn:aws:s3:::my-bucket",
            "observed_value": "public-read",
            "evidence_ref": "query-123",
            # Serialized, but deliberately NOT hashed -- see identity_dict.
            "observed_at": 0.0,
        }

    def test_surface_identity_excludes_the_observation_time(self) -> None:
        """The hash is about the environment; the timestamp is about the observer."""
        seen_now = Surface("arn:aws:s3:::b", "public-read", "q1", observed_at=1000.0)
        seen_later = Surface("arn:aws:s3:::b", "public-read", "q1", observed_at=2000.0)
        assert seen_now.identity_dict() == seen_later.identity_dict()
        assert "observed_at" not in seen_now.identity_dict()

    def test_surface_from_dict(self) -> None:
        d = {
            "surface": "arn:aws:s3:::my-bucket",
            "observed_value": "public-read",
            "evidence_ref": "query-123",
        }
        s = Surface.from_dict(d)
        assert s.surface == "arn:aws:s3:::my-bucket"
        assert s.observed_value == "public-read"
        assert s.evidence_ref == "query-123"

    def test_surface_immutable(self) -> None:
        s = Surface(
            surface="arn:aws:s3:::my-bucket",
            observed_value="public-read",
            evidence_ref="query-123",
        )
        with pytest.raises(AttributeError):
            s.surface = "other"  # type: ignore[attr-defined]


class TestInventory:
    """Tests for Inventory dataclass."""

    def test_inventory_creation(self) -> None:
        surfaces = (
            Surface("arn:aws:s3:::bucket1", "private", "q1"),
            Surface("arn:aws:s3:::bucket2", "public-read", "q2"),
        )
        inv = Inventory(surfaces=surfaces)
        assert len(inv.surfaces) == 2
        assert inv.surfaces[0].surface == "arn:aws:s3:::bucket1"

    def test_inventory_to_dict(self) -> None:
        surfaces = (
            Surface("arn:aws:s3:::bucket1", "private", "q1"),
            Surface("arn:aws:s3:::bucket2", "public-read", "q2"),
        )
        inv = Inventory(surfaces=surfaces)
        d = inv.to_dict()
        assert "surfaces" in d
        assert len(d["surfaces"]) == 2
        assert d["surfaces"][0]["surface"] == "arn:aws:s3:::bucket1"

    def test_inventory_from_dict(self) -> None:
        d = {
            "surfaces": [
                {"surface": "arn:aws:s3:::bucket1", "observed_value": "private", "evidence_ref": "q1"},
                {"surface": "arn:aws:s3:::bucket2", "observed_value": "public-read", "evidence_ref": "q2"},
            ]
        }
        inv = Inventory.from_dict(d)
        assert len(inv.surfaces) == 2
        assert inv.surfaces[0].surface == "arn:aws:s3:::bucket1"
        assert inv.surfaces[1].observed_value == "public-read"

    def test_inventory_content_hash_deterministic(self) -> None:
        surfaces = (
            Surface("arn:aws:s3:::bucket1", "private", "q1"),
            Surface("arn:aws:s3:::bucket2", "public-read", "q2"),
        )
        inv1 = Inventory(surfaces=surfaces)
        inv2 = Inventory(surfaces=surfaces)
        assert inv1.content_hash() == inv2.content_hash()

    def test_inventory_content_hash_different_order(self) -> None:
        surfaces1 = (
            Surface("arn:aws:s3:::bucket1", "private", "q1"),
            Surface("arn:aws:s3:::bucket2", "public-read", "q2"),
        )
        surfaces2 = (
            Surface("arn:aws:s3:::bucket2", "public-read", "q2"),
            Surface("arn:aws:s3:::bucket1", "private", "q1"),
        )
        inv1 = Inventory(surfaces=surfaces1)
        inv2 = Inventory(surfaces=surfaces2)
        # Hash differs because surfaces preserve their tuple order
        assert inv1.content_hash() != inv2.content_hash()

    def test_inventory_content_hash_different_content(self) -> None:
        surfaces1 = (Surface("arn:aws:s3:::bucket1", "private", "q1"),)
        surfaces2 = (Surface("arn:aws:s3:::bucket1", "public-read", "q1"),)
        inv1 = Inventory(surfaces=surfaces1)
        inv2 = Inventory(surfaces=surfaces2)
        assert inv1.content_hash() != inv2.content_hash()

    def test_inventory_get_surface(self) -> None:
        surfaces = (Surface("arn:aws:s3:::bucket1", "private", "q1"),)
        inv = Inventory(surfaces=surfaces)
        s = inv.get_surface("arn:aws:s3:::bucket1")
        assert s is not None
        assert s.observed_value == "private"

        missing = inv.get_surface("arn:aws:s3:::nonexistent")
        assert missing is None

    def test_inventory_surface_ids(self) -> None:
        surfaces = (
            Surface("arn:aws:s3:::bucket1", "private", "q1"),
            Surface("arn:aws:s3:::bucket2", "public-read", "q2"),
        )
        inv = Inventory(surfaces=surfaces)
        ids = inv.surface_ids()
        assert "arn:aws:s3:::bucket1" in ids
        assert "arn:aws:s3:::bucket2" in ids
        assert len(ids) == 2

    def test_inventory_immutable(self) -> None:
        inv = Inventory(surfaces=(Surface("a", "b", "c"),))
        with pytest.raises(AttributeError):
            inv.surfaces = ()  # type: ignore[attr-defined]


class TestPlaybookClause:
    """Tests for PlaybookClause dataclass."""

    def test_clause_creation_forbidden(self) -> None:
        c = PlaybookClause(
            surface="arn:aws:s3:::my-bucket",
            clause="No public S3 buckets",
            kind="forbidden",
        )
        assert c.surface == "arn:aws:s3:::my-bucket"
        assert c.clause == "No public S3 buckets"
        assert c.kind == "forbidden"
        assert c.declared_value is None
        assert c.declared_ceiling is None

    def test_clause_creation_required(self) -> None:
        c = PlaybookClause(
            surface="arn:aws:iam::policy/ReadOnly",
            clause="IAM policies must be ReadOnly",
            kind="required",
            declared_value="ReadOnly",
        )
        assert c.kind == "required"
        assert c.declared_value == "ReadOnly"

    def test_clause_creation_permitted_with_ceiling(self) -> None:
        c = PlaybookClause(
            surface="arn:aws:s3:::my-bucket",
            clause="Bucket permissions must not exceed private",
            kind="permitted",
            declared_ceiling="private",
        )
        assert c.kind == "permitted"
        assert c.declared_ceiling == "private"

    def test_clause_to_dict(self) -> None:
        c = PlaybookClause(
            surface="arn:aws:s3:::my-bucket",
            clause="No public S3 buckets",
            kind="forbidden",
        )
        d = c.to_dict()
        assert d == {
            "surface": "arn:aws:s3:::my-bucket",
            "clause": "No public S3 buckets",
            "kind": "forbidden",
        }

    def test_clause_to_dict_with_optional(self) -> None:
        c = PlaybookClause(
            surface="arn:aws:s3:::my-bucket",
            clause="Bucket permissions must not exceed private",
            kind="permitted",
            declared_ceiling="private",
        )
        d = c.to_dict()
        assert d["declared_ceiling"] == "private"

    def test_clause_from_dict(self) -> None:
        d = {
            "surface": "arn:aws:s3:::my-bucket",
            "clause": "No public S3 buckets",
            "kind": "forbidden",
        }
        c = PlaybookClause.from_dict(d)
        assert c.kind == "forbidden"

    def test_clause_from_dict_with_optional(self) -> None:
        d = {
            "surface": "arn:aws:s3:::my-bucket",
            "clause": "Bucket permissions must not exceed private",
            "kind": "permitted",
            "declared_ceiling": "private",
        }
        c = PlaybookClause.from_dict(d)
        assert c.declared_ceiling == "private"

    def test_clause_immutable(self) -> None:
        c = PlaybookClause(surface="a", clause="b", kind="forbidden")
        with pytest.raises(AttributeError):
            c.surface = "other"  # type: ignore[attr-defined]


class TestPlaybook:
    """Tests for Playbook dataclass."""

    def test_playbook_creation(self) -> None:
        clauses = (
            PlaybookClause("s1", "c1", "forbidden"),
            PlaybookClause("s2", "c2", "required", declared_value="v2"),
            PlaybookClause("s3", "c3", "permitted", declared_ceiling="v3"),
        )
        pb = Playbook(clauses=clauses)
        assert len(pb.clauses) == 3

    def test_playbook_to_dict(self) -> None:
        clauses = (
            PlaybookClause("s1", "c1", "forbidden"),
            PlaybookClause("s2", "c2", "required", declared_value="v2"),
        )
        pb = Playbook(clauses=clauses)
        d = pb.to_dict()
        assert "clauses" in d
        assert len(d["clauses"]) == 2

    def test_playbook_from_dict(self) -> None:
        d = {
            "clauses": [
                {"surface": "s1", "clause": "c1", "kind": "forbidden"},
                {"surface": "s2", "clause": "c2", "kind": "required", "declared_value": "v2"},
            ]
        }
        pb = Playbook.from_dict(d)
        assert len(pb.clauses) == 2
        assert pb.clauses[1].declared_value == "v2"

    def test_playbook_content_hash_deterministic(self) -> None:
        clauses = (
            PlaybookClause("s1", "c1", "forbidden"),
            PlaybookClause("s2", "c2", "required", declared_value="v2"),
        )
        pb1 = Playbook(clauses=clauses)
        pb2 = Playbook(clauses=clauses)
        assert pb1.content_hash() == pb2.content_hash()

    def test_semantically_identical_playbooks_hash_equal_regardless_of_clause_order(
        self,
    ) -> None:
        # A playbook is a *set* of declared posture rules: two operators who
        # write the same rules in a different order have declared the same
        # posture, and a receipt naming "the playbook in force" must not
        # depend on which order an author happened to type the clauses in.
        clauses1 = (
            PlaybookClause("s1", "c1", "forbidden"),
            PlaybookClause("s2", "c2", "required", declared_value="v2"),
        )
        clauses2 = (
            PlaybookClause("s2", "c2", "required", declared_value="v2"),
            PlaybookClause("s1", "c1", "forbidden"),
        )
        pb1 = Playbook(clauses=clauses1)
        pb2 = Playbook(clauses=clauses2)
        assert pb1.content_hash() == pb2.content_hash()

    def test_playbook_content_hash_different_content(self) -> None:
        clauses1 = (PlaybookClause("s1", "c1", "forbidden"),)
        clauses2 = (PlaybookClause("s1", "different clause", "forbidden"),)
        pb1 = Playbook(clauses=clauses1)
        pb2 = Playbook(clauses=clauses2)
        assert pb1.content_hash() != pb2.content_hash()

    def test_playbook_clauses_by_kind(self) -> None:
        clauses = (
            PlaybookClause("s1", "c1", "forbidden"),
            PlaybookClause("s2", "c2", "required", declared_value="v2"),
            PlaybookClause("s3", "c3", "forbidden"),
        )
        pb = Playbook(clauses=clauses)
        forbidden = pb.clauses_by_kind("forbidden")
        assert len(forbidden) == 2
        required = pb.clauses_by_kind("required")
        assert len(required) == 1

    def test_playbook_surface_ids(self) -> None:
        clauses = (
            PlaybookClause("s1", "c1", "forbidden"),
            PlaybookClause("s2", "c2", "required", declared_value="v2"),
        )
        pb = Playbook(clauses=clauses)
        ids = pb.surface_ids()
        assert "s1" in ids
        assert "s2" in ids
        assert len(ids) == 2

    def test_playbook_immutable(self) -> None:
        pb = Playbook(clauses=(PlaybookClause("s1", "c1", "forbidden"),))
        with pytest.raises(AttributeError):
            pb.clauses = ()  # type: ignore[attr-defined]


class TestPlaybookValidation:
    """Tests for #4979: a playbook is data, not a script -- an unknown key or
    an undeclared reference is a validation error, never a silently-ignored
    field."""

    def test_unknown_clause_field_is_rejected_not_ignored(self) -> None:
        d = {
            "surface": "s1",
            "clause": "c1",
            "kind": "forbidden",
            "notes": "left over from an older schema",
        }
        with pytest.raises(PlaybookValidationError):
            PlaybookClause.from_dict(d)

    def test_unknown_playbook_field_is_rejected_not_ignored(self) -> None:
        d = {
            "clauses": [{"surface": "s1", "clause": "c1", "kind": "forbidden"}],
            "owner": "someone typed the wrong top-level key",
        }
        with pytest.raises(PlaybookValidationError):
            Playbook.from_dict(d)

    def test_clause_kind_outside_declared_set_is_rejected(self) -> None:
        d = {"surface": "s1", "clause": "c1", "kind": "recommended"}
        with pytest.raises(PlaybookValidationError):
            PlaybookClause.from_dict(d)

    def test_clause_referencing_undeclared_principal_class_fails_validation(
        self,
    ) -> None:
        clause = PlaybookClause(
            surface="tool:shell",
            clause="worker agents may not invoke shell",
            kind="forbidden",
            principal_class="worker",
        )
        with pytest.raises(PlaybookValidationError):
            # "manager" is declared, but the clause names "worker", which is
            # not -- a ceiling that references a principal class nobody
            # declared can never be satisfied or violated, so it must fail
            # to construct rather than parse into an inert no-op.
            Playbook(clauses=(clause,), principal_classes=("manager",))

    def test_clause_referencing_declared_principal_class_is_accepted(self) -> None:
        clause = PlaybookClause(
            surface="tool:shell",
            clause="worker agents may not invoke shell",
            kind="forbidden",
            principal_class="worker",
        )
        pb = Playbook(clauses=(clause,), principal_classes=("worker", "manager"))
        assert pb.clauses[0].principal_class == "worker"

    def test_content_hash_changes_when_principal_classes_differ(self) -> None:
        clause = PlaybookClause("s1", "c1", "forbidden")
        pb1 = Playbook(clauses=(clause,), principal_classes=("worker",))
        pb2 = Playbook(clauses=(clause,), principal_classes=("manager",))
        assert pb1.content_hash() != pb2.content_hash()


class TestRoundTrip:
    """Tests for round-trip serialization."""

    def test_inventory_round_trip(self) -> None:
        original = Inventory(
            surfaces=(
                Surface("arn:aws:s3:::bucket1", "private", "q1"),
                Surface("arn:aws:s3:::bucket2", "public-read", "q2"),
            )
        )
        d = original.to_dict()
        restored = Inventory.from_dict(d)
        assert restored.content_hash() == original.content_hash()

    def test_playbook_round_trip(self) -> None:
        original = Playbook(
            clauses=(
                PlaybookClause("s1", "c1", "forbidden"),
                PlaybookClause("s2", "c2", "required", declared_value="v2"),
                PlaybookClause("s3", "c3", "permitted", declared_ceiling="v3"),
            )
        )
        d = original.to_dict()
        restored = Playbook.from_dict(d)
        assert restored.content_hash() == original.content_hash()


_PLAYBOOK = {
    "forbidden": [{"surface": "s3.public-read", "clause": "C1"}],
    "permitted": [{"surface": "iam.max-role-count", "clause": "C2", "declared_ceiling": "1.0"}],
    "required": [{"surface": "kms.key-rotation", "clause": "C3", "declared_value": "enabled"}],
}


class TestComputePlanUnknown:
    """A surface the inventory could not read must never read as compliant."""

    def test_unreadable_surface_is_reported_unknown_not_compliant(self) -> None:
        inventory = {
            "surfaces": [
                {"surface": "s3.public-read", "unreadable": True, "evidence_ref": "E1"},
                {
                    "surface": "iam.max-role-count",
                    "unreadable": True,
                    "evidence_ref": "E2",
                },
            ]
        }
        plan = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=0)
        unknown = {e.surface for e in plan.entries if e.kind is PlanEntryKind.UNKNOWN}
        assert unknown == {"s3.public-read", "iam.max-role-count"}
        # and they must not have been silently classified as anything else
        other = {e.surface for e in plan.entries if e.kind is not PlanEntryKind.UNKNOWN}
        assert "s3.public-read" not in other
        assert "iam.max-role-count" not in other

    def test_unenumerated_surface_does_not_read_as_compliant(self) -> None:
        """An enumeration that failed is declared, not inferred from absence."""
        inventory = {"surfaces": [], "unreadable": ["s3.public-read"]}
        plan = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=0)
        kinds = {e.surface: e.kind for e in plan.entries}
        assert kinds["s3.public-read"] is PlanEntryKind.UNKNOWN

    def test_unreadable_required_surface_is_unknown_not_absent(self) -> None:
        """Absent and unreadable are different claims about the environment."""
        inventory = {"surfaces": [{"surface": "kms.key-rotation", "unreadable": True, "evidence_ref": "E3"}]}
        plan = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=0)
        kinds = {e.surface: e.kind for e in plan.entries}
        assert kinds["kms.key-rotation"] is PlanEntryKind.UNKNOWN

    def test_a_genuinely_absent_required_surface_is_still_absent(self) -> None:
        """The unknown path must not swallow the absent one."""
        plan = compute_plan(
            playbook=_PLAYBOOK,
            inventory={"surfaces": []},
            run_id="r",
            timestamp=0,
        )
        kinds = {e.surface: e.kind for e in plan.entries}
        assert kinds["kms.key-rotation"] is PlanEntryKind.ABSENT


class TestComputePlanCeiling:
    def test_ceiling_breach_smaller_than_one_is_still_wider(self) -> None:
        """A fractional breach is a breach; integer truncation hid it."""
        inventory = {
            "surfaces": [
                {
                    "surface": "iam.max-role-count",
                    "observed_value": "1.9",
                    "evidence_ref": "E1",
                }
            ]
        }
        plan = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=0)
        wider = [e for e in plan.entries if e.kind is PlanEntryKind.WIDER_CEILING]
        assert [e.surface for e in wider] == ["iam.max-role-count"]

    def test_observed_at_the_ceiling_is_not_a_breach(self) -> None:
        inventory = {
            "surfaces": [
                {
                    "surface": "iam.max-role-count",
                    "observed_value": "1.0",
                    "evidence_ref": "E1",
                }
            ]
        }
        plan = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=0)
        assert not [e for e in plan.entries if e.kind is PlanEntryKind.WIDER_CEILING]

    def test_observed_below_the_ceiling_is_not_a_breach(self) -> None:
        inventory = {
            "surfaces": [
                {
                    "surface": "iam.max-role-count",
                    "observed_value": "0.4",
                    "evidence_ref": "E1",
                }
            ]
        }
        plan = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=0)
        assert not [e for e in plan.entries if e.kind is PlanEntryKind.WIDER_CEILING]


class TestComputePlanArtifact:
    def test_two_runs_over_same_inputs_are_byte_identical(self) -> None:
        inventory = {
            "surfaces": [
                {
                    "surface": "s3.public-read",
                    "observed_value": "true",
                    "evidence_ref": "E1",
                }
            ]
        }
        a = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=7)
        b = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=7)
        assert a.to_canonical_bytes() == b.to_canonical_bytes()

    def test_conformant_environment_produces_an_empty_plan_not_silence(self) -> None:
        """An empty diff is still an artifact, anchored to its inputs."""
        playbook = {"forbidden": [{"surface": "s3.public-read", "clause": "C1"}]}
        plan = compute_plan(
            playbook=playbook,
            inventory={"surfaces": []},
            run_id="r",
            timestamp=0,
        )
        assert plan.entries == ()
        assert plan.inputs_hash.startswith("sha256:")
        assert plan.to_canonical_bytes()

    def test_every_entry_names_its_evidence_and_governing_clause(self) -> None:
        inventory = {
            "surfaces": [
                {
                    "surface": "s3.public-read",
                    "observed_value": "true",
                    "evidence_ref": "E1",
                },
                {
                    "surface": "iam.max-role-count",
                    "observed_value": "9.0",
                    "evidence_ref": "E2",
                },
            ]
        }
        plan = compute_plan(playbook=_PLAYBOOK, inventory=inventory, run_id="r", timestamp=0)
        assert plan.entries
        for entry in plan.entries:
            assert entry.playbook_clause, f"{entry.surface} names no clause"
            if entry.kind is not PlanEntryKind.ABSENT:
                assert entry.evidence_ref, f"{entry.surface} names no evidence"

    def test_inputs_hash_changes_when_the_inventory_changes(self) -> None:
        base = compute_plan(
            playbook=_PLAYBOOK,
            inventory={"surfaces": []},
            run_id="r",
            timestamp=0,
        )
        changed = compute_plan(
            playbook=_PLAYBOOK,
            inventory={
                "surfaces": [
                    {
                        "surface": "s3.public-read",
                        "observed_value": "true",
                        "evidence_ref": "E1",
                    }
                ]
            },
            run_id="r",
            timestamp=0,
        )
        assert base.inputs_hash != changed.inputs_hash
