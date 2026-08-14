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
from bernstein.core.security.tenanting import (
    DEFAULT_TENANT_ID,
    normalize_tenant_id,
    try_normalize_tenant_id,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _tenant_fields(record: dict[str, Any], *fields: str) -> tuple[set[str], list[str]]:
    """Split a record's tenant-bearing fields into readable and unreadable.

    Each named field is normalized on its own. Reading only the first field
    that happens to be truthy lets one field silence another: a ``tenant_id``
    that no longer normalizes would hide a ``tenant`` that still does, and a
    scan would report clean for a record it can in fact attribute. Which field
    a writer used is a question about the record's age, not about who owns it.

    Each field is looked for at the top level and inside ``labels``, because
    the metrics collector puts the tenant in the label map rather than beside
    it. A scan that read only the top level saw none of its records' owners.

    A field that is absent, null, or blank states nothing and is skipped. A
    field holding something the tenant rules cannot read is reported instead:
    the record names an owner and the scan cannot say who, which is the case a
    caller must not treat as clean.

    A record that states no owner anywhere is attributed to
    ``DEFAULT_TENANT_ID``. That is the same reading `try_normalize_tenant_id`
    gives an absent value and the same one `TaskStore.read_archive` filters by,
    so a pre-tenancy row is verified rather than skipped by one surface and
    counted by the other.

    Args:
        record: One decoded JSONL record.
        fields: Field names to read, in no particular order.

    Returns:
        The tenant IDs the record can be attributed to, and a rendering of the
        fields that name an owner the rules cannot read.
    """

    labels = record.get("labels")
    sources: list[tuple[str, dict[str, Any]]] = [("", record)]
    if isinstance(labels, dict):
        sources.append(("labels.", labels))

    attributable: set[str] = set()
    unreadable: list[str] = []
    for prefix, source in sources:
        for field in fields:
            if field not in source:
                continue
            value = source[field]
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            normalized = try_normalize_tenant_id(value)
            if normalized is None:
                unreadable.append(f"{prefix}{field}={value!r}")
            else:
                attributable.add(normalized)
    if not attributable and not unreadable:
        attributable.add(DEFAULT_TENANT_ID)
    return attributable, unreadable


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
        """Scan JSONL files in metrics_dir for records not owned by owner_tenant.

        Two things fail the scan. A record that reads as *foreign_tenant* is
        contamination. A record that names an owner the tenant rules cannot
        read is not contamination -- it names no tenant, so it cannot be
        pinned on one -- but it is equally not a record this scan has cleared,
        and reporting it as clean would let a hand-edited or pre-rules row
        stand in for a verified one.

        Returns:
            Whether anything was flagged, and one detail line per flagged
            record.
        """
        contamination_details: list[str] = []
        unreadable_details: list[str] = []
        for fpath in metrics_dir.iterdir():
            if not fpath.is_file():
                continue
            with contextlib.suppress(json.JSONDecodeError, OSError):
                for line in fpath.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    tenants, unreadable = _tenant_fields(record, "tenant_id", "tenant")
                    if foreign_tenant in tenants:
                        contamination_details.append(
                            f"{fpath.name}: record with tenant={foreign_tenant} found in {owner_tenant} dir"
                        )
                    elif unreadable:
                        unreadable_details.append(
                            f"{fpath.name}: record names an unreadable tenant ({', '.join(unreadable)}); "
                            "cannot be attributed"
                        )
        details = contamination_details + unreadable_details
        return bool(details), details

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
                    description=(
                        f"Every record in '{norm_a}' metrics dir names a readable tenant, and none names '{norm_b}'"
                    ),
                    passed=not contaminated,
                    details="; ".join(details) if contaminated else "no cross-contamination",
                )
            )
        else:
            results.append(
                IsolationTest(
                    name="cost_content_not_cross_contaminated",
                    description=(
                        f"Every record in '{norm_a}' metrics dir names a readable tenant, and none names '{norm_b}'"
                    ),
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
                    description=(
                        f"Every entry in '{norm_a}' WAL files names a readable tenant, and none names '{norm_b}'"
                    ),
                    passed=not cross_leak,
                    details=cross_leak or "no cross-tenant WAL entries",
                )
            )
        else:
            results.append(
                IsolationTest(
                    name="wal_content_no_cross_leak",
                    description=(
                        f"Every entry in '{norm_a}' WAL files names a readable tenant, and none names '{norm_b}'"
                    ),
                    passed=True,
                    details="one or both WAL dirs do not exist on disk",
                )
            )

        return results

    @staticmethod
    def _wal_record_belongs_to_tenant(record: dict[str, Any], tenant: str) -> bool:
        """Check if a WAL record belongs to the given tenant.

        Every tenant-bearing field is read on its own, so a value that no
        longer normalizes cannot stand in for the record's answer and hide a
        sibling field that still does.
        """
        tenants, _ = _tenant_fields(record, "actor", "tenant_id", "tenant")
        return tenant in tenants

    @staticmethod
    def _check_wal_content_leak(wal_dir: Path, foreign_tenant: str) -> str:
        """Scan WAL JSONL files in *wal_dir* for entries not owned by this tenant.

        An entry that reads as *foreign_tenant* is a leak. An entry that names
        an owner the tenant rules cannot read is not a leak -- it names no
        tenant -- but it is an entry the scan could not clear, and saying
        nothing about it would report a directory as verified on the strength
        of the rows that happened to be readable.

        Returns:
            An empty string when every entry was read and none was foreign, or
            a description of the first entry that failed either way.
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
                    if not isinstance(record, dict):
                        continue
                    tenants, unreadable = _tenant_fields(record, "actor", "tenant_id", "tenant")
                    if foreign_tenant in tenants:
                        return f"{wal_file.name}: entry belongs to foreign tenant '{foreign_tenant}'"
                    if unreadable:
                        return (
                            f"{wal_file.name}: entry names an unreadable tenant "
                            f"({', '.join(unreadable)}); cannot be attributed"
                        )
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
        with contextlib.suppress(OSError):
            for line in archive_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A JSONL line is valid JSON, not necessarily an object: an
                # array, a scalar or `null` decodes fine and has no `.get`.
                # Reading one raised `AttributeError` past the decode handler
                # and ended the scan, so every later row went unchecked.
                if not isinstance(record, dict):
                    continue
                # Unchanged, not coerced: `str()` on a stored `true` or `123`
                # yields a string the tenant rules accept, which would sort the
                # record into a tenant nothing wrote it for.
                rec_tenant = try_normalize_tenant_id(record.get("tenant_id"))
                if rec_tenant == norm_a:
                    a_records.append(record)
                elif rec_tenant == norm_b:
                    b_records.append(record)

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
                    details=leak or "no cross-tenant archive records",
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
        with contextlib.suppress(OSError):
            for line in archive_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # An array, a scalar or `null` decodes fine and has no `.get`;
                # reading one raised past the decode handler and ended the scan.
                if not isinstance(record, dict):
                    continue
                # Unchanged, not coerced: `str()` on a stored `true` or `123`
                # yields a string the tenant rules accept, which would sort the
                # record into a tenant nothing wrote it for.
                rec_tenant = try_normalize_tenant_id(record.get("tenant_id"))
                if rec_tenant == foreign_tenant:
                    task_id = record.get("task_id", "unknown")
                    return f"task_id={task_id} belongs to foreign tenant '{foreign_tenant}'"
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
        f"## Tenant Isolation Report - {status}",
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
