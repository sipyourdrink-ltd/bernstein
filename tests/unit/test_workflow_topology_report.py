"""Contracts for the generated workflow topology report (#1827 F-058)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

from scripts.gen_workflow_topology import WorkflowInfo

REPORT = Path("docs/operations/ci-topology.md")


def test_workflow_topology_report_is_current() -> None:
    """The checked-in report must match the workflow YAML graph."""
    proc = subprocess.run(
        [sys.executable, "scripts/gen_workflow_topology.py", "--check"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_workflow_topology_report_names_high_risk_edges() -> None:
    """The report must expose the edges reviewers need to inspect."""
    text = REPORT.read_text(encoding="utf-8")

    for heading in (
        "## Workflow Summary",
        "## Check Emitters",
        "## Permissions And Secrets",
        "## Cross-Workflow Calls",
        "## Artifact Hand-Offs",
    ):
        assert heading in text


class TestPathsAreRenderedAsPosixLocators:
    """#4000: the report embedded OS-native separators.

    These must fail on Linux without the fix, or they are decoration. That
    is what ``PureWindowsPath`` is for: it renders with backslashes on
    *every* host, not just on Windows, so the assertion exercises the real
    defect on the platform CI actually runs.

    A test asserting "no row in the generated report contains a backslash"
    would NOT do: on Linux the glob yields a ``PosixPath`` and that passes
    whether or not anything was fixed. It cannot fail where it runs, so it
    is a comment with a green tick.
    """

    def info(self, path: Path | PureWindowsPath | PurePosixPath) -> WorkflowInfo:
        return WorkflowInfo(
            path=path,  # type: ignore[arg-type]  # the point is that a wrong flavour is coerced
            name="CI",
            triggers=("push",),
            concurrency="-",
            job_count=1,
            emitted_checks=(),
            permissions=(),
            secrets=(),
            calls=(),
            artifacts=(),
        )

    def test_a_windows_flavoured_path_renders_with_forward_slashes(self) -> None:
        # The exact shape the Windows glob produces. Before the fix this
        # renders `.github\workflows\ci.yml` on Linux too.
        rendered = str(self.info(PureWindowsPath(r".github\workflows\ci.yml")).path)
        assert rendered == ".github/workflows/ci.yml"
        assert "\\" not in rendered

    def test_a_posix_path_is_left_alone(self) -> None:
        rendered = str(self.info(PurePosixPath(".github/workflows/ci.yml")).path)
        assert rendered == ".github/workflows/ci.yml"

    def test_the_locator_type_cannot_be_opened(self) -> None:
        # The guarantee behind the type choice: `path` is a locator, not a
        # file handle. If someone widens it back to `Path` to call
        # `read_text` on it, this is what says no.
        assert not hasattr(self.info(PurePosixPath("a/b.yml")).path, "read_text")

    def test_every_locator_in_the_committed_report_is_posix(self) -> None:
        """The end-to-end backstop, scoped to the locator CELL.

        Not the whole row: ten rows carry a legitimate backslash in the
        concurrency column, where a `${{ }}` expression is JSON-escaped.
        Asserting over the row would fail on correct output - a guard that
        cries wolf, which is how guards get deleted.

        This one passes vacuously on Linux, which is exactly why it is not
        the only test in this class.
        """
        locators = [
            line.split("|")[1].strip()
            for line in REPORT.read_text(encoding="utf-8").splitlines()
            if line.startswith("| .github")
        ]
        assert locators, "no workflow locators found in the report - has the format changed?"
        offenders = [locator for locator in locators if "\\" in locator]
        assert not offenders, f"{len(offenders)} locator(s) use OS-native separators: {offenders[:3]}"
