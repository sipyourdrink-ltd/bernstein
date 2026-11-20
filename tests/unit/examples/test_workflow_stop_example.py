"""Verify the dependency graph, the stop after failure, and which commands actually ran."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.workflows import load_workflow_spec
from tests.unit.examples import ROOT, run_demo

if TYPE_CHECKING:
    from pathlib import Path


def test_example_has_command_branches_and_a_second_downstream_layer() -> None:
    spec = load_workflow_spec(ROOT / "examples" / "workflow-stop" / "workflow.yaml")
    assert all(node.kind == "command" for node in spec.nodes)
    assert {node.id: list(node.depends_on) for node in spec.nodes} == {
        "branch-a": [],
        "branch-b": [],
        "after-a": ["branch-a"],
        "after-b": ["branch-b"],
        "after-after-a": ["after-a"],
    }


def test_failed_branch_skips_all_later_commands(tmp_path: Path) -> None:
    proc = run_demo("workflow-stop", tmp_path)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["succeeded"] is False
    assert sorted((n["id"], n["status"], n["exit_code"]) for n in summary["nodes"]) == [
        ("after-a", "skipped", None),
        ("after-after-a", "skipped", None),
        ("after-b", "skipped", None),
        ("branch-a", "success", 0),
        ("branch-b", "failed", 1),
    ]
    assert summary["markers"] == ["branch-a-ran.txt", "branch-b-ran.txt"]
