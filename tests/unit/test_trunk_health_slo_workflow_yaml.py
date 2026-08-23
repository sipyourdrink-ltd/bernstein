"""Structural assertions on the trunk andon mechanism.

The red-trunk merge hold spans two workflows: ``trunk-health-slo.yml``
computes the main-branch red-rate and toggles a marker issue labeled
``trunk-unstable``; the andon gate step in ``pr-policy.yml`` holds merges
while that marker is open. These tests pin the properties that let the
mechanism actually fire, learned from its first design not having them.

They also pin the property that keeps the mechanism from firing *forever*:
the gate must never be able to latch itself open. See the section at the
bottom of this module for why that loop closes and which half of the
sample-size guard is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml

SLO_WORKFLOW = Path(".github/workflows/trunk-health-slo.yml")
POLICY_WORKFLOW = Path(".github/workflows/pr-policy.yml")
TOGGLE_STEP_NAME = "Toggle the trunk-unstable marker issue"


@pytest.fixture(scope="module")
def slo_text() -> str:
    return SLO_WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def policy_text() -> str:
    return POLICY_WORKFLOW.read_text(encoding="utf-8")


def test_the_flag_needs_no_token_beyond_the_job_scoped_one(slo_text: str) -> None:
    """The andon flag must be writable with plain GITHUB_TOKEN.

    The first design stored the flag in an Actions repo variable, which
    GITHUB_TOKEN cannot write under any ``permissions:`` grant. The write
    silently depended on a PAT that was never provisioned, every write
    failed with a swallowed warning, and the gate spent its whole life
    inert. Anchoring the flag on an issue keeps the mechanism inside what
    the job-scoped token can do, so there is no second credential to
    provision, rotate, or silently lose.
    """
    assert "BOT_PAT" not in slo_text, (
        "the andon write side must not depend on a separately provisioned PAT; "
        "that dependency is how the gate was inert the first time"
    )
    assert "actions/variables" not in slo_text, (
        "the flag must not live in an Actions variable -- GITHUB_TOKEN cannot write those"
    )
    assert "trunk-unstable" in slo_text, "the marker issue label is the flag; the writer must reference it"


def test_a_failed_flag_write_is_loud(slo_text: str) -> None:
    """A write failure must redden the SLO job, not degrade to a warning.

    The inert-gate failure mode survived for months because the write
    wrapped its failure in ``::warning`` + ``exit 0``. With ``set -e`` and
    no swallow, a broken write turns the scheduled run red, which is the
    only surface an unattended repository actually checks.
    """
    loaded = yaml.safe_load(slo_text)
    jobs = cast(dict[str, object], loaded["jobs"])
    compute = cast(dict[str, object], jobs["compute"])

    permissions = cast(dict[str, object], compute.get("permissions", {}))
    assert permissions.get("issues") == "write", "toggling the marker issue needs issues: write"

    steps = cast(list[dict[str, object]], compute.get("steps", []))
    toggle = next(
        (step for step in steps if "marker" in str(step.get("name", "")).lower()),
        None,
    )
    assert toggle is not None, "expected a step that toggles the trunk-unstable marker issue"
    run = str(toggle.get("run", ""))
    assert "set -euo pipefail" in run
    assert "exit 0" not in run, (
        "the toggle step must not swallow API failures; an inert andon gate looks exactly like a healthy trunk"
    )


def test_the_gate_reads_the_marker_issue(policy_text: str) -> None:
    """pr-policy's andon gate must key off the marker issue, not a variable."""
    assert "labels=trunk-unstable" in policy_text, (
        "the gate must query open trunk-unstable issues; that is where the flag lives now"
    )
    assert "actions/variables/TRUNK_UNSTABLE" not in policy_text, (
        "the gate must not read the retired Actions variable -- nothing writes it"
    )
    assert "hotfix-cleared" in policy_text, "the hotfix carve-out label must survive the mechanism change"


# ---------------------------------------------------------------------------
# The gate must not be able to latch itself open.
#
# While the marker is open the andon gate holds merges on every PR except
# `hotfix-cleared`. Held merges mean no pushes to main, and `ci.yml` runs on
# main only on `push` and `merge_group` -- there is no schedule. So a held
# repo stops producing the very CI runs this SLO samples:
#
#     marker open -> merges held -> no pushes to main -> no CI runs
#       -> sample under MIN_SAMPLE_SIZE -> close suppressed -> marker open
#
# If a thin sample is allowed to suppress the *close* path, that loop has no
# exit: the documented recovery (a manual `workflow_dispatch`) hits the same
# suppression at the default 24h lookback. The asymmetry that keeps the gate
# safe is that a thin sample is a reason not to *raise* an alarm, never a
# reason to keep one *latched*.
#
# These tests execute the shipped toggle body rather than a copy of it, so a
# refactor cannot restore the symmetry quietly -- whether by putting a
# step-level `if:` back on the step, or by widening the guard inside it.
# ---------------------------------------------------------------------------

_BASH_AVAILABLE = sys.platform != "win32" and shutil.which("bash") is not None
requires_bash = pytest.mark.skipif(
    not _BASH_AVAILABLE,
    reason="needs POSIX bash to execute the shipped toggle step",
)


def _toggle_step(slo_text: str) -> dict[str, object]:
    """The shipped marker-toggle step, by name."""
    loaded = yaml.safe_load(slo_text)
    jobs = cast(dict[str, object], loaded["jobs"])
    compute = cast(dict[str, object], jobs["compute"])
    steps = cast(list[dict[str, object]], compute.get("steps", []))
    for step in steps:
        if step.get("name") == TOGGLE_STEP_NAME:
            return step
    raise AssertionError(f"{SLO_WORKFLOW} no longer has a {TOGGLE_STEP_NAME!r} step")


def _write_fake_gh(tmp_path: Path, open_markers: str) -> Path:
    """A `gh` shim that logs every call and reports the open marker numbers.

    The toggle body reads the open marker list out of a single
    ``gh api ...issues?labels=trunk-unstable&state=open... --jq`` call, so the
    shim answers that one query and accepts everything else, recording the
    full argument vector for the assertions.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "gh $*" >> "${GH_CALL_LOG}"\n'
        'case "$*" in\n'
        f'  *"state=open"*) printf "%s\\n" "{open_markers}" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return bin_dir


def _run_toggle(
    slo_text: str,
    tmp_path: Path,
    *,
    open_markers: str,
    unstable: str,
    insufficient: str,
    total: str = "3",
    red: str = "0",
    red_pct: str = "0",
) -> list[str]:
    """Execute the shipped toggle body; return the `gh` calls it made."""
    run = str(_toggle_step(slo_text).get("run", ""))
    assert run, "the toggle step must carry a `run:` body"

    bin_dir = _write_fake_gh(tmp_path, open_markers)
    call_log = tmp_path / "gh-calls.log"
    call_log.write_text("", encoding="utf-8")
    summary = tmp_path / "step-summary.md"
    summary.write_text("", encoding="utf-8")

    proc = subprocess.run(
        ["bash", "-c", run],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "GH_CALL_LOG": str(call_log),
            "GH_TOKEN": "unused-by-the-shim",
            "REPO": "acme/widgets",
            "UNSTABLE": unstable,
            "INSUFFICIENT": insufficient,
            "TOTAL": total,
            "RED": red,
            "RED_PCT": red_pct,
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"toggle step failed.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return [line for line in call_log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _opened(calls: list[str]) -> list[str]:
    # The open branch is the only call that POSTs a new issue with a title.
    return [c for c in calls if "-X POST" in c and "-f title=" in c]


def _closed(calls: list[str]) -> list[str]:
    return [c for c in calls if "-X PATCH" in c and "state=closed" in c]


def _close_comments(calls: list[str]) -> list[str]:
    return [c for c in calls if "-X POST" in c and "/comments" in c]


def test_the_toggle_step_is_not_skipped_on_a_thin_sample(slo_text: str) -> None:
    """No step-level ``if:`` may skip the toggle step on sample size.

    A step-level guard is indiscriminate: skipping the step skips the branch
    that closes an open marker along with the branch that opens a new one,
    which is the latch itself. The sample-size guard belongs inside the body,
    around the open branch only.
    """
    condition = _toggle_step(slo_text).get("if")
    assert condition is None or "insufficient_sample" not in str(condition), (
        "the toggle step must not be skipped on an insufficient sample -- skipping it "
        "also skips the branch that closes an open marker, so the gate can hold every "
        f"merge in the repo with no automated way out. Found `if: {condition}`."
    )


@requires_bash
def test_a_thin_sample_still_closes_an_open_marker(slo_text: str, tmp_path: Path) -> None:
    """The wedge case: marker open, sample too thin to judge -> still closes.

    This is the state the repo lands in once the gate has held merges long
    enough to starve its own sample.
    """
    calls = _run_toggle(slo_text, tmp_path, open_markers="4242", unstable="false", insufficient="true")
    assert _closed(calls), (
        "an open marker must still close when the sample is insufficient, or the gate "
        "latches the repo shut. gh calls:\n" + "\n".join(calls)
    )


@requires_bash
def test_a_thin_sample_never_opens_a_new_marker(slo_text: str, tmp_path: Path) -> None:
    """The half of the guard worth keeping: no alarm raised on no evidence."""
    calls = _run_toggle(slo_text, tmp_path, open_markers="", unstable="false", insufficient="true")
    assert not _opened(calls), "an insufficient sample must not open a new marker. gh calls:\n" + "\n".join(calls)


@requires_bash
def test_a_thin_sample_closes_for_lack_of_evidence_not_recovery(slo_text: str, tmp_path: Path) -> None:
    """The marker thread is an audit trail; it must not claim a recovery.

    ``score_runs`` returns ``red_pct=0`` for a thin window, so a single shared
    close message would report "back under threshold (0%)" for a window in
    which nothing was measured. An operator reading the closed marker later
    would take that as evidence main recovered.
    """
    calls = _run_toggle(slo_text, tmp_path, open_markers="4242", unstable="false", insufficient="true")
    comments = _close_comments(calls)
    assert comments, "closing a marker must leave a comment saying why"
    body = "\n".join(comments)
    assert "under threshold" not in body, (
        f"closing on a thin sample must not claim a measured recovery. Comment was:\n{body}"
    )


@requires_bash
def test_a_measured_recovery_still_reports_the_rate_it_measured(slo_text: str, tmp_path: Path) -> None:
    """The other close path keeps saying what it actually measured."""
    calls = _run_toggle(
        slo_text,
        tmp_path,
        open_markers="4242",
        unstable="false",
        insufficient="false",
        total="50",
        red="1",
        red_pct="2",
    )
    assert _closed(calls), "a measured recovery must close the marker"
    body = "\n".join(_close_comments(calls))
    assert "2%" in body, f"a measured recovery should report its rate. Comment was:\n{body}"


@requires_bash
def test_a_measured_red_trunk_still_opens_a_marker(slo_text: str, tmp_path: Path) -> None:
    """The gate still gates: a well-sampled red trunk holds merges."""
    calls = _run_toggle(
        slo_text, tmp_path, open_markers="", unstable="true", insufficient="false", total="50", red="20", red_pct="40"
    )
    assert _opened(calls), "a measured red trunk over threshold must open the marker. gh calls:\n" + "\n".join(calls)


def test_the_script_withholds_unstable_on_a_thin_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A thin window reports ``insufficient_sample``, never ``unstable``.

    The workflow's open branch keys off ``unstable``, so scoring a red rate
    from a two-run window here would open a marker on a sample too thin to
    judge even with the workflow-level guard in place.
    """
    from scripts import trunk_health_slo

    output = tmp_path / "github-output.txt"
    output.write_text("", encoding="utf-8")
    thin_window = [{"conclusion": "failure"}, {"conclusion": "success"}]

    monkeypatch.setattr(trunk_health_slo, "fetch_ci_runs", lambda *a, **k: thin_window)
    monkeypatch.setenv("GH_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["trunk_health_slo.py", "--repo", "acme/widgets"])

    trunk_health_slo.main()

    emitted = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert emitted["insufficient_sample"] == "true"
    assert emitted["unstable"] == "false", (
        "a window under MIN_SAMPLE_SIZE must not report `unstable`, or the workflow "
        "opens a marker on a sample too thin to judge."
    )
