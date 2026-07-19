"""Release entrypoint documentation stays aligned with workflow files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_DOC = REPO_ROOT / "docs" / "operations" / "release.md"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

type YamlMap = dict[object, object]

RELEASE_ENTRYPOINTS: dict[str, tuple[str, ...]] = {
    "post-ci-dispatcher.yml": ("workflow_run",),
    "auto-release.yml": ("workflow_call",),
    "publish.yml": ("push",),
    "release-major-minor.yml": ("workflow_dispatch",),
    "reconcile-release.yml": ("schedule", "workflow_dispatch"),
    "publish-docker.yml": ("release", "workflow_dispatch"),
    "publish-homebrew.yml": ("release", "workflow_dispatch"),
    "sbom-upload.yml": ("push", "release"),
}


@dataclass(frozen=True)
class ReleaseDocRow:
    """One row in the release workflow ownership table."""

    workflow_path: str
    workflow_name: str
    triggers: str
    owns: str
    handoff: str


def _load_yaml(path: Path) -> YamlMap:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path} must parse as a YAML mapping"
    return cast("YamlMap", parsed)


def _workflow_name(path: Path) -> str:
    name = _load_yaml(path).get("name")
    assert isinstance(name, str), f"{path} must declare a workflow name"
    return name


def _workflow_triggers(path: Path) -> set[str]:
    data = _load_yaml(path)
    on_block = data.get("on")
    if on_block is None:
        on_block = data.get(True)
    assert isinstance(on_block, dict), f"{path} must declare mapping-style triggers"
    trigger_map = cast("YamlMap", on_block)
    return {str(trigger) for trigger in trigger_map}


def _release_table_rows(text: str) -> dict[str, ReleaseDocRow]:
    rows: dict[str, ReleaseDocRow] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 5 or not cells[0].startswith("`.github/workflows/"):
            continue
        workflow_path = cells[0].strip("`")
        rows[workflow_path] = ReleaseDocRow(
            workflow_path=workflow_path,
            workflow_name=cells[1],
            triggers=cells[2],
            owns=cells[3],
            handoff=cells[4],
        )
    return rows


def test_release_entrypoint_doc_references_actual_workflows() -> None:
    """The release operations doc must name the real workflow entrypoints."""
    assert RELEASE_DOC.exists(), f"{RELEASE_DOC} must document release workflow ownership"
    rows = _release_table_rows(RELEASE_DOC.read_text(encoding="utf-8"))

    for workflow_file, expected_triggers in RELEASE_ENTRYPOINTS.items():
        workflow_path = WORKFLOWS_DIR / workflow_file
        documented_path = f".github/workflows/{workflow_file}"
        assert workflow_path.exists(), f"{documented_path} no longer exists"
        assert documented_path in rows, f"{documented_path} is missing from {RELEASE_DOC}"

        row = rows[documented_path]
        assert row.workflow_name == _workflow_name(workflow_path)
        assert row.owns, f"{documented_path} must document ownership"
        assert row.handoff, f"{documented_path} must document the handoff"

        workflow_triggers = _workflow_triggers(workflow_path)
        for trigger in expected_triggers:
            assert trigger in workflow_triggers, f"{documented_path} no longer has trigger {trigger}"
            assert trigger in row.triggers, f"{documented_path} doc row must list trigger {trigger}"
