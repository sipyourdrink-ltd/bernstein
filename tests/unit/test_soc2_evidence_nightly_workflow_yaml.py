"""Structural and behavioral assertions for the SOC 2 evidence pack workflow.

Covers ``.github/workflows/soc2-evidence-nightly.yml``:

- the ``preflight`` gate step must never let a missing
  ``SOC2_EVIDENCE_ENABLED`` secret read as a silent success - a run with
  the secret unset stays green (a dormant lane is not a failure) but must
  annotate the run (``::warning::``) and write a step-summary line naming
  what did not happen and what would enable it,
- the ``preflight`` gate step with the secret set stays quiet (no warning,
  no summary line),
- the ``pack`` job's ``retention-days: 90`` is GitHub's ceiling for public
  repositories, not a value this workflow can raise, and the workflow
  documents that plus the durable-export follow-up in a comment.

The gate step's ``run:`` block is executed as a real subprocess against
both a present and an absent ``SOC2_EVIDENCE_ENABLED`` so this test proves
the actual shell behavior, not just the presence of the string
``::warning::`` somewhere in the YAML.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "soc2-evidence-nightly.yml"


def _load() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _job(name: str) -> dict[str, Any]:
    jobs = _load().get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get(name)
    assert isinstance(job, dict), f"expected job {name!r}"
    return cast("dict[str, Any]", job)


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [cast("dict[str, Any]", step) for step in steps if isinstance(step, dict)]


def _step_by_id(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    match = next((step for step in _steps(job) if step.get("id") == step_id), None)
    assert match is not None, f"expected step id {step_id!r}"
    return match


def _step_by_name(job: dict[str, Any], name: str) -> dict[str, Any]:
    match = next((step for step in _steps(job) if step.get("name") == name), None)
    assert match is not None, f"expected step named {name!r}"
    return match


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_gate_step_run_block_mentions_warning_and_step_summary() -> None:
    """Static check that the branches an operator needs are in the script."""
    run = _step_by_id(_job("preflight"), "gate").get("run", "")
    assert isinstance(run, str)
    assert "::warning::" in run
    assert "GITHUB_STEP_SUMMARY" in run
    assert "SOC2_EVIDENCE_ENABLED" in run


def _run_gate_script(run_script: str, tmp_path: Path, *, enabled: str | None) -> dict[str, Any]:
    """Execute the gate step's real ``run:`` block as a subprocess.

    Mirrors how Actions invokes a ``run:`` block: ``GITHUB_OUTPUT`` and
    ``GITHUB_STEP_SUMMARY`` point at real files the script appends to.
    """
    output_path = tmp_path / "github_output.txt"
    summary_path = tmp_path / "github_step_summary.txt"
    output_path.write_text("", encoding="utf-8")
    summary_path.write_text("", encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_STEP_SUMMARY": str(summary_path),
    }
    if enabled is not None:
        env["SOC2_EVIDENCE_ENABLED"] = enabled

    proc = subprocess.run(
        ["bash", "-c", run_script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output": output_path.read_text(encoding="utf-8"),
        "summary": summary_path.read_text(encoding="utf-8"),
    }


def test_gate_step_with_secret_unset_stays_green_but_warns_loudly(tmp_path: Path) -> None:
    """14 consecutive silent no-op runs is exactly the bug this locks out.

    A dormant lane is not a failure, so the step must still exit 0 - but it
    must never again look identical to a real evidence pack: an
    ``::warning::`` annotation (where the Actions UI surfaces it) and a
    ``GITHUB_STEP_SUMMARY`` line naming what didn't happen and what would
    enable it.
    """
    run = _step_by_id(_job("preflight"), "gate").get("run", "")
    assert isinstance(run, str)

    result = _run_gate_script(run, tmp_path, enabled=None)

    assert result["returncode"] == 0, f"a dormant lane must stay green.\nstderr:\n{result['stderr']}"
    assert "enabled=false" in result["output"]
    assert "::warning::" in result["stdout"]
    assert "SOC2_EVIDENCE_ENABLED" in result["stdout"]
    summary = result["summary"]
    assert "SOC2_EVIDENCE_ENABLED" in summary
    assert "secret" in summary.lower() or "provision" in summary.lower()


def test_gate_step_with_secret_set_runs_quietly(tmp_path: Path) -> None:
    """The happy path must not gain a spurious warning or summary noise."""
    run = _step_by_id(_job("preflight"), "gate").get("run", "")
    assert isinstance(run, str)

    result = _run_gate_script(run, tmp_path, enabled="1")

    assert result["returncode"] == 0
    assert "enabled=true" in result["output"]
    assert "::warning::" not in result["stdout"]
    assert result["summary"] == ""


def test_pack_job_stays_gated_on_the_preflight_output() -> None:
    """The dormancy fix must not accidentally make the pack job unconditional."""
    pack = _job("pack")
    condition = pack.get("if", "")
    assert condition == "needs.preflight.outputs.enabled == 'true'"


def test_retention_is_githubs_ceiling_and_the_workflow_says_so() -> None:
    """90 days is the maximum GitHub allows for public repos - not a value
    to raise. The workflow must say so in place, rather than silently
    bumping a number that has nowhere higher to go, and must point at the
    durable-export follow-up.
    """
    pack = _job("pack")
    upload = _step_by_name(pack, "Upload evidence pack")
    with_block = upload.get("with", {})
    assert isinstance(with_block, dict)
    assert with_block.get("retention-days") == 90

    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "GitHub's hard ceiling" in raw
    assert "#3966" in raw, "the workflow comment must reference the durable-export follow-up issue"
