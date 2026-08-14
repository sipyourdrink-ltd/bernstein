"""Tests for cross-tenant data isolation verification suite.

Verifies IsolationTest, IsolationReport, TenantIsolationVerifier, and
render_isolation_report against real temporary filesystem state.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.tenant_isolation import (
    ensure_tenant_data_layout,
    tenant_data_paths,
)
from bernstein.core.security.tenant_isolation_verify import (
    IsolationReport,
    IsolationTest,
    TenantIsolationVerifier,
    render_isolation_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class StubTask:
    """Minimal task stand-in for isolation verification tests."""

    id: str
    tenant_id: str
    title: str = "stub"


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write a list of dicts as a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdd(tmp_path: Path) -> Path:
    """Create a temporary .sdd directory and provision two tenants."""
    d = tmp_path / ".sdd"
    d.mkdir()
    ensure_tenant_data_layout(d, "tenant-a")
    ensure_tenant_data_layout(d, "tenant-b")
    return d


@pytest.fixture()
def verifier() -> TenantIsolationVerifier:
    return TenantIsolationVerifier()


# ---------------------------------------------------------------------------
# IsolationTest frozen dataclass
# ---------------------------------------------------------------------------


class TestIsolationTestDataclass:
    """IsolationTest is frozen and holds expected fields."""

    def test_frozen(self) -> None:
        t = IsolationTest(name="x", description="d", passed=True, details="ok")
        with pytest.raises(FrozenInstanceError):
            t.name = "y"  # type: ignore[misc]

    def test_fields(self) -> None:
        t = IsolationTest(name="n", description="desc", passed=False, details="err")
        assert t.name == "n"
        assert t.description == "desc"
        assert t.passed is False
        assert t.details == "err"


# ---------------------------------------------------------------------------
# IsolationReport frozen dataclass
# ---------------------------------------------------------------------------


class TestIsolationReportDataclass:
    """IsolationReport is frozen and computes counts correctly."""

    def test_frozen(self) -> None:
        r = IsolationReport(tests=(), total=0, passed_count=0, failed_count=0, passed=True)
        with pytest.raises(FrozenInstanceError):
            r.total = 5  # type: ignore[misc]

    def test_all_passed(self) -> None:
        t1 = IsolationTest(name="a", description="", passed=True, details="")
        t2 = IsolationTest(name="b", description="", passed=True, details="")
        r = IsolationReport(tests=(t1, t2), total=2, passed_count=2, failed_count=0, passed=True)
        assert r.passed is True
        assert r.failed_count == 0

    def test_some_failed(self) -> None:
        t1 = IsolationTest(name="a", description="", passed=True, details="")
        t2 = IsolationTest(name="b", description="", passed=False, details="bad")
        r = IsolationReport(tests=(t1, t2), total=2, passed_count=1, failed_count=1, passed=False)
        assert r.passed is False
        assert r.failed_count == 1


# ---------------------------------------------------------------------------
# verify_task_isolation
# ---------------------------------------------------------------------------


class TestVerifyTaskIsolation:
    """Task store isolation checks."""

    def test_clean_store_passes(self, verifier: TenantIsolationVerifier) -> None:
        store: dict[str, Any] = {
            "t1": StubTask(id="t1", tenant_id="tenant-a"),
            "t2": StubTask(id="t2", tenant_id="tenant-b"),
        }
        results = verifier.verify_task_isolation(store, "tenant-a", "tenant-b")
        assert all(r.passed for r in results)

    def test_empty_store_passes(self, verifier: TenantIsolationVerifier) -> None:
        results = verifier.verify_task_isolation({}, "tenant-a", "tenant-b")
        assert all(r.passed for r in results)

    def test_ambiguous_tenant_id_detected(self, verifier: TenantIsolationVerifier) -> None:
        store: dict[str, Any] = {
            "t1": StubTask(id="t1", tenant_id=""),
        }
        results = verifier.verify_task_isolation(store, "tenant-a", "tenant-b")
        ambig = [r for r in results if r.name == "no_ambiguous_tenant_id"]
        assert len(ambig) == 1
        assert ambig[0].passed is False

    def test_whitespace_tenant_id_is_ambiguous(self, verifier: TenantIsolationVerifier) -> None:
        store: dict[str, Any] = {
            "t1": StubTask(id="t1", tenant_id="   "),
        }
        results = verifier.verify_task_isolation(store, "x", "y")
        ambig = [r for r in results if r.name == "no_ambiguous_tenant_id"]
        assert ambig[0].passed is False

    def test_three_results_returned(self, verifier: TenantIsolationVerifier) -> None:
        results = verifier.verify_task_isolation({}, "a", "b")
        assert len(results) == 3

    def test_single_tenant_store(self, verifier: TenantIsolationVerifier) -> None:
        store: dict[str, Any] = {
            "t1": StubTask(id="t1", tenant_id="tenant-a"),
            "t2": StubTask(id="t2", tenant_id="tenant-a"),
        }
        results = verifier.verify_task_isolation(store, "tenant-a", "tenant-b")
        assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# verify_cost_isolation
# ---------------------------------------------------------------------------


class TestVerifyCostIsolation:
    """Cost / metrics directory isolation checks."""

    def test_distinct_dirs_pass(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")
        dirs_test = [r for r in results if r.name == "cost_dirs_distinct"]
        assert dirs_test[0].passed is True

    def test_no_overlap(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")
        overlap_test = [r for r in results if r.name == "cost_dirs_no_overlap"]
        assert overlap_test[0].passed is True

    def test_clean_cost_data_passes(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        paths_b = tenant_data_paths(sdd, "tenant-b")
        _write_jsonl(paths_a.metrics_dir / "cost.jsonl", [{"cost_usd": 1.0, "tenant_id": "tenant-a"}])
        _write_jsonl(paths_b.metrics_dir / "cost.jsonl", [{"cost_usd": 2.0, "tenant_id": "tenant-b"}])
        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")
        content_test = [r for r in results if r.name == "cost_content_not_cross_contaminated"]
        assert content_test[0].passed is True

    def test_cross_contaminated_cost_detected(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        paths_b = tenant_data_paths(sdd, "tenant-b")
        # Tenant-b record placed in tenant-a directory
        _write_jsonl(paths_a.metrics_dir / "cost.jsonl", [{"cost_usd": 1.0, "tenant_id": "tenant-b"}])
        _write_jsonl(paths_b.metrics_dir / "cost.jsonl", [{"cost_usd": 2.0, "tenant_id": "tenant-b"}])
        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")
        content_test = [r for r in results if r.name == "cost_content_not_cross_contaminated"]
        assert content_test[0].passed is False

    def test_missing_dirs_pass(self, verifier: TenantIsolationVerifier, tmp_path: Path) -> None:
        empty_sdd = tmp_path / "empty_sdd"
        empty_sdd.mkdir()
        results = verifier.verify_cost_isolation(empty_sdd, "tenant-a", "tenant-b")
        content_test = [r for r in results if r.name == "cost_content_not_cross_contaminated"]
        assert content_test[0].passed is True


# ---------------------------------------------------------------------------
# verify_wal_isolation
# ---------------------------------------------------------------------------


class TestVerifyWalIsolation:
    """WAL namespace isolation checks."""

    def test_distinct_wal_dirs(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")
        dirs_test = [r for r in results if r.name == "wal_dirs_distinct"]
        assert dirs_test[0].passed is True

    def test_wal_dirs_no_overlap(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")
        overlap_test = [r for r in results if r.name == "wal_dirs_no_overlap"]
        assert overlap_test[0].passed is True

    def test_wal_dirs_rooted_in_tenant(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")
        rooted_test = [r for r in results if r.name == "wal_dirs_rooted_in_tenant"]
        assert rooted_test[0].passed is True

    def test_clean_wal_files_pass(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        paths_b = tenant_data_paths(sdd, "tenant-b")
        _write_jsonl(paths_a.wal_dir / "run-001.wal.jsonl", [{"actor": "tenant-a", "seq": 0}])
        _write_jsonl(paths_b.wal_dir / "run-002.wal.jsonl", [{"actor": "tenant-b", "seq": 0}])
        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")
        leak_test = [r for r in results if r.name == "wal_content_no_cross_leak"]
        assert leak_test[0].passed is True

    def test_wal_cross_leak_detected(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        paths_b = tenant_data_paths(sdd, "tenant-b")
        # Tenant-b actor placed in tenant-a WAL dir
        _write_jsonl(paths_a.wal_dir / "leaked.wal.jsonl", [{"actor": "tenant-b", "seq": 0}])
        _write_jsonl(paths_b.wal_dir / "run.wal.jsonl", [{"actor": "tenant-b", "seq": 0}])
        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")
        leak_test = [r for r in results if r.name == "wal_content_no_cross_leak"]
        assert leak_test[0].passed is False

    def test_missing_wal_dirs_pass(self, verifier: TenantIsolationVerifier, tmp_path: Path) -> None:
        empty_sdd = tmp_path / "empty_sdd"
        empty_sdd.mkdir()
        results = verifier.verify_wal_isolation(empty_sdd, "a", "b")
        leak_test = [r for r in results if r.name == "wal_content_no_cross_leak"]
        assert leak_test[0].passed is True


# ---------------------------------------------------------------------------
# verify_archive_isolation
# ---------------------------------------------------------------------------


class TestVerifyArchiveIsolation:
    """Archive record isolation checks."""

    def test_clean_shared_archive_passes(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        archive_file = sdd / "archive" / "tasks.jsonl"
        _write_jsonl(
            archive_file,
            [
                {"task_id": "t1", "tenant_id": "tenant-a"},
                {"task_id": "t2", "tenant_id": "tenant-b"},
            ],
        )
        results = verifier.verify_archive_isolation(archive_file, "tenant-a", "tenant-b")
        no_shared = [r for r in results if r.name == "archive_no_shared_task_ids"]
        assert no_shared[0].passed is True

    def test_overlapping_task_ids_detected(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        archive_file = sdd / "archive" / "tasks.jsonl"
        _write_jsonl(
            archive_file,
            [
                {"task_id": "shared-1", "tenant_id": "tenant-a"},
                {"task_id": "shared-1", "tenant_id": "tenant-b"},
            ],
        )
        results = verifier.verify_archive_isolation(archive_file, "tenant-a", "tenant-b")
        no_shared = [r for r in results if r.name == "archive_no_shared_task_ids"]
        assert no_shared[0].passed is False

    def test_tenant_archive_paths_distinct(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        results = verifier.verify_archive_isolation(sdd, "tenant-a", "tenant-b")
        paths_test = [r for r in results if r.name == "archive_tenant_paths_distinct"]
        assert paths_test[0].passed is True

    def test_tenant_archive_content_isolated(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        paths_b = tenant_data_paths(sdd, "tenant-b")
        _write_jsonl(paths_a.root / "backlog" / "archive.jsonl", [{"task_id": "t1", "tenant_id": "tenant-a"}])
        _write_jsonl(paths_b.root / "backlog" / "archive.jsonl", [{"task_id": "t2", "tenant_id": "tenant-b"}])
        results = verifier.verify_archive_isolation(sdd, "tenant-a", "tenant-b")
        content_test = [r for r in results if r.name == "archive_tenant_content_isolated"]
        assert content_test[0].passed is True

    def test_archive_cross_leak_detected(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        paths_b = tenant_data_paths(sdd, "tenant-b")
        # Tenant-b record in tenant-a archive
        _write_jsonl(paths_a.root / "backlog" / "archive.jsonl", [{"task_id": "t99", "tenant_id": "tenant-b"}])
        _write_jsonl(paths_b.root / "backlog" / "archive.jsonl", [{"task_id": "t2", "tenant_id": "tenant-b"}])
        results = verifier.verify_archive_isolation(sdd, "tenant-a", "tenant-b")
        content_test = [r for r in results if r.name == "archive_tenant_content_isolated"]
        assert content_test[0].passed is False

    def test_missing_archive_passes(self, verifier: TenantIsolationVerifier, tmp_path: Path) -> None:
        empty_sdd = tmp_path / "empty"
        empty_sdd.mkdir()
        results = verifier.verify_archive_isolation(empty_sdd, "a", "b")
        assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    """Integration: run_all_checks aggregates all verifiers."""

    def test_clean_state_passes(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        report = verifier.run_all_checks(sdd)
        assert report.passed is True
        assert report.failed_count == 0
        assert report.total > 0

    def test_with_task_store(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        store: dict[str, Any] = {
            "t1": StubTask(id="t1", tenant_id="tenant-a"),
            "t2": StubTask(id="t2", tenant_id="tenant-b"),
        }
        report = verifier.run_all_checks(sdd, store=store)
        assert report.passed is True

    def test_report_counts(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        report = verifier.run_all_checks(sdd)
        assert report.total == report.passed_count + report.failed_count

    def test_custom_tenant_names(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        ensure_tenant_data_layout(sdd, "acme")
        ensure_tenant_data_layout(sdd, "globex")
        report = verifier.run_all_checks(sdd, tenant_a="acme", tenant_b="globex")
        assert report.passed is True

    def test_report_is_frozen(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        report = verifier.run_all_checks(sdd)
        with pytest.raises(FrozenInstanceError):
            report.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# render_isolation_report
# ---------------------------------------------------------------------------


class TestRenderIsolationReport:
    """Markdown rendering of IsolationReport."""

    def test_pass_report_header(self) -> None:
        report = IsolationReport(tests=(), total=0, passed_count=0, failed_count=0, passed=True)
        md = render_isolation_report(report)
        assert "PASS" in md
        assert "FAIL" not in md.split("\n")[0]

    def test_fail_report_header(self) -> None:
        t = IsolationTest(name="x", description="d", passed=False, details="bad")
        report = IsolationReport(tests=(t,), total=1, passed_count=0, failed_count=1, passed=False)
        md = render_isolation_report(report)
        assert md.startswith("## Tenant Isolation Report - FAIL")

    def test_table_rows(self) -> None:
        t1 = IsolationTest(name="a", description="desc-a", passed=True, details="ok")
        t2 = IsolationTest(name="b", description="desc-b", passed=False, details="err")
        report = IsolationReport(tests=(t1, t2), total=2, passed_count=1, failed_count=1, passed=False)
        md = render_isolation_report(report)
        lines = md.strip().split("\n")
        table_rows = [ln for ln in lines if ln.startswith("| PASS") or ln.startswith("| FAIL")]
        assert len(table_rows) == 2

    def test_pipe_chars_escaped_in_details(self) -> None:
        t = IsolationTest(name="x", description="d", passed=True, details="a|b|c")
        report = IsolationReport(tests=(t,), total=1, passed_count=1, failed_count=0, passed=True)
        md = render_isolation_report(report)
        assert "a\\|b\\|c" in md

    def test_counts_in_summary(self) -> None:
        t1 = IsolationTest(name="a", description="", passed=True, details="")
        t2 = IsolationTest(name="b", description="", passed=True, details="")
        report = IsolationReport(tests=(t1, t2), total=2, passed_count=2, failed_count=0, passed=True)
        md = render_isolation_report(report)
        assert "**Total:** 2" in md
        assert "**Passed:** 2" in md
        assert "**Failed:** 0" in md


# ---------------------------------------------------------------------------
# Tenant-bearing fields are read independently
# ---------------------------------------------------------------------------


class TestMalformedTenantFieldCannotSilenceASiblingField:
    """A record's tenant fields answer for themselves, one at a time.

    Reading only the first field that happened to be truthy let a `tenant_id`
    that no longer normalizes stand in for the record's answer, hiding a
    `tenant` field that still does. The scan then reported a directory clean
    while a foreign record sat in it.
    """

    def test_metrics_leak_found_behind_a_malformed_tenant_id(
        self, verifier: TenantIsolationVerifier, sdd: Path
    ) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(
            paths_a.metrics_dir / "cost.jsonl",
            [{"cost_usd": 1.0, "tenant_id": "not a tenant id", "tenant": "tenant-b"}],
        )

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert content_test.passed is False
        assert "tenant-b" in content_test.details

    def test_wal_leak_found_behind_a_malformed_tenant_id(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(
            paths_a.wal_dir / "run.jsonl",
            [{"seq": 0, "tenant_id": "not a tenant id", "tenant": "tenant-b"}],
        )

        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")

        wal_test = next(r for r in results if r.name == "wal_content_no_cross_leak")
        assert wal_test.passed is False
        assert "tenant-b" in wal_test.details

    def test_wal_leak_found_behind_a_malformed_actor(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(
            paths_a.wal_dir / "run.jsonl",
            [{"seq": 0, "actor": "not an actor id", "tenant_id": "tenant-b"}],
        )

        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")

        wal_test = next(r for r in results if r.name == "wal_content_no_cross_leak")
        assert wal_test.passed is False


class TestUnreadableTenantIsNotReportedAsClean:
    """A record naming an owner nothing can read is not a verified record.

    It is not contamination either -- it names no tenant, so it cannot be
    pinned on one -- but counting it as passed lets a hand-edited or
    pre-rules row stand in for one the scan actually cleared.
    """

    def test_metrics_record_with_unreadable_tenant_fails_the_check(
        self, verifier: TenantIsolationVerifier, sdd: Path
    ) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(paths_a.metrics_dir / "cost.jsonl", [{"cost_usd": 1.0, "tenant_id": "../escape"}])

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert content_test.passed is False
        assert "unreadable" in content_test.details

    def test_wal_record_with_unreadable_tenant_fails_the_check(
        self, verifier: TenantIsolationVerifier, sdd: Path
    ) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(paths_a.wal_dir / "run.jsonl", [{"seq": 0, "tenant_id": "../escape"}])

        results = verifier.verify_wal_isolation(sdd, "tenant-a", "tenant-b")

        wal_test = next(r for r in results if r.name == "wal_content_no_cross_leak")
        assert wal_test.passed is False
        assert "unreadable" in wal_test.details

    def test_non_string_tenant_id_is_not_coerced_into_a_tenant_name(
        self, verifier: TenantIsolationVerifier, sdd: Path
    ) -> None:
        """`str(True)` is `"True"`, which the identifier rules accept."""
        # The foreign tenant's own directory has to exist, or the check
        # short-circuits before it reads a single record.
        ensure_tenant_data_layout(sdd, "True")
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(paths_a.metrics_dir / "cost.jsonl", [{"cost_usd": 1.0, "tenant_id": True}])

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "True")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert "found in tenant-a dir" not in content_test.details

    def test_absent_and_blank_fields_say_nothing_either_way(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(
            paths_a.metrics_dir / "cost.jsonl",
            [{"cost_usd": 1.0}, {"cost_usd": 2.0, "tenant_id": ""}, {"cost_usd": 3.0, "tenant_id": None}],
        )

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert content_test.passed is True


class TestScansReadTheRecordShapesWritersActuallyProduce:
    """The collector nests the tenant in `labels`, and rows are not all objects."""

    def test_collector_shaped_record_is_attributed(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        """`MetricsCollector` writes the tenant under `labels`, not beside it."""
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(
            paths_a.metrics_dir / "api_usage.jsonl",
            [{"timestamp": 1.0, "value": 2.0, "labels": {"provider": "x", "tenant_id": "tenant-b"}}],
        )

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert content_test.passed is False
        assert "tenant-b" in content_test.details

    def test_unreadable_nested_label_is_reported(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(
            paths_a.metrics_dir / "api_usage.jsonl",
            [{"value": 2.0, "labels": {"tenant_id": "../escape"}}],
        )

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert content_test.passed is False
        assert "labels.tenant_id" in content_test.details

    def test_non_object_rows_do_not_stop_the_archive_scan(self, verifier: TenantIsolationVerifier, sdd: Path) -> None:
        """A JSONL line is valid JSON, not necessarily an object.

        An array or a scalar has no `.get`, and reading one raised past the
        decode handler, ending the scan before the rows that follow it.
        """
        archive_file = sdd / "archive" / "tasks.jsonl"
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file.write_text(
            "[1, 2, 3]\n"
            '"a bare string"\n'
            "null\n"
            "12345\n"
            '{"task_id": "t1", "tenant_id": "tenant-a"}\n'
            '{"task_id": "t1", "tenant_id": "tenant-b"}\n',
            encoding="utf-8",
        )

        results = verifier.verify_archive_isolation(archive_file, "tenant-a", "tenant-b")

        overlap = next(r for r in results if r.name == "archive_no_shared_task_ids")
        assert overlap.passed is False
        assert "t1" in overlap.details

    def test_non_object_rows_do_not_stop_the_tenant_archive_scan(
        self, verifier: TenantIsolationVerifier, sdd: Path
    ) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        paths_b = tenant_data_paths(sdd, "tenant-b")
        tenant_archive = paths_a.root / "backlog" / "archive.jsonl"
        tenant_archive.parent.mkdir(parents=True, exist_ok=True)
        tenant_archive.write_text(
            "[1, 2, 3]\nnull\n" + '{"task_id": "t9", "tenant_id": "tenant-b"}\n',
            encoding="utf-8",
        )
        # The check only runs when both tenants' archives are on disk.
        other_archive = paths_b.root / "backlog" / "archive.jsonl"
        other_archive.parent.mkdir(parents=True, exist_ok=True)
        other_archive.write_text('{"task_id": "t8", "tenant_id": "tenant-b"}\n', encoding="utf-8")

        results = verifier.verify_archive_isolation(sdd, "tenant-a", "tenant-b")

        leak = next(r for r in results if r.name == "archive_tenant_content_isolated")
        assert leak.passed is False


class TestRecordsThatNameNoOwnerReadAsDefault:
    """One reading of a missing tenant, shared with `TaskStore.read_archive`."""

    def test_record_with_no_tenant_field_is_attributed_to_default(
        self, verifier: TenantIsolationVerifier, sdd: Path
    ) -> None:
        ensure_tenant_data_layout(sdd, "default")
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(paths_a.metrics_dir / "cost.jsonl", [{"cost_usd": 1.0}])

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "default")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert content_test.passed is False

    def test_record_with_no_tenant_field_still_passes_an_unrelated_tenant(
        self, verifier: TenantIsolationVerifier, sdd: Path
    ) -> None:
        paths_a = tenant_data_paths(sdd, "tenant-a")
        _write_jsonl(paths_a.metrics_dir / "cost.jsonl", [{"cost_usd": 1.0}])

        results = verifier.verify_cost_isolation(sdd, "tenant-a", "tenant-b")

        content_test = next(r for r in results if r.name == "cost_content_not_cross_contaminated")
        assert content_test.passed is True
