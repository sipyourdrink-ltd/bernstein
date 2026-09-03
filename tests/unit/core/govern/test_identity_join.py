"""Tests for cross-source inventory identity: field maps, join, profile, store.

Each test is named for the property it protects. The load-bearing one is
``test_absent_field_is_declared_null_not_inferred``: a null in a joined record
must say whether the source declares it carries no such field or whether the
record simply did not supply a value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.govern.identity_join import (
    DECLARED_ABSENT,
    MISSING,
    PRESENT,
    CanonicalField,
    EntityStore,
    EntityStoreError,
    FieldMapError,
    FieldMapTable,
    IdentityJoinError,
    SourceFieldMap,
    join_sources,
    normalise_account_name,
    normalise_hardware_id,
)

OBSERVED_AT = "2026-09-02T05:12:00Z"


def _table() -> FieldMapTable:
    return FieldMapTable(
        fields=(
            CanonicalField(name="hardware_id", normaliser="hardware_id"),
            CanonicalField(
                name="account_name",
                normaliser="account_name",
                edge_kind="assigned_to",
            ),
            CanonicalField(name="os_version"),
        ),
        key="hardware_id",
        sources={
            "mdm": SourceFieldMap(
                source="mdm",
                fields={
                    "hardware_id": "SerialNumber",
                    "account_name": "AssignedUser",
                    "os_version": "OSVersion",
                },
            ),
            # The agent feed carries no OS version at all: absence is declared.
            "agent": SourceFieldMap(
                source="agent",
                fields={
                    "hardware_id": "serial",
                    "account_name": "user",
                    "os_version": None,
                },
            ),
        },
    )


def _records() -> dict[str, list[dict[str, object]]]:
    return {
        "mdm": [
            {
                "SerialNumber": "ab-12-cd",
                "AssignedUser": " Alice@Example.COM ",
                "OSVersion": "14.5",
            }
        ],
        "agent": [
            {"serial": "AB12CD", "user": "alice@example.com"},
            # Second host: the agent declares a ``user`` column but this row
            # did not supply one.
            {"serial": "ZZ-99"},
        ],
    }


def _join(pass_id: str = "pass-1"):
    return join_sources(
        table=_table(),
        records=_records(),
        observed_at=OBSERVED_AT,
        pass_id=pass_id,
    )


def _attr(node, source: str, field: str):
    for a in node.attributes:
        if a.source == source and a.field == field:
            return a
    raise AssertionError(f"no attribute {source}/{field} on {node.entity_id}")


def _node_by_key(graph, key: str):
    for n in graph.nodes:
        if n.key == key:
            return n
    raise AssertionError(f"no node with key {key!r}")


class TestFieldMapAbsence:
    """1. Declared absence is distinguishable from a missing value."""

    def test_absent_field_is_declared_null_not_inferred(self) -> None:
        graph = _join().graph

        joined = _node_by_key(graph, "AB12CD")
        declared = _attr(joined, "agent", "os_version")
        assert declared.value is None
        assert declared.presence == DECLARED_ABSENT

        other = _node_by_key(graph, "ZZ99")
        unsupplied = _attr(other, "agent", "account_name")
        assert unsupplied.value is None
        assert unsupplied.presence == MISSING

        # Both read as null; only the presence marker separates "this source
        # carries no such field" from "this row supplied no value".
        assert declared.value == unsupplied.value
        assert declared.presence != unsupplied.presence

    def test_source_map_must_declare_every_canonical_field(self) -> None:
        with pytest.raises(FieldMapError):
            FieldMapTable(
                fields=(
                    CanonicalField(name="hardware_id", normaliser="hardware_id"),
                    CanonicalField(name="os_version"),
                ),
                key="hardware_id",
                sources={
                    "agent": SourceFieldMap(
                        source="agent",
                        fields={"hardware_id": "serial"},
                    )
                },
            )

    def test_record_without_a_key_value_is_rejected(self) -> None:
        with pytest.raises(IdentityJoinError):
            join_sources(
                table=_table(),
                records={"agent": [{"user": "bob"}]},
                observed_at=OBSERVED_AT,
                pass_id="pass-1",
            )


class TestNormalisers:
    """2. Normalisation happens before the join, not after."""

    def test_normaliser_runs_before_join_not_after(self) -> None:
        graph = _join().graph

        # "ab-12-cd" (mdm) and "AB12CD" (agent) are the same hardware id.
        keys = sorted(n.key for n in graph.nodes)
        assert keys == ["AB12CD", "ZZ99"]

        joined = _node_by_key(graph, "AB12CD")
        assert {a.source for a in joined.attributes} == {"mdm", "agent"}

    def test_normalise_hardware_id_strips_to_alphanumerics_upper(self) -> None:
        assert normalise_hardware_id(" ab-12:cd ") == "AB12CD"

    def test_normalise_account_name_trims_and_lowercases(self) -> None:
        assert normalise_account_name(" Alice@Example.COM ") == "alice@example.com"


class TestGraphShape:
    """3. The joined output is nodes and edges, never a flat frame."""

    def test_join_output_is_nodes_and_edges_never_a_flat_frame(self) -> None:
        graph = _join().graph

        assert hasattr(graph, "nodes")
        assert hasattr(graph, "edges")

        # One host seen by two sources is one node carrying both sources'
        # attributes, not two rows.
        joined = _node_by_key(graph, "AB12CD")
        assert len([n for n in graph.nodes if n.key == "AB12CD"]) == 1
        assert _attr(joined, "mdm", "os_version").presence == PRESENT
        assert _attr(joined, "agent", "os_version").presence == DECLARED_ABSENT

        assert graph.edges
        for edge in graph.edges:
            assert edge.kind == "assigned_to"
            assert edge.from_id.startswith("entity:")
            assert edge.to_id.startswith("entity:")

    def test_edge_endpoints_are_entity_ids_never_urls(self) -> None:
        graph = _join().graph
        for edge in graph.edges:
            for endpoint in (edge.from_id, edge.to_id):
                assert "://" not in endpoint
                assert endpoint == endpoint.lower()

    def test_conflicting_values_are_recorded_per_source_not_resolved(self) -> None:
        records = _records()
        records["agent"][0]["user"] = "someone.else@example.com"
        result = join_sources(
            table=_table(),
            records=records,
            observed_at=OBSERVED_AT,
            pass_id="pass-1",
        )
        joined = _node_by_key(result.graph, "AB12CD")
        assert _attr(joined, "mdm", "account_name").value == "alice@example.com"
        assert _attr(joined, "agent", "account_name").value == "someone.else@example.com"

    def test_entity_id_is_stable_across_passes(self) -> None:
        first = {n.key: n.entity_id for n in _join("pass-1").graph.nodes}
        second = {n.key: n.entity_id for n in _join("pass-2").graph.nodes}
        assert first == second


class TestSourceProfile:
    """4. Every pass records its own per-source profile."""

    def test_per_source_profile_recorded_every_pass(self, tmp_path: Path) -> None:
        store = EntityStore(tmp_path)
        store.append_profiles(_join("pass-1").profiles)
        store.append_profiles(_join("pass-2").profiles)

        profiles = store.load_profiles()
        assert len(profiles) == 4
        assert {(p["source"], p["pass_id"]) for p in profiles} == {
            ("mdm", "pass-1"),
            ("agent", "pass-1"),
            ("mdm", "pass-2"),
            ("agent", "pass-2"),
        }

    def test_profile_counts_rows_columns_and_nulls(self) -> None:
        profiles = {p.source: p for p in _join().profiles}

        assert profiles["mdm"].rows == 1
        assert profiles["agent"].rows == 2
        assert profiles["agent"].columns == ("hardware_id", "account_name")
        assert profiles["agent"].null_counts["os_version"] == 2
        assert profiles["agent"].null_counts["account_name"] == 1
        assert profiles["mdm"].observed_at == OBSERVED_AT


class TestEntityStore:
    """5. The store is one schema-validated file per entity."""

    def test_entity_file_is_schema_validated(self, tmp_path: Path) -> None:
        store = EntityStore(tmp_path)
        graph = _join().graph
        store.write_graph(graph)

        entity_id = _node_by_key(graph, "AB12CD").entity_id
        loaded = store.load_entity(entity_id)
        assert loaded["observed_at"] == OBSERVED_AT

        path = store.entity_path(entity_id)
        broken = json.loads(path.read_text(encoding="utf-8"))
        del broken["observed_at"]
        path.write_text(json.dumps(broken), encoding="utf-8")

        with pytest.raises(EntityStoreError):
            store.load_entity(entity_id)

    def test_entity_file_rejects_a_url_where_an_edge_id_belongs(self, tmp_path: Path) -> None:
        store = EntityStore(tmp_path)
        graph = _join().graph
        store.write_graph(graph)

        entity_id = _node_by_key(graph, "AB12CD").entity_id
        path = store.entity_path(entity_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["edges"][0]["to"] = "https://mdm.example.com/accounts/alice"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(EntityStoreError):
            store.load_entity(entity_id)

    def test_store_writes_one_file_per_entity_and_lookup_files(self, tmp_path: Path) -> None:
        store = EntityStore(tmp_path)
        graph = _join().graph
        store.write_graph(graph)

        entity_files = sorted((tmp_path / "entities").glob("*.json"))
        assert len(entity_files) == len(graph.nodes)

        sources = json.loads((tmp_path / "lookups" / "sources.json").read_text(encoding="utf-8"))
        assert sources == ["agent", "mdm"]
        edge_kinds = json.loads((tmp_path / "lookups" / "edge_kinds.json").read_text(encoding="utf-8"))
        assert edge_kinds == ["assigned_to"]

    def test_edge_roundtrip_preserves_from_id(self, tmp_path: Path) -> None:
        """from_id is lost if to_dict omits it: read from disk and check."""
        store = EntityStore(tmp_path)
        graph = _join().graph
        store.write_graph(graph)

        entity_id = _node_by_key(graph, "AB12CD").entity_id
        loaded = store.load_entity(entity_id)

        for edge in loaded["edges"]:
            assert "from" in edge, "write_graph/load_entity round-trip must preserve the 'from' field"
            assert "to" in edge
            assert edge["from"].startswith("entity:")
            assert edge["to"].startswith("entity:")

        # Also verify the loaded 'from' matches the Python dataclass.
        assert len(graph.edges) == len(loaded["edges"])
        for py_edge, loaded_edge in zip(graph.edges, loaded["edges"], strict=True):
            assert loaded_edge["from"] == py_edge.from_id
            assert loaded_edge["to"] == py_edge.to_id

    def test_entity_schema_requires_from_in_edges(self, tmp_path: Path) -> None:
        """A missing 'from' on an edge must be rejected at load time."""
        store = EntityStore(tmp_path)
        graph = _join().graph
        store.write_graph(graph)

        entity_id = _node_by_key(graph, "AB12CD").entity_id
        path = store.entity_path(entity_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["edges"][0].pop("from")
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(EntityStoreError):
            store.load_entity(entity_id)
