"""Unit tests for :mod:`bernstein.core.quality.finding_disposition`.

Covers the disposition ledger introduced for review findings:
the ``FindingDisposition`` enum, the ``FindingDispositionRecord`` dataclass,
and the append-only JSONL ledger helpers (``record_disposition``,
``load_disposition_ledger``, ``get_current_disposition``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bernstein.core.quality.finding_disposition import (
    FindingDisposition,
    FindingDispositionRecord,
    disposition_ledger_path,
    get_current_disposition,
    load_disposition_ledger,
    record_disposition,
)

# ---------------------------------------------------------------------------
# FindingDisposition enum
# ---------------------------------------------------------------------------


def test_enum_has_six_members() -> None:
    assert {m.value for m in FindingDisposition} == {
        "open",
        "confirmed",
        "false_positive",
        "accepted_risk",
        "fixed",
        "duplicate",
    }


def test_enum_is_str_subtype() -> None:
    assert FindingDisposition.OPEN == "open"
    assert f"{FindingDisposition.FIXED}" == "fixed"


# ---------------------------------------------------------------------------
# FindingDispositionRecord
# ---------------------------------------------------------------------------


def test_record_defaults() -> None:
    rec = FindingDispositionRecord(
        finding_id="f-1",
        disposition=FindingDisposition.OPEN,
        resolved_by="qa",
    )
    assert rec.rationale == ""
    assert rec.task_id == ""
    assert isinstance(rec.timestamp, int)
    assert rec.timestamp > 0


def test_record_roundtrip() -> None:
    rec = FindingDispositionRecord(
        finding_id="f-2",
        disposition=FindingDisposition.FIXED,
        resolved_by="resolver",
        rationale="patched",
        task_id="t-9",
        timestamp=1_700_000_000,
    )
    assert FindingDispositionRecord.from_dict(rec.to_dict()) == rec


def test_record_from_dict_omits_optional_fields() -> None:
    data = {
        "finding_id": "f-3",
        "disposition": "confirmed",
        "resolved_by": "qa",
    }
    rec = FindingDispositionRecord.from_dict(data)
    assert rec.finding_id == "f-3"
    assert rec.disposition is FindingDisposition.CONFIRMED
    assert rec.resolved_by == "qa"
    assert rec.rationale == ""
    assert rec.task_id == ""
    assert rec.timestamp == 0


def test_record_to_dict_keys() -> None:
    rec = FindingDispositionRecord(
        finding_id="f-4",
        disposition=FindingDisposition.DUPLICATE,
        resolved_by="qa",
    )
    d = rec.to_dict()
    assert set(d) == {
        "finding_id",
        "disposition",
        "resolved_by",
        "rationale",
        "task_id",
        "timestamp",
    }
    assert d["disposition"] == "duplicate"


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def test_disposition_ledger_path(tmp_path: Path) -> None:
    assert disposition_ledger_path(tmp_path) == tmp_path / "finding_dispositions.jsonl"


# ---------------------------------------------------------------------------
# record_disposition + load_disposition_ledger
# ---------------------------------------------------------------------------


def test_record_creates_file_and_parents(tmp_path: Path) -> None:
    runtime = tmp_path / "deep" / "nested" / "runtime"
    rec = record_disposition(
        finding_id="f-10",
        disposition=FindingDisposition.OPEN,
        resolved_by="qa",
        rationale="initial",
        task_id="t-1",
        runtime_dir=runtime,
    )
    expected = runtime / "finding_dispositions.jsonl"
    assert expected.is_file()
    assert rec.finding_id == "f-10"
    assert rec.disposition is FindingDisposition.OPEN
    assert rec.rationale == "initial"
    assert rec.task_id == "t-1"


def test_record_appends_in_order(tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    for i, d in enumerate(
        [
            FindingDisposition.OPEN,
            FindingDisposition.CONFIRMED,
            FindingDisposition.FIXED,
        ]
    ):
        record_disposition(
            finding_id=f"f-{i}",
            disposition=d,
            resolved_by="qa",
            runtime_dir=runtime,
        )
    lines = (runtime / "finding_dispositions.jsonl").read_text().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["disposition"] for p in parsed] == ["open", "confirmed", "fixed"]


def test_load_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load_disposition_ledger(tmp_path / "nope") == []


def test_load_returns_records_in_append_order(tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    record_disposition(finding_id="a", disposition=FindingDisposition.OPEN, resolved_by="qa", runtime_dir=runtime)
    record_disposition(
        finding_id="b",
        disposition=FindingDisposition.CONFIRMED,
        resolved_by="qa",
        runtime_dir=runtime,
    )
    recs = load_disposition_ledger(runtime)
    assert [r.finding_id for r in recs] == ["a", "b"]
    assert recs[0].disposition is FindingDisposition.OPEN
    assert recs[1].disposition is FindingDisposition.CONFIRMED


def test_load_skips_malformed_lines(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runtime = tmp_path / "rt"
    ledger = runtime / "finding_dispositions.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    good_a = FindingDispositionRecord(
        finding_id="ok-1",
        disposition=FindingDisposition.OPEN,
        resolved_by="qa",
        timestamp=1,
    )
    good_b = FindingDispositionRecord(
        finding_id="ok-2",
        disposition=FindingDisposition.FIXED,
        resolved_by="qa",
        timestamp=2,
    )
    ledger.write_text(
        json.dumps(good_a.to_dict())
        + "\n"
        + "not-json\n"
        + json.dumps({"missing": "fields"})
        + "\n"
        + json.dumps(good_b.to_dict())
        + "\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="bernstein.core.quality.finding_disposition"):
        recs = load_disposition_ledger(runtime)
    assert [r.finding_id for r in recs] == ["ok-1", "ok-2"]
    assert any("malformed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# get_current_disposition
# ---------------------------------------------------------------------------


def test_get_current_returns_none_when_empty(tmp_path: Path) -> None:
    assert get_current_disposition("f-x", tmp_path / "missing") is None


def test_get_current_returns_latest(tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    record_disposition(
        finding_id="f",
        disposition=FindingDisposition.OPEN,
        resolved_by="qa",
        runtime_dir=runtime,
    )
    record_disposition(
        finding_id="f",
        disposition=FindingDisposition.CONFIRMED,
        resolved_by="qa",
        runtime_dir=runtime,
    )
    record_disposition(
        finding_id="f",
        disposition=FindingDisposition.FIXED,
        resolved_by="resolver",
        runtime_dir=runtime,
    )
    assert get_current_disposition("f", runtime) is FindingDisposition.FIXED


def test_get_current_ignores_other_ids(tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    record_disposition(
        finding_id="a",
        disposition=FindingDisposition.OPEN,
        resolved_by="qa",
        runtime_dir=runtime,
    )
    record_disposition(
        finding_id="b",
        disposition=FindingDisposition.FIXED,
        resolved_by="qa",
        runtime_dir=runtime,
    )
    assert get_current_disposition("a", runtime) is FindingDisposition.OPEN
    assert get_current_disposition("missing", runtime) is None
