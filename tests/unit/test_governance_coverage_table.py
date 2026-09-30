"""The governance coverage table cites tests that exist and prove what the row claims.

Issue #5569. The table maps each failure class an agentic workload exhibits to the control
that answers it, where that control is enforced, and the test that proves it.

The interesting column is the one that rots. A row citing a test that was renamed, moved or
deleted still reads as an assurance, and nothing about it looks wrong -- which is worse than
no table, because a stale assurance is read as a current one. So the citations are checked
here, by collecting them, rather than reviewed by eye.

The second guard is the one that keeps the table honest rather than flattering: a row may not
say ``covered`` without a test. The point of the table is the rows that say ``partial`` and
``none``; a table that could only say ``covered`` would be a feature list.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts.gen_governance_coverage import (
    REPORT_PATH,
    SOURCE_PATH,
    STATUS_REQUIRING_TEST,
    CoverageError,
    Row,
    load_rows,
    render_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def rows() -> tuple[Row, ...]:
    return load_rows(REPO_ROOT / SOURCE_PATH)


def test_the_table_has_rows(rows: tuple[Row, ...]) -> None:
    """A guard over an empty table passes for the wrong reason."""
    assert len(rows) >= 10


def test_coverage_table_cites_only_existing_tests(rows: tuple[Row, ...]) -> None:
    """Every cited node id is collectable.

    One `--collect-only` run over the whole set rather than one per row: pytest startup
    dominates, and a single failure names the row that broke either way.
    """
    cited = [row.test for row in rows if row.test is not None]
    assert cited, "no row cites a test; the guard has lost its subject"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header", *cited],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"a row in docs/governance/coverage.yaml cites a test that does not collect:\n{result.stdout}\n{result.stderr}"
    )


def test_no_row_is_covered_without_a_test(rows: tuple[Row, ...]) -> None:
    """A control nothing proves is `partial` at best."""
    unproven = [row.failure_class for row in rows if row.status == STATUS_REQUIRING_TEST and row.test is None]

    assert unproven == [], f"these rows claim coverage with no test: {unproven}"


def test_every_cited_module_exists(rows: tuple[Row, ...]) -> None:
    """A control enforced in a file that is gone is not enforced."""
    missing = [row.module for row in rows if not (REPO_ROOT / row.module).is_file()]

    assert missing == [], f"these rows name a module that does not exist: {missing}"


def test_a_test_is_cited_in_the_file_it_names(rows: tuple[Row, ...]) -> None:
    """The node id's path and its test file must be the same file.

    Collection would catch a path that does not exist at all. It would not catch a row whose
    node id drifted onto a neighbouring file that happens to define a same-named test.
    """
    for row in rows:
        if row.test is None:
            continue
        path, _, selector = row.test.partition("::")
        assert selector, f"{row.failure_class!r} cites {row.test!r} with no test selector"
        name = selector.rsplit("::", 1)[-1]
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert f"def {name}(" in source, f"{path} does not define {name}"


def test_every_uncovered_row_links_the_issue_that_closes_it(rows: tuple[Row, ...]) -> None:
    """Prose about why a row is partial rots in place; an issue moves when the work does."""
    unlinked = [row.failure_class for row in rows if row.status == "none" and row.issue is None]

    assert unlinked == [], f"these rows report no control and link no issue: {unlinked}"


def test_every_row_states_its_residual_risk(rows: tuple[Row, ...]) -> None:
    """Residual risk is the part the operator carries; a row without it overclaims."""
    silent = [row.failure_class for row in rows if len(row.residual) < 20]

    assert silent == [], f"these rows state no meaningful residual risk: {silent}"


def test_coverage_md_regenerates_byte_identically(rows: tuple[Row, ...]) -> None:
    """The rendered file and the YAML have to be the same document."""
    assert (REPO_ROOT / REPORT_PATH).read_text(encoding="utf-8") == render_report(rows)


def test_a_covered_row_without_a_test_is_refused_at_load(tmp_path: Path) -> None:
    """The generator refuses it too, so the guard holds on a run that skips the test suite."""
    source = tmp_path / "coverage.yaml"
    source.write_text(
        "rows:\n"
        "  - failure_class: Something\n"
        "    control: A control\n"
        "    module: README.md\n"
        "    test: null\n"
        "    residual: Everything else, which is a long enough sentence.\n"
        "    status: covered\n"
        "    issue: null\n",
        encoding="utf-8",
    )

    with pytest.raises(CoverageError, match="no test"):
        load_rows(source)


def test_an_unknown_status_is_refused_at_load(tmp_path: Path) -> None:
    """Three answers, not a free-form string that can quietly become a fourth."""
    source = tmp_path / "coverage.yaml"
    source.write_text(
        "rows:\n"
        "  - failure_class: Something\n"
        "    control: A control\n"
        "    module: README.md\n"
        "    test: tests/unit/test_governance_coverage_table.py::test_the_table_has_rows\n"
        "    residual: Everything else, which is a long enough sentence.\n"
        "    status: mostly\n"
        "    issue: null\n",
        encoding="utf-8",
    )

    with pytest.raises(CoverageError, match="unknown status"):
        load_rows(source)
