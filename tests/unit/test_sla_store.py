"""Unit tests for the SLA contract store (#2549)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bernstein.core.planning.sla_store import (
    SLAContractError,
    SLAContractIdError,
    SLAStore,
    build_contract,
)


def test_add_is_idempotent_by_derived_id(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = build_contract(subject_type="schedule", subject_id="sched_x", fire_frequency_s=3600)
    first = store.add(contract, now=1.0)
    second = store.add(contract, now=999.0)
    assert first.id == second.id
    assert first.created_at == second.created_at == 1.0
    assert len(store.list()) == 1


def test_persist_and_reload_preserves_hash(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = build_contract(
        subject_type="envelope",
        subject_id="subscription",
        spend_rate_usd_per_hour=2.5,
        remediation_cost_usd=1.0,
    )
    stored = store.add(contract)
    reloaded = store.get(stored.id)
    assert reloaded is not None
    assert reloaded.contract_hash == stored.contract_hash
    assert reloaded.spend_rate_usd_per_hour == 2.5


def test_for_subject_filters(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    a = store.add(build_contract(subject_type="schedule", subject_id="sched_a", fire_frequency_s=60))
    store.add(build_contract(subject_type="schedule", subject_id="sched_b", fire_frequency_s=60))
    hits = store.for_subject("schedule", "sched_a")
    assert [c.id for c in hits] == [a.id]


def test_remove(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = store.add(build_contract(subject_type="schedule", subject_id="s", fire_frequency_s=60))
    assert store.remove(contract.id) is True
    assert store.get(contract.id) is None
    assert store.remove(contract.id) is False


def test_contract_with_no_axis_is_rejected() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="schedule", subject_id="s")


def test_freshness_axis_requires_artifact_path() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="schedule", subject_id="s", artifact_freshness_s=3600)


def test_unknown_subject_type_is_rejected() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="galaxy", subject_id="s", fire_frequency_s=60)


class TestContractIdPathContainment:
    """Contract ids reach the store from operator input and from request paths,
    and both ``get`` and ``remove`` turn one into a filesystem operation."""

    @pytest.mark.parametrize(
        "contract_id",
        [
            "../../../../etc/passwd",
            "../outside",
            "..",
            ".",
            "/absolute/path",
            "sla_deadbeefdead/../../escape",
            "sla_NOTHEX123456",
            "sla_deadbeef",
            "sla_deadbeefdeadbeef",
            "contract\x00null",
            "sla_deadbeefdead\r\ninjected",
            "",
        ],
    )
    def test_malformed_id_is_refused_before_touching_the_filesystem(self, tmp_path: Path, contract_id: str) -> None:
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractIdError):
            store.get(contract_id)
        with pytest.raises(SLAContractIdError):
            store.remove(contract_id)

    def test_traversal_id_cannot_read_a_file_outside_the_store(self, tmp_path: Path) -> None:
        secret = tmp_path / "outside.json"
        secret.write_text('{"subject_type": "schedule"}', encoding="utf-8")
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractIdError):
            store.get("../../outside")
        assert secret.exists()

    def test_traversal_id_cannot_unlink_a_file_outside_the_store(self, tmp_path: Path) -> None:
        """``remove`` unlinks, so an unchecked id is arbitrary file deletion."""
        victim = tmp_path / "victim.json"
        victim.write_text("keep me", encoding="utf-8")
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractIdError):
            store.remove("../../victim")
        assert victim.read_text(encoding="utf-8") == "keep me"

    def test_refusal_is_a_contract_error_subclass(self, tmp_path: Path) -> None:
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractError):
            store.get("../escape")

    def test_derived_ids_still_round_trip(self, tmp_path: Path) -> None:
        store = SLAStore(tmp_path / ".sdd")
        stored = store.add(build_contract(subject_type="schedule", subject_id="s", fire_frequency_s=60))
        assert store.get(stored.id) is not None
        assert store.remove(stored.id) is True


class TestLogInjection:
    def test_crlf_in_a_store_path_cannot_forge_a_log_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A value carrying CR/LF must not open a second log line.

        ``_load_contract`` logs the path it failed on. A forged line is how an
        attacker plants a fake record an operator or log shipper then reads as
        real, so the value has to stay on one line.
        """
        from bernstein.core.planning.sla_store import _load_contract

        forged = "WARNING  bernstein: Registered SLA contract sla_000000000000"
        path = tmp_path / f"evil\r\n{forged}.json"
        with caplog.at_level(logging.WARNING, logger="bernstein.core.planning.sla_store"):
            assert _load_contract(path) is None

        assert caplog.records, "the failed load must still be logged"
        for record in caplog.records:
            rendered = record.getMessage()
            # The attacker's text is still present -- it cannot be stripped, only
            # neutralised. What must not survive is the line break that would
            # make it a separate record: the whole thing stays on one line, with
            # the CR/LF rendered as visible escapes.
            assert rendered.splitlines() == [rendered]
            assert "\n" not in rendered
            assert "\r" not in rendered
            assert "\\r\\n" in rendered
            assert not rendered.startswith(forged)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain", "plain"),
            ("a\nb", "a\\nb"),
            ("a\r\nb", "a\\r\\nb"),
            ("a\tb", "a\\x09b"),
            ("a\x1b[31mb", "a\\x1b[31mb"),
            ("line\u2028sep", "line\\x2028sep"),
        ],
    )
    def test_single_line_escapes_every_control_character(self, raw: str, expected: str) -> None:
        from bernstein.core.planning.sla_store import _single_line

        assert _single_line(raw) == expected

    def test_single_line_caps_length(self) -> None:
        from bernstein.core.planning.sla_store import _single_line

        out = _single_line("x" * 5000)
        assert out.endswith("...(truncated)")
        assert len(out) < 300
