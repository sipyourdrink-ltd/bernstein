"""The deleted-source-of-truth gate on pull requests.

A change that deletes a module the playbook names as a doc's source of
truth leaves the doc documenting something the tree no longer contains.
The drift report next to this gate is advisory on pull requests, so that
class of change used to land green and turn main red on the push run
afterwards, with the broken doc already published.

These tests pin the gate to the shape that catches it: the checker
function itself, the CLI exit codes the workflow depends on, and the
workflow step that feeds it the change's deleted paths.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_docs_drift.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-drift.yml"
STEP_NAME = "Fail when the change deletes a documented source of truth"

#: The deletion that produced this gate: `team_hub_loader.py` was removed as
#: unreachable code while `docs/concepts/team-hub.md` still named it as its
#: source of truth, and the push run on main failed after the merge.
REAL_DELETED_SOURCE = "src/bernstein/core/plugins_core/team_hub_loader.py"


def _script() -> ModuleType:
    cached = sys.modules.get("check_docs_drift")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("check_docs_drift", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the script's dataclasses resolve their own
    # module out of sys.modules when their fields are built.
    sys.modules["check_docs_drift"] = module
    spec.loader.exec_module(module)
    return module


def _row(sources_raw: str, remediation: str = "manual-prose"):
    return _script().DocRow(
        doc="team-hub.md",
        sources_raw=sources_raw,
        drift_signal="Manifest schema change",
        remediation=remediation,
    )


def test_deleting_a_documented_source_is_reported() -> None:
    """The exact deletion that turned main red must be flagged."""
    mod = _script()
    rows = [_row(f"`{REAL_DELETED_SOURCE}`, `src/bernstein/core/plugins_core/team_hub_manifest.py`")]

    hits = mod.sources_deleted_by([REAL_DELETED_SOURCE], rows)

    assert [src for _, src in hits] == [REAL_DELETED_SOURCE]


def test_deleting_an_undocumented_path_is_not_reported() -> None:
    """Deletions unrelated to any doc must not block the change."""
    mod = _script()
    rows = [_row("`src/bernstein/core/plugins_core/team_hub_manifest.py`")]

    assert mod.sources_deleted_by(["src/bernstein/core/plugins_core/dep_resolver.py"], rows) == []


def test_static_rows_are_exempt() -> None:
    """`static` rows are excluded here exactly as they are in check_sources."""
    mod = _script()
    rows = [_row(f"`{REAL_DELETED_SOURCE}`", remediation="static")]

    assert mod.sources_deleted_by([REAL_DELETED_SOURCE], rows) == []


def test_playbook_no_longer_names_the_deleted_loader() -> None:
    """The row that broke must stay fixed, or this gate has nothing to catch."""
    mod = _script()
    rows = mod.parse_playbook(mod.PLAYBOOK)

    assert rows, "playbook parsed to zero rows"
    assert mod.sources_deleted_by([REAL_DELETED_SOURCE], rows) == []


def test_cli_exits_nonzero_when_a_documented_source_is_deleted(tmp_path: Path) -> None:
    """The workflow step reads the process exit code, so pin it."""
    deleted = tmp_path / "deleted.txt"
    deleted.write_text(f"{REAL_DELETED_SOURCE}\nsrc/bernstein/core/routing/cloudflare_ai.py\n", encoding="utf-8")

    # The current playbook is fixed, so a path it still names is needed to
    # drive the failing branch; team_hub_manifest.py is that path.
    documented = tmp_path / "documented.txt"
    documented.write_text("src/bernstein/core/plugins_core/team_hub_manifest.py\n", encoding="utf-8")

    clean = subprocess.run(
        [sys.executable, str(SCRIPT), "--deleted-paths-file", str(deleted)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert clean.returncode == 0, clean.stderr

    flagged = subprocess.run(
        [sys.executable, str(SCRIPT), "--deleted-paths-file", str(documented)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert flagged.returncode == 1
    assert "team_hub_manifest.py" in flagged.stderr
    assert "docs/playbooks/docs-drift.md" in flagged.stderr


def _step() -> dict[str, object]:
    data = cast("dict[str, object]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    jobs = cast("dict[str, object]", data["jobs"])
    job = cast("dict[str, object]", jobs["drift-check"])
    steps = cast("list[object]", job["steps"])
    for step in steps:
        if isinstance(step, dict) and step.get("name") == STEP_NAME:
            return cast("dict[str, object]", step)
    pytest.fail(f"docs-drift.yml has no {STEP_NAME!r} step")


def test_workflow_runs_the_gate_on_pull_requests() -> None:
    """A gate that only ran on push would repeat the failure it exists to stop."""
    step = _step()

    assert "pull_request" in str(step.get("if", ""))

    run = str(step.get("run", ""))
    assert "--diff-filter=D" in run, "the step must feed the gate deletions only"
    assert "--deleted-paths-file" in run
