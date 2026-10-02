"""Required gate-evasion CI job and strict skip policy (#6154)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_planner_covers_quality_corpus_tools_baseline_and_merge_group() -> None:
    jobs = _jobs()
    planner = jobs["determine-changes"]
    assert planner["outputs"]["gate_evasion_relevant"] == "${{ steps.classify.outputs.gate_evasion_relevant }}"
    script = next(s["run"] for s in planner["steps"] if s.get("id") == "classify")
    for path in (
        "src/bernstein/core/quality/",
        "src/bernstein/eval/cases/gate_evasion/",
        "src/bernstein/eval/bench/",
        "gate-evasion-baseline",
        "pyproject",
        "uv",
        "workflows/ci",
    ):
        assert path in script
    assert 'echo "gate_evasion_relevant=true"' in script
    assert "gate_evasion_relevant=false" in script
    assert 'git diff --name-only "${MERGE_GROUP_BASE_SHA}...HEAD"' in script
    assert 'git diff --name-only "origin/${BASE_REF}...HEAD"' in script
    classifier = next(line for line in script.splitlines() if "grep -Eq" in line and "gate-evasion-baseline" in line)
    expression = classifier.split("grep -Eq '", 1)[1].rsplit("'", 1)[0]
    for path in (
        "src/bernstein/core/quality/janitor.py",
        "src/bernstein/eval/cases/gate_evasion/foo/manifest.json",
        "src/bernstein/eval/bench/gate_evasion_baseline.py",
        "src/bernstein/eval/bench/bench_cli.py",
        ".github/gate-evasion-baseline.json",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "uv.lock",
    ):
        assert re.search(expression, path), path
    for path in ("docs/README.md", "src/bernstein/core/tasks/models.py"):
        assert not re.search(expression, path), path


def test_job_uses_locked_tools_and_is_required_by_ci_gate() -> None:
    jobs = _jobs()
    job = jobs["gate-evasion"]
    assert job["needs"] == ["determine-changes"]
    assert job["env"]["UV_PYTHON"] == "3.13"
    assert "gate_evasion_relevant == 'true'" in job["if"]
    assert "gate-evasion" in jobs["ci-gate"]["needs"]
    assert any(step.get("uses") == "./.github/actions/bootstrap" for step in job["steps"])
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert "uv sync --locked --group dev --python 3.13" in commands
    assert "uv run --no-sync bernstein bench gate-evasion-baseline" in commands
    assert "--update" not in commands


def _rollup_script() -> str:
    job = _jobs()["ci-gate"]
    script = next(step["run"] for step in job["steps"] if step.get("id") == "roll-up")
    return script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


@pytest.mark.parametrize(
    ("flag", "result", "docs_only", "expected"),
    [
        ("false", "skipped", "false", 0),
        ("true", "skipped", "false", 1),
        ("true", "skipped", "true", 1),
        ("true", "failure", "false", 1),
        ("true", "cancelled", "false", 1),
        ("true", "success", "false", 0),
        ("", "skipped", "false", 1),
    ],
)
def test_rollup_rejects_unexpected_skips_and_failures(
    flag: str, result: str, docs_only: str, expected: int, tmp_path: Path
) -> None:
    (tmp_path / "results.json").write_text(json.dumps({"gate-evasion": {"result": result}}))
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "gate_evasion_relevant": flag,
                "docs_only": docs_only,
                "macos_sensitive": "false",
                "rpm_relevant_changed": "false",
            }
        )
    )
    event_file = tmp_path / "event.json"
    event_file.write_text("{}")
    env = {
        **os.environ,
        "EVENT_NAME": "merge_group",
        "REF_NAME": "refs/heads/main",
        "HEAD_COMMIT_MESSAGE": "",
        "GITHUB_EVENT_PATH": str(event_file),
    }
    proc = subprocess.run(
        ["python3", "-c", _rollup_script()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (proc.returncode == 0) == (expected == 0), proc.stdout + proc.stderr
