"""``dependabot-auto-merge.yml`` never arms a Draft and fails closed on an unknown state (#6758).

The auto-arm step used to run ``gh pr merge --auto`` on every eligible
patch/minor update, and the API refuses that for a Draft
(``Pull request is a draft (enablePullRequestAutoMerge)``), so a Draft
update failed the lane instead of waiting. These tests execute the step's
actual ``run:`` script under bash with a recording ``gh`` on PATH, so the
property holds for the shipped shell rather than for a re-implementation
of it, and read the YAML for the trigger and binding the shell depends on.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml"
ARM_STEP = "Auto-merge patch and minor updates"
PR_URL = "https://github.com/sipyourdrink-ltd/bernstein/pull/6730"
DEPENDABOT_USER_ID = 49699333


def _bash() -> str:
    """A bash the step can run under; Git for Windows before the WSL launcher."""
    candidates = [
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and "system32" not in candidate.lower():
            return candidate
    pytest.skip("no bash available to run the workflow shell")


def _load() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{WORKFLOW} is not a mapping"
    return doc


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare ``on:`` key as boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), "workflow has no trigger mapping"
    return triggers


def _arm_step(doc: dict[str, Any]) -> dict[str, Any]:
    steps = doc["jobs"]["auto-merge"]["steps"]
    matches = [s for s in steps if isinstance(s, dict) and s.get("name") == ARM_STEP]
    assert len(matches) == 1, f"expected exactly one step named {ARM_STEP!r}, found {len(matches)}"
    return matches[0]


def _run_arm_step(
    tmp_path: Path,
    *,
    draft: str | None,
    gh_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Execute the step's ``run:`` body the way Actions does, with a recording ``gh``.

    Returns the completed process and the argv of every ``gh`` call made.
    """
    script = _arm_step(_load())["run"]
    assert isinstance(script, str)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-argv.log"
    recorder = bin_dir / "gh"
    recorder.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$GH_ARGV_LOG"\nexit "${GH_RECORDER_STATUS:-0}"\n',
        encoding="utf-8",
        newline="\n",
    )
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "GH_TOKEN": "not-a-real-token",
            "PR_URL": PR_URL,
            "GH_ARGV_LOG": log.as_posix(),
            "GH_RECORDER_STATUS": str(gh_status),
        }
    )
    env.pop("PR_DRAFT", None)
    if draft is not None:
        env["PR_DRAFT"] = draft

    # Actions runs `run:` under `bash --noprofile --norc -eo pipefail {0}`.
    proc = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


# ---------------------------------------------------------------------------
# The shipped shell
# ---------------------------------------------------------------------------


def test_draft_dependency_update_does_not_arm_auto_merge(tmp_path: Path) -> None:
    """A Draft finishes green without calling gh at all (#6758)."""
    proc, calls = _run_arm_step(tmp_path, draft="true")

    assert proc.returncode == 0, proc.stderr
    assert calls == [], f"a Draft must not reach gh, but gh was called with {calls!r}"
    assert "draft" in proc.stdout.lower()


def test_ready_dependency_update_uses_the_merge_queue(tmp_path: Path) -> None:
    """A non-Draft makes exactly the one `gh pr merge --auto URL` call, with no extra flags."""
    proc, calls = _run_arm_step(tmp_path, draft="false")

    assert proc.returncode == 0, proc.stderr
    assert calls == [f"pr merge --auto {PR_URL}"]


def test_auto_merge_failure_is_not_hidden_for_ready_updates(tmp_path: Path) -> None:
    """gh's real exit status still fails the step; the Draft guard added no swallow."""
    proc, calls = _run_arm_step(tmp_path, draft="false", gh_status=7)

    assert proc.returncode == 7, (proc.stdout, proc.stderr)
    assert calls == [f"pr merge --auto {PR_URL}"]


@pytest.mark.parametrize(
    "draft", [None, "", "yes", "True", "null", "1"], ids=["unset", "empty", "yes", "True", "null", "1"]
)
def test_missing_or_invalid_draft_state_cannot_arm_auto_merge(tmp_path: Path, draft: str | None) -> None:
    """Anything but a literal true/false fails closed and never reaches gh."""
    proc, calls = _run_arm_step(tmp_path, draft=draft)

    assert proc.returncode != 0, "an ambiguous Draft state must not be treated as ready"
    assert calls == [], f"gh was called with {calls!r} on an ambiguous Draft state"
    assert "::error::" in proc.stdout


# ---------------------------------------------------------------------------
# The YAML the shell depends on
# ---------------------------------------------------------------------------


def test_ready_for_review_can_reach_dependency_auto_merge() -> None:
    """The default triggers stay, `ready_for_review` is added, and the shell reads the event's own Draft flag."""
    doc = _load()
    types = _on(doc)["pull_request"]["types"]
    assert set(types) == {"opened", "reopened", "synchronize", "ready_for_review"}, types

    step = _arm_step(doc)
    assert step["env"]["PR_DRAFT"] == "${{ github.event.pull_request.draft }}"
    assert step["env"]["PR_URL"] == "${{ github.event.pull_request.html_url }}"


def test_bot_identity_and_metadata_gates_are_unchanged() -> None:
    """The Draft guard sits inside the existing gates; it must not have loosened any of them."""
    doc = _load()
    job = doc["jobs"]["auto-merge"]

    condition = " ".join(str(job["if"]).split())
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in condition
    assert f"github.event.pull_request.user.id == {DEPENDABOT_USER_ID}" in condition
    assert "github.event.pull_request.user.type == 'Bot'" in condition

    step_condition = " ".join(str(_arm_step(doc)["if"]).split())
    assert "steps.metadata.outcome == 'success'" in step_condition
    assert "version-update:semver-patch" in step_condition
    assert "version-update:semver-minor" in step_condition
    assert "semver-major" not in step_condition

    assert doc["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert doc["concurrency"]["cancel-in-progress"] is True

    pinned = [str(s["uses"]) for s in job["steps"] if isinstance(s, dict) and "uses" in s]
    assert pinned, "the job lost its pinned actions"
    for uses in pinned:
        sha = uses.split("@", 1)[1].split()[0]
        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), f"{uses} is not pinned to a commit"
