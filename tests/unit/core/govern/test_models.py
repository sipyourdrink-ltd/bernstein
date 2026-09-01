"""Tests for inventory and playbook data models in govern plan subsystem."""

from __future__ import annotations

import pytest

from bernstein.core.govern.inventory_models import Inventory, Surface
from bernstein.core.govern.playbook_models import Playbook, PlaybookClause


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
        }

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

    def test_playbook_content_hash_different_order(self) -> None:
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
        # Hash differs because clauses preserve their tuple order
        assert pb1.content_hash() != pb2.content_hash()

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
