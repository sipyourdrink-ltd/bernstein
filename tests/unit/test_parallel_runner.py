"""Tests for bernstein.testing.parallel_runner — bucketed parallel execution."""

from __future__ import annotations

from pathlib import Path

from bernstein.testing.parallel_runner import (
    FileTestResult,
    ParallelConfig,
    ParallelRunReport,
    TestBucket,
    bucket_tests,
    build_parallel_report,
    discover_test_files,
    format_parallel_report,
)

# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Verify frozen dataclass construction and defaults."""

    def test_test_bucket_fields(self) -> None:
        bucket = TestBucket(bucket_id=0, test_files=["a.py"], estimated_duration_s=1.5)
        assert bucket.bucket_id == 0
        assert bucket.test_files == ["a.py"]
        assert bucket.estimated_duration_s == 1.5

    def test_parallel_config_defaults(self) -> None:
        cfg = ParallelConfig()
        assert cfg.max_workers == 8
        assert cfg.timeout_per_file == 120
        assert cfg.isolation_mode == "file"
        assert cfg.fail_fast is True

    def test_parallel_config_custom(self) -> None:
        cfg = ParallelConfig(max_workers=4, timeout_per_file=60, isolation_mode="directory", fail_fast=False)
        assert cfg.max_workers == 4
        assert cfg.isolation_mode == "directory"
        assert cfg.fail_fast is False

    def test_file_test_result_fields(self) -> None:
        r = FileTestResult(
            file_path="tests/test_x.py",
            passed=3,
            failed=1,
            skipped=0,
            duration_s=2.5,
            exit_code=1,
            error="AssertionError",
        )
        assert r.passed == 3
        assert r.failed == 1
        assert r.error == "AssertionError"

    def test_file_test_result_no_error(self) -> None:
        r = FileTestResult(
            file_path="t.py",
            passed=1,
            failed=0,
            skipped=0,
            duration_s=0.1,
            exit_code=0,
            error=None,
        )
        assert r.error is None

    def test_parallel_run_report_defaults(self) -> None:
        report = ParallelRunReport(
            total_files=0,
            total_passed=0,
            total_failed=0,
            total_skipped=0,
            wall_time_s=0.0,
            cpu_time_s=0.0,
            speedup=0.0,
        )
        assert report.results == []
        assert report.failures == []


# ---------------------------------------------------------------------------
# discover_test_files
# ---------------------------------------------------------------------------


class TestDiscoverTestFiles:
    """Test file discovery with tmp_path fixtures."""

    def test_discovers_matching_files(self, tmp_path: Path) -> None:
        (tmp_path / "test_alpha.py").write_text("# test")
        (tmp_path / "test_beta.py").write_text("# test")
        (tmp_path / "helper.py").write_text("# not a test")
        result = discover_test_files(tmp_path)
        assert len(result) == 2
        assert all("test_" in p for p in result)

    def test_returns_sorted(self, tmp_path: Path) -> None:
        (tmp_path / "test_zeta.py").write_text("")
        (tmp_path / "test_alpha.py").write_text("")
        result = discover_test_files(tmp_path)
        assert result[0].endswith("test_alpha.py")
        assert result[1].endswith("test_zeta.py")

    def test_custom_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "check_foo.py").write_text("")
        (tmp_path / "test_foo.py").write_text("")
        result = discover_test_files(tmp_path, pattern="check_*.py")
        assert len(result) == 1
        assert "check_foo" in result[0]

    def test_empty_directory(self, tmp_path: Path) -> None:
        result = discover_test_files(tmp_path)
        assert result == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        result = discover_test_files(tmp_path / "no_such_dir")
        assert result == []


# ---------------------------------------------------------------------------
# bucket_tests
# ---------------------------------------------------------------------------


class TestBucketTests:
    """Test file distribution across buckets."""

    def test_even_distribution_no_history(self) -> None:
        files = [f"test_{i}.py" for i in range(6)]
        buckets = bucket_tests(files, num_buckets=3)
        assert len(buckets) == 3
        total_files = sum(len(b.test_files) for b in buckets)
        assert total_files == 6
        # Each bucket should have 2 files (uniform default duration)
        for b in buckets:
            assert len(b.test_files) == 2

    def test_single_bucket(self) -> None:
        files = ["a.py", "b.py", "c.py"]
        buckets = bucket_tests(files, num_buckets=1)
        assert len(buckets) == 1
        assert len(buckets[0].test_files) == 3

    def test_more_buckets_than_files(self) -> None:
        files = ["a.py", "b.py"]
        buckets = bucket_tests(files, num_buckets=5)
        assert len(buckets) == 5
        non_empty = [b for b in buckets if b.test_files]
        assert len(non_empty) == 2
        total = sum(len(b.test_files) for b in buckets)
        assert total == 2

    def test_empty_files(self) -> None:
        buckets = bucket_tests([], num_buckets=3)
        assert len(buckets) == 3
        assert all(b.test_files == [] for b in buckets)

    def test_history_aware_balancing(self) -> None:
        files = ["slow.py", "fast1.py", "fast2.py", "fast3.py"]
        history = {"slow.py": 10.0, "fast1.py": 1.0, "fast2.py": 1.0, "fast3.py": 1.0}
        buckets = bucket_tests(files, num_buckets=2, history=history)
        # The slow file should be alone in one bucket
        slow_bucket = next(b for b in buckets if "slow.py" in b.test_files)
        assert len(slow_bucket.test_files) == 1
        assert slow_bucket.estimated_duration_s == 10.0
        # The other bucket gets the three fast files
        fast_bucket = next(b for b in buckets if "slow.py" not in b.test_files)
        assert len(fast_bucket.test_files) == 3
        assert fast_bucket.estimated_duration_s == 3.0

    def test_bucket_ids_sequential(self) -> None:
        files = ["a.py", "b.py", "c.py"]
        buckets = bucket_tests(files, num_buckets=3)
        assert [b.bucket_id for b in buckets] == [0, 1, 2]

    def test_num_buckets_clamped_to_one(self) -> None:
        files = ["a.py"]
        buckets = bucket_tests(files, num_buckets=0)
        assert len(buckets) == 1
        assert buckets[0].test_files == ["a.py"]

    def test_partial_history(self) -> None:
        files = ["known.py", "unknown.py"]
        history = {"known.py": 5.0}
        buckets = bucket_tests(files, num_buckets=2, history=history)
        # known.py (5.0s) goes to one bucket, unknown.py (default 1.0s) to the other
        total = sum(b.estimated_duration_s for b in buckets)
        assert total == 6.0


# ---------------------------------------------------------------------------
# build_parallel_report
# ---------------------------------------------------------------------------


class TestBuildParallelReport:
    """Test report aggregation logic."""

    def _make_result(
        self,
        path: str = "t.py",
        passed: int = 1,
        failed: int = 0,
        skipped: int = 0,
        duration: float = 1.0,
        exit_code: int = 0,
    ) -> FileTestResult:
        return FileTestResult(
            file_path=path,
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_s=duration,
            exit_code=exit_code,
            error=None if exit_code == 0 else "fail",
        )

    def test_all_passing(self) -> None:
        results = [self._make_result(f"t{i}.py", passed=2, duration=1.0) for i in range(4)]
        report = build_parallel_report(results, wall_time=1.5)
        assert report.total_files == 4
        assert report.total_passed == 8
        assert report.total_failed == 0
        assert report.failures == []
        assert report.cpu_time_s == 4.0
        assert report.wall_time_s == 1.5
        assert report.speedup == round(4.0 / 1.5, 2)

    def test_with_failures(self) -> None:
        results = [
            self._make_result("ok.py", passed=1),
            self._make_result("bad.py", passed=0, failed=2, exit_code=1),
        ]
        report = build_parallel_report(results, wall_time=1.0)
        assert report.total_failed == 2
        assert report.failures == ["bad.py"]

    def test_empty_results(self) -> None:
        report = build_parallel_report([], wall_time=0.0)
        assert report.total_files == 0
        assert report.speedup == 0.0

    def test_skipped_counted(self) -> None:
        results = [self._make_result("s.py", passed=0, skipped=5)]
        report = build_parallel_report(results, wall_time=0.5)
        assert report.total_skipped == 5


# ---------------------------------------------------------------------------
# format_parallel_report
# ---------------------------------------------------------------------------


class TestFormatParallelReport:
    """Test human-readable report formatting."""

    def test_format_contains_key_info(self) -> None:
        report = ParallelRunReport(
            total_files=10,
            total_passed=8,
            total_failed=1,
            total_skipped=1,
            wall_time_s=5.0,
            cpu_time_s=20.0,
            speedup=4.0,
            results=[],
            failures=["bad.py"],
        )
        text = format_parallel_report(report)
        assert "Files:   10" in text
        assert "Passed:  8" in text
        assert "Failed: 1" in text
        assert "Skipped: 1" in text
        assert "Wall:    5.0s" in text
        assert "CPU:     20.0s" in text
        assert "Speedup: 4.00x" in text
        assert "FAILURES (1):" in text
        assert "bad.py" in text

    def test_format_no_failures(self) -> None:
        report = ParallelRunReport(
            total_files=3,
            total_passed=3,
            total_failed=0,
            total_skipped=0,
            wall_time_s=1.0,
            cpu_time_s=3.0,
            speedup=3.0,
        )
        text = format_parallel_report(report)
        assert "FAILURES" not in text
        assert "Speedup: 3.00x" in text
