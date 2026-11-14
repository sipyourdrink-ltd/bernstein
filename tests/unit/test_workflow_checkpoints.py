"""Verify workflow resume metadata without starting any nodes."""

from pathlib import Path

from bernstein.core.workflows import load_workflow_spec_from_text
from bernstein.core.workflows.workflow_runner import load_spec_snapshot, record_spec_snapshot, spec_digest


def test_snapshot_binds_parsed_spec_and_original_source(tmp_path: Path) -> None:
    manifest = 'name: deploy\ndescription: Example deploy\nversion: "1.0.0"\nnodes: [{id: verify, command: echo ready}]'
    spec = load_workflow_spec_from_text(manifest)
    reformatted = load_workflow_spec_from_text("# A comment\n" + manifest)
    changed = load_workflow_spec_from_text(manifest.replace("echo ready", "echo changed"))

    assert spec_digest(spec) == spec_digest(reformatted)
    assert spec_digest(spec) != spec_digest(changed)
    assert load_spec_snapshot(tmp_path, "run-1") is None
    digest = record_spec_snapshot(tmp_path, "run-1", spec, "workflows/deploy.yaml")

    assert digest == spec_digest(spec)
    snapshot = load_spec_snapshot(tmp_path, "run-1")
    assert snapshot == {
        "version": 1,
        "spec_name": "deploy",
        "spec_version": "1.0.0",
        "spec_digest": digest,
        "node_ids": ["verify"],
        "source": "workflows/deploy.yaml",
    }
    assert load_spec_snapshot(tmp_path, "run-2") is None
