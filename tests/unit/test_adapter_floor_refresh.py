"""Floor-map refresh pipeline: data-only diff + signed update receipt (#2515)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS, AdapterAdvisory
from bernstein.adapters.floor_refresh import (
    build_floor_update_receipt,
    diff_floor_maps,
    parse_feed,
    receipt_sha256,
    render_advisory_block,
    verify_floor_update_receipt,
    write_advisory_block,
)
from bernstein.adapters.security_floor import hash_floor_map

_GENERATED_AT = "2026-07-16T00:00:00+00:00"

_ADVISORIES_PATH = Path(__file__).resolve().parents[2] / "src" / "bernstein" / "adapters" / "advisories.py"


def _feed_from_current() -> dict:
    return {
        "schema_version": 1,
        "adapters": {
            name: {
                "min_safe_version": adv.min_safe_version,
                "advisory_id": adv.advisory_id,
                "note": adv.note,
            }
            for name, adv in ADAPTER_MIN_SAFE_VERSIONS.items()
        },
    }


class TestParseFeed:
    def test_round_trips_current_map(self) -> None:
        parsed = parse_feed(_feed_from_current())
        assert hash_floor_map(parsed) == hash_floor_map(dict(ADAPTER_MIN_SAFE_VERSIONS))

    def test_missing_field_raises(self) -> None:
        with pytest.raises(ValueError, match="missing field"):
            parse_feed({"adapters": {"aider": {"min_safe_version": "0.60.0"}}})

    def test_non_object_feed_raises(self) -> None:
        with pytest.raises(ValueError, match="adapters"):
            parse_feed({"nope": 1})


class TestDiff:
    def test_no_change_is_empty(self) -> None:
        old = dict(ADAPTER_MIN_SAFE_VERSIONS)
        assert diff_floor_maps(old, old).is_empty

    def test_version_bump_is_data_only_change(self) -> None:
        old = dict(ADAPTER_MIN_SAFE_VERSIONS)
        new = dict(old)
        bumped = AdapterAdvisory(
            adapter="aider",
            min_safe_version="0.70.0",
            advisory_id=old["aider"].advisory_id,
            note=old["aider"].note,
        )
        new["aider"] = bumped
        diff = diff_floor_maps(old, new)
        assert diff.added == [] and diff.removed == []
        assert diff.changed == [{"adapter": "aider", "field": "min_safe_version", "old": "0.60.0", "new": "0.70.0"}]


class TestRenderFaithful:
    def test_render_reproduces_current_source_block(self) -> None:
        # The renderer is faithful: regenerating the in-code map reproduces the
        # current source block, so a bump is a pure data diff, no logic change.
        rendered = render_advisory_block(dict(ADAPTER_MIN_SAFE_VERSIONS))
        source = _ADVISORIES_PATH.read_text(encoding="utf-8")
        assert rendered.strip() in source

    def test_write_is_idempotent(self, tmp_path: Path) -> None:
        # A copy of advisories.py rewritten with the same map is byte-identical.
        original = _ADVISORIES_PATH.read_text(encoding="utf-8")
        copy = tmp_path / "advisories.py"
        copy.write_text(original, encoding="utf-8")
        write_advisory_block(copy, dict(ADAPTER_MIN_SAFE_VERSIONS))
        assert copy.read_text(encoding="utf-8") == original


class TestUpdateReceipt:
    def test_receipt_is_deterministic_and_binds_hashes(self) -> None:
        old = dict(ADAPTER_MIN_SAFE_VERSIONS)
        new = dict(old)
        new["aider"] = AdapterAdvisory("aider", "0.70.0", old["aider"].advisory_id, old["aider"].note)
        a = build_floor_update_receipt(old, new, generated_at=_GENERATED_AT)
        b = build_floor_update_receipt(old, new, generated_at=_GENERATED_AT)
        assert a == b
        assert a["old_floor_map_hash"] == hash_floor_map(old)
        assert a["new_floor_map_hash"] == hash_floor_map(new)
        assert a["new_floor_map_hash"] != a["old_floor_map_hash"]

    def test_written_receipt_verifies_and_tamper_detected(self, tmp_path: Path) -> None:
        old = dict(ADAPTER_MIN_SAFE_VERSIONS)
        new = dict(old)
        new["aider"] = AdapterAdvisory("aider", "0.70.0", old["aider"].advisory_id, old["aider"].note)
        receipt = build_floor_update_receipt(old, new, generated_at=_GENERATED_AT)
        doc = {"receipt": receipt, "receipt_sha256": receipt_sha256(receipt)}
        path = tmp_path / "update.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        assert verify_floor_update_receipt(json.loads(path.read_text(encoding="utf-8")))
        doc["receipt"]["new_floor_map_hash"] = "sha256:" + "0" * 64
        assert not verify_floor_update_receipt(doc)


class TestUpdateReceiptAnchoring:
    def test_update_receipt_anchors_into_chain(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit_chain import (
            EVENT_ADAPTER_FLOOR_UPDATE,
            AuditChainStore,
            record_adapter_floor_update_receipt,
        )

        old = dict(ADAPTER_MIN_SAFE_VERSIONS)
        new = dict(old)
        new["aider"] = AdapterAdvisory("aider", "0.70.0", old["aider"].advisory_id, old["aider"].note)
        receipt = build_floor_update_receipt(old, new, generated_at=_GENERATED_AT)
        sha = receipt_sha256(receipt)
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        event = record_adapter_floor_update_receipt(
            chain=chain,
            receipt_sha256=sha,
            old_floor_map_hash=receipt["old_floor_map_hash"],
            new_floor_map_hash=receipt["new_floor_map_hash"],
            diff=receipt["diff"],
        )
        assert event.event_type == EVENT_ADAPTER_FLOOR_UPDATE
        rows = chain.query(event_type=EVENT_ADAPTER_FLOOR_UPDATE)
        assert rows[0].details["new_floor_map_hash"] == receipt["new_floor_map_hash"]
        assert "prev_chain_digest" in rows[0].details
        assert chain.verify()[0]
