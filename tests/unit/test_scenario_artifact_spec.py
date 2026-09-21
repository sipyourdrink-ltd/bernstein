"""Test scenario runs with non-default artifact_spec."""
from __future__ import annotations

import tempfile
from pathlib import Path

from bernstein.core.planning.routine_bridge import RoutineBridge
from bernstein.core.tasks.artifacts import ArtifactKind


def test_scenario_task_carries_non_default_artifact_spec():
    """A scenario task with a declared artifact_spec emits that spec in the task payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        scenarios_dir = tmp_path / ".bernstein" / "scenarios"
        scenarios_dir.mkdir(parents=True)
        state_dir = tmp_path / ".sdd" / "routines"
        state_dir.mkdir(parents=True)

        # Create a scenario YAML with a task that declares a REPORT artifact spec
        scenario_yaml = """
id: test-artifact-scenario
name: Test Artifact Scenario
description: Test scenario with non-default artifact spec
tags: [test]
version: "1.0"
tasks:
  - title: Generate report
    description: Create a compliance report
    role: qa
    priority: 1
    scope: small
    complexity: low
    artifact_spec:
      kind: report
      output_path: report.json
"""
        (scenarios_dir / "test.yaml").write_text(scenario_yaml.strip())

        # Load the scenario library and invoke the scenario
        bridge = RoutineBridge.from_paths(scenarios_dir, state_dir)
        _invocation, payloads = bridge.invoke_scenario("test-artifact-scenario")

        # Verify we got exactly one task payload
        assert len(payloads) == 1
        payload = payloads[0]

        # Verify the payload contains the expected artifact spec
        assert payload.artifact_spec.kind == ArtifactKind.REPORT
        assert payload.artifact_spec.output_path == "report.json"

        # Verify the server payload includes the artifact spec
        server_payload = payload.as_server_payload()
        assert "artifact_spec" in server_payload
        assert server_payload["artifact_spec"]["kind"] == "report"
        assert server_payload["artifact_spec"]["output_path"] == "report.json"


def test_scenario_task_without_artifact_spec_defaults_to_code_diff():
    """A scenario task without artifact_spec defaults to code_diff in the task payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        scenarios_dir = tmp_path / ".bernstein" / "scenarios"
        scenarios_dir.mkdir(parents=True)
        state_dir = tmp_path / ".sdd" / "routines"
        state_dir.mkdir(parents=True)

        # Create a scenario YAML with a task that has NO artifact_spec (should default)
        scenario_yaml = """
id: test-default-scenario
name: Test Default Scenario
description: Test scenario with default artifact spec
tags: [test]
version: "1.0"
tasks:
  - title: Write code
    description: Implement a feature
    role: backend
    priority: 1
    scope: small
    complexity: low
    # No artifact_spec declared - should default to code_diff
"""
        (scenarios_dir / "test.yaml").write_text(scenario_yaml.strip())

        # Load the scenario library and invoke the scenario
        bridge = RoutineBridge.from_paths(scenarios_dir, state_dir)
        _invocation, payloads = bridge.invoke_scenario("test-default-scenario")

        # Verify we got exactly one task payload
        assert len(payloads) == 1
        payload = payloads[0]

        # Verify the payload defaults to code_diff artifact spec
        assert payload.artifact_spec.kind == ArtifactKind.CODE_DIFF
        assert payload.artifact_spec.output_path == ""

        # Verify the server payload does NOT include artifact_spec (since it's default)
        server_payload = payload.as_server_payload()
        assert "artifact_spec" not in server_payload


def test_scenario_task_with_code_diff_artifact_spec_is_omitted_from_payload():
    """A scenario task with explicit code_diff artifact_spec omits it from the task payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        scenarios_dir = tmp_path / ".bernstein" / "scenarios"
        scenarios_dir.mkdir(parents=True)
        state_dir = tmp_path / ".sdd" / "routines"
        state_dir.mkdir(parents=True)

        # Create a scenario YAML with a task that explicitly declares code_diff
        scenario_yaml = """
id: test-explicit-code-diff
name: Test Explicit Code Diff
description: Test scenario with explicit code_diff artifact spec
tags: [test]
version: "1.0"
tasks:
  - title: Write code
    description: Implement a feature
    role: backend
    priority: 1
    scope: small
    complexity: low
    artifact_spec:
      kind: code_diff
"""
        (scenarios_dir / "test.yaml").write_text(scenario_yaml.strip())

        # Load the scenario library and invoke the scenario
        bridge = RoutineBridge.from_paths(scenarios_dir, state_dir)
        _invocation, payloads = bridge.invoke_scenario("test-explicit-code-diff")

        # Verify we got exactly one task payload
        assert len(payloads) == 1
        payload = payloads[0]

        # Verify the payload has code_diff artifact spec
        assert payload.artifact_spec.kind == ArtifactKind.CODE_DIFF

        # Verify the server payload does NOT include artifact_spec (since it's default)
        server_payload = payload.as_server_payload()
        assert "artifact_spec" not in server_payload
