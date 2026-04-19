"""Tests for token waste post-session report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.token_waste_report import (
    TokenRecord,
    _detect_loops,
    _detect_oversized_contexts,
    _detect_retries,
    _parse_token_records,
    analyze_token_waste,
    generate_session_waste_report,
)

# ---------------------------------------------------------------------------
# _parse_token_records
# ---------------------------------------------------------------------------


class TestParseTokenRecords:
    def test_parses_valid_records(self) -> None:
        raw = '{"ts": 1.0, "in": 100, "out": 10}\n{"ts": 2.0, "in": 200, "out": 20}\n'
        records = _parse_token_records(raw)
        assert len(records) == 2
        assert records[0].input_tokens == 100
        assert records[0].output_tokens == 10
        assert records[1].total == 220

    def test_skips_malformed_lines(self) -> None:
        raw = 'not json\n{"ts": 1.0, "in": 50, "out": 5}\n'
        records = _parse_token_records(raw)
        assert len(records) == 1
        assert records[0].input_tokens == 50

    def test_empty_input_returns_empty_list(self) -> None:
        assert _parse_token_records("") == []

    def test_skips_blank_lines(self) -> None:
        raw = '\n\n{"ts": 1.0, "in": 10, "out": 1}\n\n'
        records = _parse_token_records(raw)
        assert len(records) == 1


# ---------------------------------------------------------------------------
# _detect_retries
# ---------------------------------------------------------------------------


class TestDetectRetries:
    def test_no_retries_below_threshold(self) -> None:
        records = [TokenRecord(ts=1.0, input_tokens=100, output_tokens=10)] * 5
        findings = _detect_retries(records, spike_threshold=5_000)
        assert findings == []

    def test_consecutive_spikes_flagged(self) -> None:
        records = [
            TokenRecord(ts=1.0, input_tokens=6_000, output_tokens=100),
            TokenRecord(ts=2.0, input_tokens=6_500, output_tokens=100),
        ]
        findings = _detect_retries(records, spike_threshold=5_000)
        assert len(findings) == 1
        assert findings[0].category == "retry"
        assert findings[0].record_index == 1

    def test_non_consecutive_spikes_not_flagged(self) -> None:
        records = [
            TokenRecord(ts=1.0, input_tokens=6_000, output_tokens=100),
            TokenRecord(ts=2.0, input_tokens=100, output_tokens=10),
            TokenRecord(ts=3.0, input_tokens=6_000, output_tokens=100),
        ]
        findings = _detect_retries(records, spike_threshold=5_000)
        assert findings == []

    def test_three_consecutive_spikes_flagged_twice(self) -> None:
        records = [TokenRecord(ts=float(i), input_tokens=6_000, output_tokens=10) for i in range(3)]
        findings = _detect_retries(records, spike_threshold=5_000)
        assert len(findings) == 2


# ---------------------------------------------------------------------------
# _detect_loops
# ---------------------------------------------------------------------------


class TestDetectLoops:
    def test_no_loop_with_constant_growth(self) -> None:
        # Constant deltas — not a loop
        records = [TokenRecord(ts=float(i), input_tokens=100, output_tokens=10) for i in range(5)]
        findings = _detect_loops(records, growth_ratio=1.8)
        assert findings == []

    def test_loop_detected_when_delta_doubles(self) -> None:
        # totals: 0, 100, 300, 700  -> deltas: 100, 200, 400
        records = [
            TokenRecord(ts=0.0, input_tokens=0, output_tokens=0),
            TokenRecord(ts=1.0, input_tokens=100, output_tokens=0),
            TokenRecord(ts=2.0, input_tokens=200, output_tokens=0),
            TokenRecord(ts=3.0, input_tokens=400, output_tokens=0),
        ]
        findings = _detect_loops(records, growth_ratio=1.8)
        assert len(findings) >= 1
        assert findings[0].category == "loop"

    def test_insufficient_samples_no_loop(self) -> None:
        records = [TokenRecord(ts=0.0, input_tokens=100, output_tokens=0)]
        findings = _detect_loops(records, growth_ratio=1.8)
        assert findings == []


# ---------------------------------------------------------------------------
# _detect_oversized_contexts
# ---------------------------------------------------------------------------


class TestDetectOversizedContexts:
    def test_no_findings_below_threshold(self) -> None:
        records = [TokenRecord(ts=1.0, input_tokens=5_000, output_tokens=500)] * 3
        findings = _detect_oversized_contexts(records, interval_threshold=20_000)
        assert findings == []

    def test_oversized_record_flagged(self) -> None:
        records = [
            TokenRecord(ts=1.0, input_tokens=18_000, output_tokens=3_000),
        ]
        findings = _detect_oversized_contexts(records, interval_threshold=20_000)
        assert len(findings) == 1
        assert findings[0].category == "oversized_context"
        assert findings[0].token_count == 21_000

    def test_exact_threshold_is_flagged(self) -> None:
        records = [TokenRecord(ts=1.0, input_tokens=20_000, output_tokens=0)]
        findings = _detect_oversized_contexts(records, interval_threshold=20_000)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# analyze_token_waste
# ---------------------------------------------------------------------------


class TestAnalyzeTokenWaste:
    def test_empty_records_returns_zero_waste(self) -> None:
        report = analyze_token_waste("s1", [])
        assert report.total_tokens == 0
        assert report.wasted_tokens == 0
        assert report.efficiency_pct == pytest.approx(100.0)
        assert report.findings == []

    def test_all_small_records_clean(self) -> None:
        records = [TokenRecord(ts=float(i), input_tokens=100, output_tokens=10) for i in range(5)]
        report = analyze_token_waste("s2", records, retry_spike_threshold=5_000, oversized_threshold=20_000)
        assert report.total_tokens == 550
        assert report.findings == []
        assert report.efficiency_pct == pytest.approx(100.0)

    def test_retry_increases_waste(self) -> None:
        records = [
            TokenRecord(ts=1.0, input_tokens=6_000, output_tokens=100),
            TokenRecord(ts=2.0, input_tokens=6_000, output_tokens=100),
        ]
        report = analyze_token_waste("s3", records, retry_spike_threshold=5_000)
        assert any(f.category == "retry" for f in report.findings)
        assert report.wasted_tokens > 0
        assert report.efficiency_pct < 100.0

    def test_session_id_preserved(self) -> None:
        report = analyze_token_waste("my-session-42", [])
        assert report.session_id == "my-session-42"


# ---------------------------------------------------------------------------
# generate_session_waste_report
# ---------------------------------------------------------------------------


class TestGenerateSessionWasteReport:
    def test_returns_empty_report_when_no_sidecar(self, tmp_path: Path) -> None:
        report = generate_session_waste_report("sess-missing", tmp_path, save=False)
        assert report.session_id == "sess-missing"
        assert report.total_tokens == 0

    def test_reads_sidecar_and_returns_report(self, tmp_path: Path) -> None:
        sidecar = tmp_path / ".sdd" / "runtime" / "sess-x1.tokens"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text('{"ts": 1.0, "in": 100, "out": 10}\n{"ts": 2.0, "in": 200, "out": 20}\n')
        report = generate_session_waste_report("sess-x1", tmp_path, save=False)
        assert report.total_tokens == 330
        assert report.session_id == "sess-x1"

    def test_save_writes_json_file(self, tmp_path: Path) -> None:
        sidecar = tmp_path / ".sdd" / "runtime" / "sess-save.tokens"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text('{"ts": 1.0, "in": 50, "out": 5}\n')

        generate_session_waste_report("sess-save", tmp_path, save=True)

        out = tmp_path / ".sdd" / "metrics" / "token_waste_sess-save.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["session_id"] == "sess-save"
        assert data["total_tokens"] == 55

    def test_summary_string_contains_session_id(self, tmp_path: Path) -> None:
        report = generate_session_waste_report("sess-summary", tmp_path, save=False)
        assert "sess-summary" in report.summary()
