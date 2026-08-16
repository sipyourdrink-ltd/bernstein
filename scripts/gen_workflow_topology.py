#!/usr/bin/env python3
"""Generate the GitHub Actions workflow topology report."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence


WORKFLOWS_DIR = Path(".github/workflows")
REPORT_PATH = Path("docs/operations/ci-topology.md")
SECRET_REF_RE = re.compile(r"\bsecrets\.([A-Z0-9_]+)\b")


@dataclass(frozen=True)
class WorkflowInfo:
    """Parsed workflow facts used by the report.

    ``path`` is a repository-relative *locator*, not a path you can open.

    That distinction is the fix for #4000. The report embeds this value in
    five different tables, and it used to hold whatever :func:`Path` the
    glob produced - a ``WindowsPath`` on Windows, which renders with
    backslashes and rewrote all 204 rows of the committed report on any
    regeneration from a Windows machine. CI is Linux, so nothing could see
    it: ``--check`` exits 0 there whether or not the bug is present.

    Normalising here rather than at the five render sites is deliberate. A
    rule that five call sites must each remember is a rule that gets
    forgotten once, and the sixth table is what reintroduces the bug.

    ``PurePosixPath`` is doing two jobs. It renders with forward slashes on
    every platform, and it has no ``read_text``/``open`` at all - so the
    type makes "this is a locator, not a file handle" unforgeable rather
    than a comment somebody has to honour. The file is read in
    :func:`parse_workflow` before this record exists, so nothing downstream
    ever wanted to open it.
    """

    path: PurePosixPath
    name: str
    triggers: tuple[str, ...]
    concurrency: str
    job_count: int
    emitted_checks: tuple[str, ...]
    permissions: tuple[str, ...]
    secrets: tuple[str, ...]
    calls: tuple[str, ...]
    artifacts: tuple[str, ...]

    def __post_init__(self) -> None:
        """Coerce whatever path flavour arrived into a posix locator.

        Belt and braces over the annotation. ``parse_workflow`` already
        passes a ``PurePosixPath``, but the annotation alone is a promise
        rather than a mechanism - nothing stops a future caller handing in
        the ``WindowsPath`` straight off the glob, which is exactly how
        this bug arrived the first time. ``object.__setattr__`` because the
        dataclass is frozen.
        """
        object.__setattr__(self, "path", PurePosixPath(self.path.as_posix()))


def _as_mapping(value: object) -> dict[str, object]:
    """Return ``value`` as a string-keyed mapping when possible."""
    if not isinstance(value, dict):
        return {}
    raw = cast("dict[object, object]", value)
    result: dict[str, object] = {}
    for key, item in raw.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _as_sequence(value: object) -> list[object]:
    """Return ``value`` as a list when possible."""
    return cast("list[object]", value) if isinstance(value, list) else []


def _scalar(value: object) -> str:
    """Render a compact deterministic scalar."""
    if value in (None, {}, [], ""):
        return "-"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _cell(value: object) -> str:
    """Escape a markdown table cell."""
    return _scalar(value).replace("\n", "<br>").replace("|", "\\|")


def _trigger_names(doc: dict[str, object]) -> tuple[str, ...]:
    """Extract workflow trigger names from a parsed document."""
    raw = doc.get("on")
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        sequence = cast("list[object]", raw)
        return tuple(sorted(str(item) for item in sequence))
    if isinstance(raw, dict):
        mapping = cast("dict[object, object]", raw)
        return tuple(sorted(str(key) for key in mapping))
    return ()


def _permissions(label: str, value: object) -> tuple[str, ...]:
    """Render workflow or job permissions."""
    if value in (None, {}, ""):
        return ()
    return (f"{label}: {_scalar(value)}",)


def _declared_call_secrets(doc: dict[str, object]) -> set[str]:
    """Return workflow_call secret names declared by a reusable workflow."""
    raw_on = _as_mapping(doc.get("on"))
    workflow_call = _as_mapping(raw_on.get("workflow_call"))
    declared = _as_mapping(workflow_call.get("secrets"))
    return set(declared)


def _referenced_secrets(text: str) -> set[str]:
    """Return secret names referenced in expression syntax."""
    return set(SECRET_REF_RE.findall(text))


def _job_checks(jobs: dict[str, object]) -> tuple[str, ...]:
    """Return job check names emitted by the workflow."""
    checks: list[str] = []
    for job_id, raw_job in sorted(jobs.items()):
        job = _as_mapping(raw_job)
        name = job.get("name")
        checks.append(f"{job_id}: {name}" if isinstance(name, str) else job_id)
    return tuple(checks)


def _job_permissions(jobs: dict[str, object]) -> tuple[str, ...]:
    """Return job-level permissions."""
    rows: list[str] = []
    for job_id, raw_job in sorted(jobs.items()):
        job = _as_mapping(raw_job)
        rows.extend(_permissions(job_id, job.get("permissions")))
    return tuple(rows)


def _workflow_calls(jobs: dict[str, object]) -> tuple[str, ...]:
    """Return reusable workflow calls."""
    calls: list[str] = []
    for job_id, raw_job in sorted(jobs.items()):
        job = _as_mapping(raw_job)
        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
            needs = _scalar(job.get("needs"))
            calls.append(f"{job_id} -> {uses} (needs: {needs})")
    return tuple(calls)


def _artifact_edges(jobs: dict[str, object]) -> tuple[str, ...]:
    """Return upload/download-artifact hand-offs."""
    edges: list[str] = []
    for job_id, raw_job in sorted(jobs.items()):
        job = _as_mapping(raw_job)
        for step in _as_sequence(job.get("steps")):
            step_map = _as_mapping(step)
            uses = step_map.get("uses")
            if not isinstance(uses, str):
                continue
            if "actions/upload-artifact" not in uses and "actions/download-artifact" not in uses:
                continue
            with_block = _as_mapping(step_map.get("with"))
            name = _scalar(with_block.get("name"))
            action = "upload" if "upload-artifact" in uses else "download"
            edges.append(f"{job_id}: {action} {name}")
    return tuple(edges)


def parse_workflow(path: Path) -> WorkflowInfo:
    """Parse one workflow file."""
    text = path.read_text(encoding="utf-8")
    parsed = cast("object", yaml.load(text, Loader=yaml.BaseLoader))
    doc = _as_mapping(parsed)
    jobs = _as_mapping(doc.get("jobs"))
    secrets = _declared_call_secrets(doc) | _referenced_secrets(text)
    workflow_permissions = _permissions("workflow", doc.get("permissions"))

    return WorkflowInfo(
        # Normalised at the boundary: the row builders below just render it.
        path=PurePosixPath(path.as_posix()),
        name=str(doc.get("name", path.name)),
        triggers=_trigger_names(doc),
        concurrency=_scalar(doc.get("concurrency")),
        job_count=len(jobs),
        emitted_checks=_job_checks(jobs),
        permissions=workflow_permissions + _job_permissions(jobs),
        secrets=tuple(sorted(secrets)),
        calls=_workflow_calls(jobs),
        artifacts=_artifact_edges(jobs),
    )


def load_workflows(workflows_dir: Path = WORKFLOWS_DIR) -> tuple[WorkflowInfo, ...]:
    """Load workflow information in deterministic path order."""
    paths = sorted((*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")))
    return tuple(parse_workflow(path) for path in paths)


def _table(headers: tuple[str, ...], rows: Sequence[Sequence[object]]) -> list[str]:
    """Render a markdown table."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return lines


def render_report(workflows: tuple[WorkflowInfo, ...]) -> str:
    """Render the workflow topology report."""
    lines: list[str] = [
        "# GitHub Actions workflow topology",
        "",
        "<!-- AUTO-GENERATED: run `uv run python scripts/gen_workflow_topology.py --update` to refresh -->",
        "",
        "This report lists the workflow graph surfaces reviewers need to inspect when CI topology changes.",
        "",
        "Drift on `main` self-heals: `.github/workflows/ci-topology-heal.yml` regenerates this report",
        "after workflow changes merge and opens a squash auto-merge PR when the committed copy is stale.",
        "",
        "## Workflow Summary",
        "",
    ]
    lines.extend(
        _table(
            ("Workflow", "Name", "Triggers", "Concurrency", "Jobs"),
            [
                (
                    str(info.path),
                    info.name,
                    ", ".join(info.triggers) or "-",
                    info.concurrency,
                    info.job_count,
                )
                for info in workflows
            ],
        )
    )

    lines.extend(["", "## Check Emitters", ""])
    lines.extend(
        _table(
            ("Workflow", "Checks"),
            [(str(info.path), "<br>".join(info.emitted_checks) or "-") for info in workflows],
        )
    )

    lines.extend(["", "## Permissions And Secrets", ""])
    lines.extend(
        _table(
            ("Workflow", "Permissions", "Secrets"),
            [
                (
                    str(info.path),
                    "<br>".join(info.permissions) or "-",
                    ", ".join(info.secrets) or "-",
                )
                for info in workflows
            ],
        )
    )

    lines.extend(["", "## Cross-Workflow Calls", ""])
    call_rows = [(str(info.path), "<br>".join(info.calls)) for info in workflows if info.calls]
    lines.extend(_table(("Caller workflow", "Reusable workflow calls"), call_rows or [("-", "-")]))

    lines.extend(["", "## Artifact Hand-Offs", ""])
    artifact_rows = [(str(info.path), "<br>".join(info.artifacts)) for info in workflows if info.artifacts]
    lines.extend(_table(("Workflow", "Artifact steps"), artifact_rows or [("-", "-")]))

    return "\n".join(lines) + "\n"


def _check_report(expected: str, report_path: Path = REPORT_PATH) -> int:
    """Return an exit code for report freshness."""
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
    mode.add_argument("--check", action="store_true", help="Fail if the checked-in report is stale.")
    mode.add_argument("--update", action="store_true", help="Write the generated report.")
    args = parser.parse_args(argv)

    report = render_report(load_workflows())
    if cast("bool", args.check):
        return _check_report(report)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"wrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
