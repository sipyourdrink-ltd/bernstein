"""Content-addressed per-profile cost report (issue #2245).

Acceptance criteria under test:

1. The report over a fixed ledger fixture is byte-identical across
   runs (no timestamps inside the hashed payload).
2. The report hash verifies against its audit chain entry; tampering
   with one ledger line changes the report hash.
3. Tasks with a recorded profile transition are excluded, never split.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.core.cost.profile_attribution import (
    record_profile_transition,
)
from bernstein.core.cost.profile_report import (
    REPORT_KIND,
    REPORT_VERSION,
    build_profile_report,
    canonical_json_bytes,
    read_ledger_window,
    write_report_artifact,
)
from bernstein.core.security.audit_chain import (
    EVENT_COST_PROFILE_REPORT,
    AuditChainStore,
    record_cost_profile_report,
)


def _write_ledger(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=False, separators=(",", ":")))
            fh.write("\n")


def _ledger_row(
    task_id: str,
    profile: str,
    *,
    ts: float = 1000.0,
    role: str = "backend",
    model: str = "sonnet",
    cost_usd: float = 0.10,
    output_tokens: int = 100,
) -> dict[str, object]:
    tags: dict[str, str] = {}
    if profile:
        tags = {"response_profile": profile, "profile_content_sha256": "a" * 64}
    return {
        "ts": ts,
        "ts_iso": "2026-01-01T00:00:00+00:00",
        "run_id": "r-1",
        "task_id": task_id,
        "agent_id": "agent-1",
        "role": role,
        "feature_label": "",
        "model": model,
        "input_tokens": 50,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": cost_usd,
        "quota_envelope": "subscription",
        "tags": tags,
    }


@pytest.fixture()
def fixed_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "cost" / "ledger.jsonl"
    _write_ledger(
        path,
        [
            _ledger_row("t-1", "terse", ts=100.0, cost_usd=0.10, output_tokens=100),
            _ledger_row("t-2", "terse", ts=200.0, cost_usd=0.12, output_tokens=120),
            _ledger_row("t-3", "verbose", ts=300.0, cost_usd=0.30, output_tokens=300),
            _ledger_row("t-4", "", ts=400.0, cost_usd=0.05, output_tokens=50),
        ],
    )
    return path


class TestCanonicalJson:
    def test_stable_key_order_and_compact(self) -> None:
        assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_non_ascii_escaped(self) -> None:
        # ensure_ascii keeps the bytes identical regardless of locale.
        assert canonical_json_bytes({"k": "é"}) == b'{"k":"\\u00e9"}'


class TestReadLedgerWindow:
    def test_reads_entries_and_raw_lines(self, fixed_ledger: Path) -> None:
        entries, lines = read_ledger_window(fixed_ledger)
        assert len(entries) == 4
        assert len(lines) == 4

    def test_cutoff_filters_by_ts(self, fixed_ledger: Path) -> None:
        entries, lines = read_ledger_window(fixed_ledger, cutoff=250.0)
        assert [e.task_id for e in entries] == ["t-3", "t-4"]
        assert len(lines) == 2

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        entries, lines = read_ledger_window(tmp_path / "absent.jsonl")
        assert entries == []
        assert lines == []


class TestReportDeterminism:
    def test_byte_identical_across_builds(self, fixed_ledger: Path) -> None:
        """Acceptance 1: same ledger fixture -> same artifact bytes."""
        report_1 = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        report_2 = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        assert report_1.artifact_bytes() == report_2.artifact_bytes()
        assert report_1.sha256 == report_2.sha256

    def test_no_timestamps_in_hashed_payload(self, fixed_ledger: Path) -> None:
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        flat = json.dumps(report.content)
        for needle in ("timestamp", '"ts"', "ts_iso", "generated_at"):
            assert needle not in flat

    def test_sha256_matches_canonical_content(self, fixed_ledger: Path) -> None:
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        assert report.sha256 == hashlib.sha256(canonical_json_bytes(report.content)).hexdigest()

    def test_tampered_ledger_line_changes_hash(self, fixed_ledger: Path) -> None:
        """Acceptance 2 (second half): one flipped ledger line -> new hash."""
        before = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        raw = fixed_ledger.read_text(encoding="utf-8")
        # Tamper one value without breaking JSON: 0.12 -> 0.13.
        fixed_ledger.write_text(raw.replace('"cost_usd":0.12', '"cost_usd":0.13'), encoding="utf-8")
        after = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        assert before.sha256 != after.sha256
        assert before.content["ledger"]["lines_sha256"] != after.content["ledger"]["lines_sha256"]


class TestReportContent:
    def test_kind_version_and_ledger_range(self, fixed_ledger: Path) -> None:
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        content = report.content
        assert content["kind"] == REPORT_KIND
        assert content["version"] == REPORT_VERSION
        ledger_block = content["ledger"]
        assert ledger_block["line_count"] == 4
        _, lines = read_ledger_window(fixed_ledger)
        assert ledger_block["first_line_sha256"] == hashlib.sha256(lines[0].encode("utf-8")).hexdigest()
        assert ledger_block["last_line_sha256"] == hashlib.sha256(lines[-1].encode("utf-8")).hexdigest()

    def test_per_profile_figures(self, fixed_ledger: Path) -> None:
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        terse = report.content["profiles"]["terse"]
        assert terse["tasks"] == 2
        assert terse["output_tokens"] == 220
        assert terse["cost_usd"] == pytest.approx(0.22)
        assert terse["mean_output_tokens_per_task"] == pytest.approx(110.0)

    def test_quality_join_from_task_records(self, fixed_ledger: Path) -> None:
        task_records = [
            {"task_id": "t-1", "janitor_passed": True},
            {"task_id": "t-2", "janitor_passed": False},
            {"task_id": "t-3", "janitor_passed": True},
        ]
        report = build_profile_report(ledger_path=fixed_ledger, task_records=task_records, transitions=[])
        terse_q = report.content["profiles"]["terse"]["quality"]
        assert terse_q["tasks_with_outcome"] == 2
        assert terse_q["verdict_pass_rate"] == pytest.approx(0.5)
        verbose_q = report.content["profiles"]["verbose"]["quality"]
        assert verbose_q["verdict_pass_rate"] == pytest.approx(1.0)

    def test_quality_omitted_when_not_computable(self, fixed_ledger: Path) -> None:
        """Figures that cannot be computed from records are omitted."""
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        assert "quality" not in report.content["profiles"]["terse"]

    def test_transitioned_task_excluded(self, tmp_path: Path, fixed_ledger: Path) -> None:
        """Acceptance 3: transitioned task appears only in the excluded block."""
        transitions_path = tmp_path / "cost" / "profile_transitions.jsonl"
        record_profile_transition(
            transitions_path,
            task_id="t-3",
            agent_id="agent-1",
            from_profile="verbose",
            to_profile="terse",
        )
        from bernstein.core.cost.profile_attribution import load_transitions

        report = build_profile_report(
            ledger_path=fixed_ledger,
            task_records=[],
            transitions=load_transitions(transitions_path),
        )
        assert "verbose" not in report.content["profiles"]
        excluded = report.content["excluded"]
        assert excluded["task_ids"] == ["t-3"]
        assert excluded["reason"] == "profile_transition"
        assert excluded["cost_usd"] == pytest.approx(0.30)

    def test_insufficient_comparable_runs_flag(self, fixed_ledger: Path) -> None:
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        assert report.content["comparisons"] == []
        assert report.content["insufficient_comparable_runs"] is True

    def test_window_label_recorded(self, fixed_ledger: Path) -> None:
        report = build_profile_report(
            ledger_path=fixed_ledger, task_records=[], transitions=[], window_label="7d", cutoff=50.0
        )
        assert report.content["window"] == "7d"


class TestArtifactAndChain:
    def test_artifact_is_content_addressed(self, tmp_path: Path, fixed_ledger: Path) -> None:
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        out = write_report_artifact(report, tmp_path / "reports")
        assert out.name == f"{report.sha256}.json"
        envelope = json.loads(out.read_text(encoding="utf-8"))
        assert envelope["sha256"] == report.sha256
        assert envelope["content"] == report.content
        # The artifact bytes are exactly the canonical envelope encoding.
        assert out.read_bytes() == report.artifact_bytes()

    def test_chain_entry_verifies_against_artifact(self, tmp_path: Path, fixed_ledger: Path) -> None:
        """Acceptance 2 (first half): the chain entry carries the report
        sha and the chain verifies end to end."""
        report = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        event = record_cost_profile_report(
            chain=chain,
            report_sha256=report.sha256,
            ledger_lines_sha256=str(report.content["ledger"]["lines_sha256"]),
            ledger_first_line_sha256=str(report.content["ledger"]["first_line_sha256"]),
            ledger_last_line_sha256=str(report.content["ledger"]["last_line_sha256"]),
            ledger_line_count=int(report.content["ledger"]["line_count"]),
            window=str(report.content["window"]),
            artifact_name=f"{report.sha256}.json",
        )
        assert event.event_type == EVENT_COST_PROFILE_REPORT
        assert event.details["report_sha256"] == report.sha256
        assert event.details["prev_chain_digest"] is not None
        ok, errors = chain.verify()
        assert ok, errors
        # Recompute the report from the same ledger: hash must match the
        # recorded chain entry (third-party recomputability).
        recomputed = build_profile_report(ledger_path=fixed_ledger, task_records=[], transitions=[])
        assert recomputed.sha256 == event.details["report_sha256"]
