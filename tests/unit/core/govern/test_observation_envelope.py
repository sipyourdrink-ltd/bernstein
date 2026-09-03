"""Tests for the govern-discover observation envelope (#5082)."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import cast

import pytest

from bernstein.core.govern.identity_join import (
    CanonicalField,
    FieldMapTable,
    SourceFieldMap,
    join_sources,
    normalise_hardware_id,
)
from bernstein.core.govern.inventory_models import Inventory, Surface
from bernstein.core.govern.observation import ObservationEnvelope, ObservationLedger


def _envelope(
    *,
    entity_id: str = "entity:aa",
    entity_class: str = "host",
    payload: dict[str, object] | None = None,
    errors: dict[str, str] | None = None,
) -> ObservationEnvelope:
    return ObservationEnvelope(
        entity_id=entity_id,
        entity_class=entity_class,
        payload=payload if payload is not None else {"hostname": "web-1"},
        observed_at="2026-09-03T09:00:00Z",
        evidence_ref="discover-pass-7",
        errors=errors if errors is not None else {},
    )


def _for_entity(*, hostname: str, entity_class: str = "host") -> ObservationEnvelope:
    return ObservationEnvelope.for_entity(
        entity_class=entity_class,
        key_field="hardware_id",
        normalised_key="AB12CD",
        payload={"hostname": hostname, "hardware_id": "AB-12-CD"},
        observed_at="2026-09-03T09:00:00Z",
        evidence_ref="discover-pass-7",
    )


class TestCollisionRule:
    def test_colliding_entity_ids_are_disambiguated_not_overwritten(self) -> None:
        # Load-bearing (#5082): the same shape on Inventory.get_surface
        # silently returns the first of two same-id entries and no lookup
        # can reach the second. Named here so the contrast is asserted, not
        # assumed: the flat model shadows, the envelope disambiguates.
        inventory = Inventory(
            surfaces=(
                Surface(surface="entity:aa", observed_value="web-1", evidence_ref="q1"),
                Surface(surface="entity:aa", observed_value="web-2", evidence_ref="q2"),
            )
        )
        assert len(inventory.surfaces) == 2
        shadowed = inventory.get_surface("entity:aa")
        assert shadowed is not None and shadowed.observed_value == "web-1"

        ledger = (
            ObservationLedger()
            .ingest(_envelope(payload={"hostname": "web-1"}))
            .ingest(_envelope(payload={"hostname": "web-2"}))
        )
        assert len(ledger.envelopes) == 2
        keys = ledger.keys_for("entity:aa")
        assert len(set(keys)) == 2
        hosts = {ledger.get(k).payload["hostname"] for k in keys}  # type: ignore[union-attr]
        assert hosts == {"web-1", "web-2"}

    def test_colliding_keys_do_not_depend_on_ingestion_order(self) -> None:
        first = _envelope(payload={"hostname": "web-1"})
        second = _envelope(payload={"hostname": "web-2"})

        forward = ObservationLedger().ingest(first).ingest(second)
        reverse = ObservationLedger().ingest(second).ingest(first)

        assert forward.key_for(first) == reverse.key_for(first)
        assert forward.key_for(second) == reverse.key_for(second)

    def test_collided_id_itself_is_not_a_silent_key(self) -> None:
        ledger = (
            ObservationLedger()
            .ingest(_envelope(payload={"hostname": "web-1"}))
            .ingest(_envelope(payload={"hostname": "web-2"}))
        )
        with pytest.raises(ValueError, match="2 envelopes"):
            ledger.get("entity:aa")

    def test_uncollided_id_is_its_own_key(self) -> None:
        only = _envelope()
        ledger = ObservationLedger().ingest(only)
        assert ledger.key_for(only) == only.entity_id
        assert ledger.get(only.entity_id) is only

    def test_byte_identical_envelopes_are_deduplicated(self) -> None:
        envelope = _envelope()
        ledger = ObservationLedger().ingest(envelope).ingest(ObservationEnvelope.from_dict(envelope.to_dict()))

        assert ledger.envelopes == (envelope,)
        assert ledger.keys_for(envelope.entity_id) == (envelope.entity_id,)
        assert ledger.get(envelope.entity_id) is envelope

    def test_issued_collision_key_stays_resolvable(self) -> None:
        first = _envelope(payload={"hostname": "web-1"}, errors={"mcp.config": "EPERM"})
        second = _envelope(payload={"hostname": "web-2"})
        ledger = ObservationLedger().ingest(first).ingest(second)
        key = ledger.key_for(first)

        with pytest.raises(TypeError):
            cast(MutableMapping[str, object], first.payload)["hostname"] = "changed"
        with pytest.raises(TypeError):
            cast(MutableMapping[str, str], first.errors)["mcp.config"] = "changed"

        assert ledger.key_for(first) == key
        assert ledger.get(key) is first


class TestPartialIngestion:
    def test_envelope_with_partial_errors_is_ingested(self) -> None:
        partial = _envelope(
            payload={"hostname": "web-1"},
            errors={"mcp.config": "permission denied reading ~/.cursor/mcp.json"},
        )
        ledger = ObservationLedger().ingest(partial)
        assert ledger.get(partial.entity_id) is partial

    def test_missing_field_carries_its_own_cause(self) -> None:
        partial = _envelope(
            payload={"hostname": "web-1"},
            errors={
                "mcp.config": "permission denied reading ~/.cursor/mcp.json",
                "runtime.processes": "process table unreadable: EPERM",
            },
        )
        assert partial.errors["mcp.config"] == "permission denied reading ~/.cursor/mcp.json"
        assert partial.errors["runtime.processes"] == "process table unreadable: EPERM"

    def test_error_marker_that_names_no_cause_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="mcp.config"):
            _envelope(errors={"mcp.config": ""})

    def test_collected_empty_is_a_present_field_not_a_missing_one(self) -> None:
        # "not collected, here's why" and "collected, value is empty" must
        # stay distinguishable: the empty value is IN the payload and the
        # probe that produced it is NOT in errors (#5082 gap statement).
        envelope = _envelope(payload={"version": ""}, errors={"runtime.processes": "EPERM"})
        assert envelope.payload["version"] == ""
        assert "version" not in envelope.errors


class TestStableEntityId:
    def test_entity_id_stable_across_hostname_change(self) -> None:
        # The hostname is a payload field; the entity key is the hardware
        # id, so the same host observed under two names is one entity.
        first = _for_entity(hostname="web-1")
        second = _for_entity(hostname="web-1-renamed")
        assert first.entity_id == second.entity_id
        assert first.entity_id.startswith("entity:")
        ledger = ObservationLedger().ingest(first).ingest(second)
        keys = ledger.keys_for(first.entity_id)
        assert len(keys) == 2
        hosts = {ledger.get(k).payload["hostname"] for k in keys}  # type: ignore[union-attr]
        assert hosts == {"web-1", "web-1-renamed"}

    def test_envelope_id_matches_the_graph_node_for_the_same_key(self) -> None:
        table = FieldMapTable(
            fields=(CanonicalField(name="hardware_id", normaliser="hardware_id"),),
            key="hardware_id",
            sources={
                "inventory": SourceFieldMap(
                    source="inventory",
                    fields={"hardware_id": "hardware_id"},
                )
            },
        )
        graph = join_sources(
            table=table,
            records={"inventory": [{"hardware_id": "ab-12-cd"}]},
            observed_at="2026-09-03T09:00:00Z",
            pass_id="discover-pass-7",
        ).graph
        node = graph.nodes[0]
        envelope = ObservationEnvelope.for_entity(
            entity_class="host",
            key_field=node.key_field,
            normalised_key=node.key,
            payload={"hostname": "web-1"},
            observed_at="2026-09-03T09:00:00Z",
            evidence_ref="discover-pass-7",
        )

        assert envelope.entity_id == node.entity_id
        assert node.key == normalise_hardware_id("ab-12-cd")


class TestRoundTrip:
    def test_to_dict_from_dict_round_trip(self) -> None:
        envelope = _envelope(errors={"mcp.config": "permission denied"})
        rebuilt = ObservationEnvelope.from_dict(envelope.to_dict())
        assert rebuilt == envelope

    def test_empty_entity_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="entity_id"):
            _envelope(entity_id="  ")
