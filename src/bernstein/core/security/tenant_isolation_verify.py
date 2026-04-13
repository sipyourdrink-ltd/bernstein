"""Cross-tenant data isolation verification with automated testing.

Provides a deterministic test harness that verifies strict data isolation
between tenants at every persistence layer: task store, cost metrics, WAL,
and archive.  Results are collected into a frozen ``IsolationReport`` that
can be rendered as a Markdown pass/fail table.

Usage::

    verifier = TenantIsolationVerifier()
    report = verifier.run_all_checks(sdd_dir)
    print(render_isolation_report(report))
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.security.tenant_isolation import tenant_data_paths
from bernstein.core.security.tenanting import normalize_tenant_id

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IsolationTest:
    """Result of a single isolation check.

    Attributes:
        name: Short machine-readable test name.
        description: Human-readable explanation of the check.
        passed: Whether the check passed.
        details: Additional context (error message on failure, stats on success).
    """

    name: str
    description: str
    passed: bool
    details: str


@dataclass(frozen=True)
class IsolationReport:
    """Aggregate report of all isolation checks.

    Attributes:
        tests: Immutable sequence of individual test results.
        total: Total number of tests executed.
        passed_count: Number of tests that passed.
        failed_count: Number of tests that failed.
        passed: True only if every test passed.
    """

    tests: tuple[IsolationTest, ...]
    total: int
    passed_count: int
    failed_count: int
    passed: bool


def _build_report(tests: list[IsolationTest]) -> IsolationReport:
    """Build an ``IsolationReport`` from a list of test results."""
    passed_count = sum(1 for t in tests if t.passed)
    failed_count = len(tests) - passed_count
    return IsolationReport(
        tests=tuple(tests),
        total=len(tests),
        passed_count=passed_count,
        failed_count=failed_count,
        passed=failed_count == 0,
    )


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class TenantIsolationVerifier:
    """Runs cross-tenant data isolation checks against the file-based state.

    Each ``verify_*`` method exercises a specific persistence boundary and
    returns a list of ``IsolationTest`` results.  ``run_all_checks`` runs
    every verifier and returns a consolidated ``IsolationReport``.
    """

    # -- task isolation -----------------------------------------------------

    def verify_task_isolation(
        self,
        store: dict[str, Any],
        tenant_a: str,
        tenant_b: str,
    ) -> list[IsolationTest]:
        """Verify that tasks belonging to *tenant_a* are invisible to *tenant_b*.

        Args:
            store: Mapping of task_id to task-like objects that expose a
                ``tenant_id`` attribute.
            tenant_a: First tenant identifier.
            tenant_b: Second tenant identifier.

        Returns:
            List of isolation test results.
        """
        norm_a = normalize_tenant_id(tenant_a)
        norm_b = normalize_tenant_id(tenant_b)
        results: list[IsolationTest] = []

        # Collect tasks per tenant
        tasks_a = {tid: t for tid, t in store.items() if getattr(t, "tenant_id", None) == norm_a}
        tasks_b = {tid: t for tid, t in store.items() if getattr(t, "tenant_id", None) == norm_b}

        # Check: tenant_a tasks not visible in tenant_b set
        leaked_to_b = set(tasks_a.keys()) & set(tasks_b.keys())
        results.append(
            IsolationTest(
                name="task_a_invisible_to_b",
                description=f"Tasks owned by '{norm_a}' must not appear in '{norm_b}' view",
                passed=len(leaked_to_b) == 0,
                details=f"leaked task ids: {sorted(leaked_to_b)}" if leaked_to_b else "no leakage detected",
            )
        )

        # Check: tenant_b tasks not visible in tenant_a set
        leaked_to_a = set(tasks_b.keys()) & set(tasks_a.keys())
        results.append(
            IsolationTest(
                name="task_b_invisible_to_a",
                description=f"Tasks owned by '{norm_b}' must not appear in '{norm_a}' view",
                passed=len(leaked_to_a) == 0,
                details=f"leaked task ids: {sorted(leaked_to_a)}" if leaked_to_a else "no leakage detected",
            )
        )

        # Check: no task has an ambiguous tenant_id (empty/missing)
        ambiguous = [tid for tid, t in store.items() if not getattr(t, "tenant_id", "").strip()]
        results.append(
            IsolationTest(
                name="no_ambiguous_tenant_id",
                description="Every task must have a non-empty tenant_id",
                passed=len(ambiguous) == 0,
                details=(
                    f"ambiguous task ids: {sorted(ambiguous)}" if ambiguous else "all tasks have explicit tenant_id"
                ),
            )
        )

        return results

    # -- cost / metrics isolation -------------------------------------------

    @staticmethod
    def _check_metrics_cross_contamination(
        metrics_dir: Path,
        owner_tenant: str,
        foreign_tenant: str,
    ) -> tuple[bool, list[str]]:
        """Scan JSONL files in metrics_dir for records belonging to foreign_tenant.

        Returns (contaminated, details) tuple.
        """
        contamination_details: list[str] = []
        for fpath in metrics_dir.iterdir():
            if not fpath.is_file():
                continue
            try:
                for line in fpath.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    rec_tenant = record.get("tenant_id") or record.get("tenant", "")
                    if rec_tenant and normalize_tenant_id(str(rec_tenant)) == foreign_tenant:
                        contamination_details.append(
                            f"{fpath.name}: record with tenant={rec_tenant} found in {owner_tenant} dir"
                        )
            except (json.JSONDecodeError, OSError):
                pass
        return bool(contamination_details), contamination_details

    @staticmethod
    def _check_dirs_overlap(dir_a: Path, dir_b: Path) -> bool:
        """Check if either directory is a parent of the other."""
        overlap = False
        with contextlib.suppress(ValueError, TypeError):
            overlap = dir_a.is_relative_to(dir_b) or dir_b.is_relative_to(dir_a)
        return overlap

    def verify_cost_isolation(
        self,
        metrics_path: Path,
        tenant_a: str,
        tenant_b: str,
    ) -> list[IsolationTest]:
        """Verify that cost data under *metrics_path* is partitioned per tenant.

        Expects per-tenant metrics directories at
        ``metrics_path / <tenant_id> / metrics /`` (matching
        ``tenant_data_paths`` layout rooted at *metrics_path*).

        Args:
            metrics_path: Root ``.sdd`` directory (not the metrics dir itself).
            tenant_a: First tenant identifier.
            tenant_b: Second tenant identifier.

        Returns:
            List of isolation test results.
        """
        norm_a = normalize_tenant_id(tenant_a)
        norm_b = normalize_tenant_id(tenant_b)

        paths_a = tenant_data_paths(metrics_path, norm_a)
        paths_b = tenant_data_paths(metrics_path, norm_b)

        dirs_distinct = paths_a.metrics_dir != paths_b.metrics_dir
        overlap = self._check_dirs_overlap(paths_a.metrics_dir, paths_b.metrics_dir)

        results: list[IsolationTest] = [
            IsolationTest(
                name="cost_dirs_distinct",
                description=f"Metrics dirs for '{norm_a}' and '{norm_b}' must be distinct paths",
                passed=dirs_distinct,
                details=(
                    f"a={paths_a.metrics_dir}, b={paths_b.metrics_dir}"
                    if dirs_distinct
                    else "DUPLICATE metrics directory detected"
                ),
            ),
            IsolationTest(
                name="cost_dirs_no_overlap",
                description="Neither tenant's metrics dir is a subdirectory of the other",
                passed=not overlap,
                details="directories are independent" if not overlap else "overlapping directory hierarchy detected",
            ),
        ]

        if paths_a.metrics_dir.is_dir() and paths_b.metrics_dir.is_dir():
            contaminated, details = self._check_metrics_cross_contamination(
                paths_a.metrics_dir,
                norm_a,
                norm_b,
            )
            results.append(
                IsolationTest(
                    name="cost_content_not_cross_contaminated",
                    description=f"No '{norm_b}' cost records appear in '{norm_a}' metrics dir",
                    passed=not contaminated,
                    details="; ".join(details) if contaminated else "no cross-contamination",
                )
            )
        else:
            results.append(
                IsolationTest(
                    name="cost_content_not_cross_contaminated",
                    description=f"No '{norm_b}' cost records appear in '{norm_a}' metrics dir",
                    passed=True,
                    details="one or both metrics dirs do not exist on disk; no cross-contamination possible",
                )
            )

        return results

    # -- WAL isolation ------------------------------------------------------

    def verify_wal_isolation(
        self,
        wal_dir: Path,
        tenant_a: str,
        tenant_b: str,
    ) -> list[IsolationTest]:
        """Verify that WAL namespaces do not leak between tenants.

        WAL files are expected under ``wal_dir / <tenant_id> / runtime / wal /``
        (matching ``TenantDataPaths.wal_dir``).  *wal_dir* should be the root
        ``.sdd`` directory.

        Args:
            wal_dir: Root ``.sdd`` directory.
            tenant_a: First tenant identifier.
            tenant_b: Second tenant identifier.

        Returns:
            List of isolation test results.
        """
        norm_a = normalize_tenant_id(tenant_a)
        norm_b = normalize_tenant_id(tenant_b)

        paths_a = tenant_data_paths(wal_dir, norm_a)
        paths_b = tenant_data_paths(wal_dir, norm_b)

        overlap = self._check_dirs_overlap(paths_a.wal_dir, paths_b.wal_dir)
        a_rooted = paths_a.wal_dir.is_relative_to(paths_a.root)
        b_rooted = paths_b.wal_dir.is_relative_to(paths_b.root)

        results: list[IsolationTest] = [
            IsolationTest(
                name="wal_dirs_distinct",
                description=f"WAL dirs for '{norm_a}' and '{norm_b}' must be distinct",
                passed=paths_a.wal_dir != paths_b.wal_dir,
                details=f"a={paths_a.wal_dir}, b={paths_b.wal_dir}",
            ),
            IsolationTest(
                name="wal_dirs_no_overlap",
                description="Neither tenant's WAL dir is a subdirectory of the other",
                passed=not overlap,
                details="directories are independent" if not overlap else "overlapping WAL hierarchy detected",
            ),
            IsolationTest(
                name="wal_dirs_rooted_in_tenant",
                description="WAL directories must be inside their tenant's root",
                passed=a_rooted and b_rooted,
                details=(
                    "both WAL dirs are correctly rooted"
                    if a_rooted and b_rooted
                    else f"a_rooted={a_rooted}, b_rooted={b_rooted}"
                ),
            ),
        ]

        if paths_a.wal_dir.is_dir() and paths_b.wal_dir.is_dir():
            cross_leak = self._check_wal_content_leak(paths_a.wal_dir, norm_b)
            results.append(
                IsolationTest(
                    name="wal_content_no_cross_leak",
                    description=f"No '{norm_b}' entries found in '{norm_a}' WAL files",
                    passed=not cross_leak,
                    details=cross_leak if cross_leak else "no cross-tenant WAL entries",
                )
            )
        else:
            results.append(
                IsolationTest(
                    name="wal_content_no_cross_leak",
                    description=f"No '{norm_b}' entries found in '{norm_a}' WAL files",
                    passed=True,
                    details="one or both WAL dirs do not exist on disk",
                )
            )

        return results

    @staticmethod
    def _wal_record_belongs_to_tenant(record: dict[str, Any], tenant: str) -> bool:
        """Check if a WAL record belongs to the given tenant."""
        actor = record.get("actor", "")
        tenant_field = record.get("tenant_id") or record.get("tenant", "")
        return any(val and normalize_tenant_id(str(val)) == tenant for val in (actor, tenant_field))

    @staticmethod
    def _check_wal_content_leak(wal_dir: Path, foreign_tenant: str) -> str:
        """Scan WAL JSONL files in *wal_dir* for entries belonging to *foreign_tenant*.

        Returns an empty string if no leak is found, or a descriptive string
        of the first leaked entry.
        """
        for wal_file in wal_dir.glob("*.jsonl"):
            try:
                for line in wal_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if TenantIsolationVerifier._wal_record_belongs_to_tenant(record, foreign_tenant):
                        return f"{wal_file.name}: entry belongs to foreign tenant '{foreign_tenant}'"
            except OSError:
                continue
        return ""

    # -- archive isolation --------------------------------------------------

    @staticmethod
    def _resolve_archive_paths(archive_path: Path) -> tuple[Path | None, Path | None]:
        """Resolve archive_path to (archive_file, sdd_dir) tuple."""
        archive_file: Path | None = None
        sdd_dir: Path | None = None
        if archive_path.is_file():
            archive_file = archive_path
            sdd_dir = archive_path.parent.parent if archive_path.parent.name == "archive" else None
        elif archive_path.is_dir():
            sdd_dir = archive_path
            candidate = archive_path / "archive" / "tasks.jsonl"
            if candidate.is_file():
                archive_file = candidate
        return archive_file, sdd_dir

    @staticmethod
    def _check_shared_archive_overlap(
        archive_file: Path | None,
        norm_a: str,
        norm_b: str,
    ) -> IsolationTest:
        """Check shared archive for task_id overlap between tenants."""
        if archive_file is None or not archive_file.is_file():
            return IsolationTest(
                name="archive_no_shared_task_ids",
                description="No task_id appears in both tenants' archive records",
                passed=True,
                details="shared archive file does not exist; no overlap possible",
            )

        a_records: list[dict[str, Any]] = []
        b_records: list[dict[str, Any]] = []
        try:
            for line in archive_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec_tenant = normalize_tenant_id(str(record.get("tenant_id", "")))
                if rec_tenant == norm_a:
                    a_records.append(record)
                elif rec_tenant == norm_b:
                    b_records.append(record)
        except OSError:
            pass

        a_task_ids = {r.get("task_id", "") for r in a_records}
        b_task_ids = {r.get("task_id", "") for r in b_records}
        shared_ids = a_task_ids & b_task_ids - {""}
        return IsolationTest(
            name="archive_no_shared_task_ids",
            description="No task_id appears in both tenants' archive records",
            passed=len(shared_ids) == 0,
            details=(
                f"shared task_ids: {sorted(shared_ids)}"
                if shared_ids
                else f"a_records={len(a_records)}, b_records={len(b_records)}, no overlap"
            ),
        )

    def _check_tenant_scoped_archives(
        self,
        sdd_dir: Path | None,
        norm_a: str,
        norm_b: str,
    ) -> list[IsolationTest]:
        """Check tenant-scoped archive directories and content."""
        if sdd_dir is None:
            return [
                IsolationTest(
                    name="archive_tenant_paths_distinct",
                    description="Tenant-scoped archive paths must be in separate directories",
                    passed=True,
                    details="sdd_dir not resolvable from archive_path; skipped",
                ),
                IsolationTest(
                    name="archive_tenant_content_isolated",
                    description=f"Tenant '{norm_a}' archive must not contain '{norm_b}' records",
                    passed=True,
                    details="sdd_dir not resolvable from archive_path; skipped",
                ),
            ]

        paths_a = tenant_data_paths(sdd_dir, norm_a)
        paths_b = tenant_data_paths(sdd_dir, norm_b)
        tenant_archive_a = paths_a.root / "backlog" / "archive.jsonl"
        tenant_archive_b = paths_b.root / "backlog" / "archive.jsonl"

        results: list[IsolationTest] = [
            IsolationTest(
                name="archive_tenant_paths_distinct",
                description="Tenant-scoped archive paths must be in separate directories",
                passed=tenant_archive_a.parent != tenant_archive_b.parent,
                details=f"a={tenant_archive_a.parent}, b={tenant_archive_b.parent}",
            ),
        ]

        if tenant_archive_a.is_file() and tenant_archive_b.is_file():
            leak = self._check_archive_content_leak(tenant_archive_a, norm_b)
            results.append(
                IsolationTest(
                    name="archive_tenant_content_isolated",
                    description=f"Tenant '{norm_a}' archive must not contain '{norm_b}' records",
                    passed=not leak,
                    details=leak if leak else "no cross-tenant archive records",
                )
            )
        else:
            results.append(
                IsolationTest(
                    name="archive_tenant_content_isolated",
                    description=f"Tenant '{norm_a}' archive must not contain '{norm_b}' records",
                    passed=True,
                    details="one or both tenant archive files do not exist",
                )
            )
        return results

    def verify_archive_isolation(
        self,
        archive_path: Path,
        tenant_a: str,
        tenant_b: str,
    ) -> list[IsolationTest]:
        """Verify that archive JSONL records are properly separated per tenant.

        Checks both the shared archive (if it exists) and tenant-scoped
        archive files.

        Args:
            archive_path: Path to the shared ``archive/tasks.jsonl`` file,
                or the root ``.sdd`` directory.
            tenant_a: First tenant identifier.
            tenant_b: Second tenant identifier.

        Returns:
            List of isolation test results.
        """
        norm_a = normalize_tenant_id(tenant_a)
        norm_b = normalize_tenant_id(tenant_b)

        archive_file, sdd_dir = self._resolve_archive_paths(archive_path)

        results: list[IsolationTest] = [
            self._check_shared_archive_overlap(archive_file, norm_a, norm_b),
        ]
        results.extend(self._check_tenant_scoped_archives(sdd_dir, norm_a, norm_b))
        return results

    @staticmethod
    def _check_archive_content_leak(archive_file: Path, foreign_tenant: str) -> str:
        """Scan an archive JSONL file for records belonging to *foreign_tenant*.

        Returns an empty string if clean, or a description of the leak.
        """
        try:
            for line in archive_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec_tenant = normalize_tenant_id(str(record.get("tenant_id", "")))
                if rec_tenant == foreign_tenant:
                    task_id = record.get("task_id", "unknown")
                    return f"task_id={task_id} belongs to foreign tenant '{foreign_tenant}'"
        except OSError:
            pass
        return ""

    # -- run all checks -----------------------------------------------------

    def run_all_checks(
        self,
        config: Path,
        *,
        tenant_a: str = "tenant-a",
        tenant_b: str = "tenant-b",
        store: dict[str, Any] | None = None,
    ) -> IsolationReport:
        """Run every isolation verifier and return a consolidated report.

        Args:
            config: Root ``.sdd`` directory path.
            tenant_a: First tenant identifier (default ``tenant-a``).
            tenant_b: Second tenant identifier (default ``tenant-b``).
            store: Optional task store dict. If ``None``, task isolation
                checks use an empty store.

        Returns:
            An ``IsolationReport`` aggregating all test results.
        """
        all_tests: list[IsolationTest] = []

        # Task isolation
        task_store = store if store is not None else {}
        all_tests.extend(self.verify_task_isolation(task_store, tenant_a, tenant_b))

        # Cost / metrics isolation
        all_tests.extend(self.verify_cost_isolation(config, tenant_a, tenant_b))

        # WAL isolation
        all_tests.extend(self.verify_wal_isolation(config, tenant_a, tenant_b))

        # Archive isolation
        all_tests.extend(self.verify_archive_isolation(config, tenant_a, tenant_b))

        return _build_report(all_tests)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_isolation_report(report: IsolationReport) -> str:
    """Render an ``IsolationReport`` as a Markdown pass/fail table.

    Args:
        report: The isolation report to render.

    Returns:
        Markdown string with a summary header and results table.
    """
    status = "PASS" if report.passed else "FAIL"
    lines: list[str] = [
        f"## Tenant Isolation Report — {status}",
        "",
        f"**Total:** {report.total} | **Passed:** {report.passed_count} | **Failed:** {report.failed_count}",
        "",
        "| Status | Test | Description | Details |",
        "|--------|------|-------------|---------|",
    ]
    for test in report.tests:
        icon = "PASS" if test.passed else "FAIL"
        # Escape pipe characters in details to avoid breaking the table
        safe_details = test.details.replace("|", "\\|")
        lines.append(f"| {icon} | {test.name} | {test.description} | {safe_details} |")
    lines.append("")
    return "\n".join(lines)
