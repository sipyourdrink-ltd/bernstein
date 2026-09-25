#!/usr/bin/env python3
"""Generate the governance control-coverage table from its YAML source.

Issue #5569. Controls are documented one at a time, so an operator evaluating
the layer for a workload has to reconstruct which failure class each one
prevents -- and nothing states which classes have no control at all.

The table is generated rather than written because the interesting column is
the one that rots: a row cites the test that proves its control, and a cited
test that no longer exists turns the whole table into a claim nobody checked.
`--check` in CI is what keeps the rendered file and the YAML the same document.

Usage::

    uv run python scripts/gen_governance_coverage.py --update
    uv run python scripts/gen_governance_coverage.py --check
"""

from __future__ import annotations

import argparse
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

SOURCE_PATH = Path("docs/governance/coverage.yaml")
REPORT_PATH = Path("docs/governance/coverage.md")
ISSUE_URL = "https://github.com/sipyourdrink-ltd/bernstein/issues"

#: The three answers a row may give. `partial` and `none` are the rows worth
#: reading: a table that could only say `covered` would be a feature list.
STATUSES = ("covered", "partial", "none")

#: A row saying this needs a test. Enforced here and again in
#: tests/unit/test_governance_coverage_table.py, because the generator is not
#: run on every change and the guard has to hold when it is not.
STATUS_REQUIRING_TEST = "covered"


class CoverageError(ValueError):
    """Raised when the coverage source cannot be rendered as written."""


@dataclass(frozen=True)
class Row:
    """One failure class and what answers it.

    Attributes:
        failure_class: The failure an agentic workload exhibits.
        control: What stands in its way, in one sentence.
        module: Where the control is enforced, as a repository-relative path.
        test: The test that FAILS WHEN THE CONTROL IS REMOVED, as a pytest node
            id. None only on a row that does not claim to be covered.
        residual: What the control does not cover. Not a softener -- the part an
            operator has to carry themselves.
        status: One of :data:`STATUSES`.
        issue: The issue that closes the gap, for a row that is not covered.
    """

    failure_class: str
    control: str
    module: str
    test: str | None
    residual: str
    status: str
    issue: int | None


def _text(raw: dict[str, Any], key: str) -> str:
    """A required, non-empty string field, with the whitespace a YAML fold leaves."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CoverageError(f"row {raw.get('failure_class')!r} is missing {key!r}")
    return " ".join(value.split())


def load_rows(path: Path = SOURCE_PATH) -> tuple[Row, ...]:
    """Parse the source, rejecting a row that cannot mean what it says.

    Raises:
        CoverageError: A row omits a required field, names a status outside the
            three, or claims `covered` without citing a test.
    """
    document = cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))
    raw_rows = document.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise CoverageError(f"{path} declares no rows")

    rows: list[Row] = []
    for raw in cast("list[dict[str, Any]]", raw_rows):
        status = _text(raw, "status")
        if status not in STATUSES:
            raise CoverageError(f"unknown status {status!r}: must be one of {list(STATUSES)}")
        test_value = raw.get("test")
        test = " ".join(str(test_value).split()) if isinstance(test_value, str) and test_value.strip() else None
        if status == STATUS_REQUIRING_TEST and test is None:
            raise CoverageError(
                f"row {raw.get('failure_class')!r} says {status!r} with no test; "
                "a control nothing proves is `partial` at best"
            )
        issue = raw.get("issue")
        rows.append(
            Row(
                failure_class=_text(raw, "failure_class"),
                control=_text(raw, "control"),
                module=_text(raw, "module"),
                test=test,
                residual=_text(raw, "residual"),
                status=status,
                issue=None if issue is None else int(cast("int", issue)),
            )
        )
    return tuple(rows)


def _cell(value: str) -> str:
    """Escape a table cell so a pipe in a path or a sentence cannot split the row."""
    return value.replace("|", "\\|")


def _status_cell(row: Row) -> str:
    """The status, carrying the link that closes the gap when there is one.

    The link rather than a justification column, deliberately: prose about why a
    row is partial goes stale in place, and an issue moves when the work does.
    """
    if row.issue is None:
        return f"`{row.status}`"
    return f"`{row.status}` ([#{row.issue}]({ISSUE_URL}/{row.issue}))"


def render_report(rows: tuple[Row, ...]) -> str:
    """Render the markdown table and the summary above it."""
    counts = {status: sum(1 for row in rows if row.status == status) for status in STATUSES}
    lines: list[str] = [
        "# Governance control coverage",
        "",
        (
            "<!-- AUTO-GENERATED from coverage.yaml: run "
            "`uv run python scripts/gen_governance_coverage.py --update` to refresh -->"
        ),
        "",
        "Which failure class each control prevents, where it is enforced, and the test that proves",
        "it. Edit `docs/governance/coverage.yaml`, not this file.",
        "",
        "Two things this table is for. The first is the ordinary one: an operator evaluating the",
        "layer for a workload should not have to reconstruct, control by control, which failure each",
        "one answers. The second is the reason it is generated rather than written -- the rows that",
        "say `partial` or `none` are the ones worth reading first, and a table that could only say",
        "`covered` would be a feature list.",
        "",
        "**A cited test is one that fails when the control is removed**, not one that merely touches",
        "the module. A test of the happy path proves the code runs, which is a different claim. A row",
        "may not say `covered` without one; `scripts/gen_governance_coverage.py --check` and",
        "`tests/unit/test_governance_coverage_table.py` both refuse it.",
        "",
        "**Residual risk is not a caveat.** It is the part of the failure class the control does not",
        "reach, which the operator carries themselves.",
        "",
        f"{len(rows)} failure classes: "
        f"{counts['covered']} covered, {counts['partial']} partial, {counts['none']} with no control.",
        "",
        "| Failure class | Control | Enforced in | Proved by | Residual risk | Status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        test = "—" if row.test is None else f"`{_cell(row.test)}`"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(row.failure_class),
                    _cell(row.control),
                    f"`{_cell(row.module)}`",
                    test,
                    _cell(row.residual),
                    _status_cell(row),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## What a status means",
            "",
            "* `covered` — a control stands in the way of this failure class, and a test fails when",
            "  it is removed.",
            "* `partial` — a control exists and does not reach the whole class. The linked issue",
            "  names the remainder.",
            "* `none` — no control. The linked issue is the one that would add it.",
            "",
            "See [SECURITY.md](../../SECURITY.md) for how to report a gap this table does not list.",
            "",
        ]
    )
    return "\n".join(lines)


def _check_report(expected: str, report_path: Path = REPORT_PATH) -> int:
    """Return an exit code for report freshness, printing the diff on drift."""
    if not report_path.exists():
        print(f"{report_path} is missing; run --update", file=sys.stderr)
        return 1
    current = report_path.read_text(encoding="utf-8")
    if current == expected:
        return 0
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=str(report_path),
        tofile=f"{report_path} (generated)",
    )
    sys.stderr.writelines(diff)
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Fail if the checked-in table is stale.")
    mode.add_argument("--update", action="store_true", help="Write the generated table.")
    args = parser.parse_args(argv)

    report = render_report(load_rows())
    if cast("bool", args.check):
        return _check_report(report)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"wrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
