"""Tests for the govern-discover observation envelope (#5082)."""

from __future__ import annotations

import pytest

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


class TestCollisionRule:
    def test_colliding_entity_ids_are_disambiguated_not_overwritten(self) -> None:
        # Load-bearing (#5082): the same shape on Inventory.get_surface
        # silently returns the first of two same-id entries and no lookup
        # can reach the second. Named here so the contrast is asserted, not
        # assumed: the flat model shadows, the envelope disambiguates.
        from bernstein.core.govern.inventory_models import Inventory, Surface

        inventory = Inventory(
            surfaces=(
                Surface(surface="entity:aa", observed_value="web-1", evidence_ref="q1"),
                Surface(surface="entity:aa", observed_value="web-2", evidence_ref="q2"),
            )
        )
        assert len(inventory.surfaces) == 2
        shadowed = inventory.get_surface("entity:aa")
        assert shadowed is not None and shadowed.observed_value == "web-1"

        first = _envelope(payload={"hostname": "web-1"})
        second = _envelope(payload={"hostname": "web-2"})
        ledger = ObservationLedger().ingest(first).ingest(second)

        assert len(ledger.envelopes) == 2
        assert ledger.entity_ids() == frozenset({first.entity_id})

        keys = ledger.keys_for(first.entity_id)
        assert len(keys) == 2
        assert len(set(keys)) == 2
        looked_up_first = ledger.get(keys[0])
        looked_up_second = ledger.get(keys[1])
        assert looked_up_first is not None and looked_up_second is not None
        assert {looked_up_first.payload["hostname"], looked_up_second.payload["hostname"]} == {
            "web-1",
            "web-2",
        }

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
            ledger.get(_envelope().entity_id)

    def test_uncollided_id_is_its_own_key(self) -> None:
        only = _envelope()
        ledger = ObservationLedger().ingest(only)
        assert ledger.key_for(only) == only.entity_id
        assert ledger.get(only.entity_id) is only


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
        first = ObservationEnvelope.for_entity(
            entity_class="host",
            normalised_key="AB12CD",
            payload={"hostname": "web-1", "hardware_id": "AB-12-CD"},
            observed_at="2026-09-03T09:00:00Z",
            evidence_ref="discover-pass-7",
        )
        second = ObservationEnvelope.for_entity(
            entity_class="host",
            normalised_key="AB12CD",
            payload={"hostname": "web-1-renamed", "hardware_id": "AB-12-CD"},
            observed_at="2026-09-03T10:00:00Z",
            evidence_ref="discover-pass-8",
        )
        assert first.entity_id == second.entity_id
        assert first.entity_id.startswith("entity:")
        ledger = ObservationLedger().ingest(first).ingest(second)
        keys = ledger.keys_for(first.entity_id)
        assert len(keys) == 2
        hosts = {ledger.get(k).payload["hostname"] for k in keys}  # type: ignore[union-attr]
        assert hosts == {"web-1", "web-1-renamed"}

    def test_normalised_key_spellings_resolve_to_one_id(self) -> None:
        from bernstein.core.govern.identity_join import entity_id_for, normalise_hardware_id

        assert entity_id_for("host", normalise_hardware_id("AB12CD")) == entity_id_for(
            "host", normalise_hardware_id("ab-12-cd")
        )

    def test_two_classes_sharing_a_key_stay_distinct(self) -> None:
        host = ObservationEnvelope.for_entity(
            entity_class="host",
            normalised_key="AB12CD",
            payload={},
            observed_at="2026-09-03T09:00:00Z",
            evidence_ref="discover-pass-7",
        )
        endpoint = ObservationEnvelope.for_entity(
            entity_class="model_endpoint",
            normalised_key="AB12CD",
            payload={},
            observed_at="2026-09-03T09:00:00Z",
            evidence_ref="discover-pass-7",
        )
        assert host.entity_id != endpoint.entity_id


class TestRoundTrip:
    def test_to_dict_from_dict_round_trip(self) -> None:
        envelope = _envelope(errors={"mcp.config": "permission denied"})
        rebuilt = ObservationEnvelope.from_dict(envelope.to_dict())
        assert rebuilt == envelope

    def test_empty_entity_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="entity_id"):
            _envelope(entity_id="  ")
