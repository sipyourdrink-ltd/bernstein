"""``bernstein run --max-blast-radius`` must actually gate the merge (#3135).

The ceiling reaches the orchestrator as ``BERNSTEIN_MAX_BLAST_RADIUS``,
propagated by the ``bernstein run`` bootstrap.  Until this change nothing
read it on the merge path, so an operator who set a ceiling in CI watched
runs pass and concluded the ceiling held.

These tests drive the ceiling through the CLI's own propagation function
and then exercise the production merge path against a real git repository.
The merge callable is a recorder, so "the merge never ran" is observable
rather than inferred.  Calling ``install_blast_radius_gate`` directly is
deliberately avoided: the existing unit tests already do that and passed
throughout the period the flag did nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from bernstein.core.agents.spawner_merge import _do_merge
from bernstein.core.lifecycle.blast_radius_gate import ENV_MAX_BLAST_RADIUS

if TYPE_CHECKING:
    from pathlib import Path

_SESSION_ID = "backend-blastprobe"
_BRANCH = f"agent/{_SESSION_ID}"

# A hard one-way change: the shipped detectors force score 1.0 for a SQL
# DROP inside a migration, so any ceiling below 1.0 must refuse it.
_HARD_ONE_WAY_FILE = "migrations/0001_drop_users.sql"
_HARD_ONE_WAY_BODY = "DROP TABLE users;\n"

# A documentation-only change scores far below any sane ceiling.
_REVERSIBLE_FILE = "docs/notes.md"
_REVERSIBLE_BODY = "Some prose about the feature.\n"


@pytest.fixture(autouse=True)
def _restore_environ() -> Any:
    """``_propagate_env_flags`` writes ``os.environ`` directly, so snapshot it."""
    before = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(before)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _repo_with_agent_branch(tmp_path: Path, *, filename: str, body: str) -> Path:
    """Build a repo on a non-default trunk with one agent branch to merge.

    The trunk is deliberately not ``main``/``master`` so the pre-existing
    protected-default-branch guard does not fire and mask the result.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "trunk")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "Probe")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    _git(repo, "checkout", "-b", _BRANCH)
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", "agent work")
    _git(repo, "checkout", "trunk")
    return repo


def _set_ceiling_via_run_flag(ceiling: float | None) -> None:
    """Propagate ``--max-blast-radius`` exactly as ``bernstein run`` does."""
    from bernstein.cli.run_bootstrap import _propagate_env_flags

    _propagate_env_flags(
        profile=False,
        workflow=None,
        routing=None,
        compliance=None,
        sandbox=None,
        container=False,
        container_image=None,
        two_phase_sandbox=False,
        quiet=False,
        task_filter=None,
        auto_pr=False,
        activity_log_path=None,
        audit=False,
        max_blast_radius=ceiling,
    )


def _make_session() -> Any:
    class _Stub:
        id = _SESSION_ID
        task_ids = ["T-blast"]
        role = "backend"

    return _Stub()


class _RecordingMergeFn:
    """Records whether the merge was attempted at all."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, session_id: str, repo_root: Path) -> Any:
        self.calls.append(session_id)

        class _Result:
            success = True
            conflicting_files: list[str] = []
            error = None

        return _Result()


def _merge(repo: Path) -> tuple[Any, _RecordingMergeFn]:
    merge_fn = _RecordingMergeFn()
    with patch("bernstein.core.git_ops.safe_push") as safe_push:
        safe_push.return_value.ok = True
        safe_push.return_value.stderr = ""
        result = _do_merge(_make_session(), repo, {}, merge_fn)
    return result, merge_fn


# ---------------------------------------------------------------------------
# The flag is propagated where the gate looks for it
# ---------------------------------------------------------------------------


def test_run_flag_propagates_the_ceiling_the_gate_reads() -> None:
    """``--max-blast-radius`` lands on the env var the gate consumes."""
    _set_ceiling_via_run_flag(0.25)

    assert float(os.environ[ENV_MAX_BLAST_RADIUS]) == pytest.approx(0.25)


def test_no_flag_leaves_the_ceiling_unset() -> None:
    """Off by default: existing runs stay unaffected."""
    os.environ.pop(ENV_MAX_BLAST_RADIUS, None)
    _set_ceiling_via_run_flag(None)

    assert ENV_MAX_BLAST_RADIUS not in os.environ


# ---------------------------------------------------------------------------
# The ceiling is enforced on the production merge path
# ---------------------------------------------------------------------------


def test_merge_refused_when_change_exceeds_the_run_ceiling(tmp_path: Path) -> None:
    """A change above the ceiling is blocked, and the merge is never attempted."""
    repo = _repo_with_agent_branch(tmp_path, filename=_HARD_ONE_WAY_FILE, body=_HARD_ONE_WAY_BODY)
    _set_ceiling_via_run_flag(0.2)

    result, merge_fn = _merge(repo)

    assert merge_fn.calls == [], "the merge ran despite the ceiling"
    assert result is not None
    assert result.success is False
    error = result.error or ""
    assert "1.00" in error, f"the computed score is not named in the failure: {error!r}"
    assert "0.20" in error, f"the ceiling is not named in the failure: {error!r}"


def test_merge_proceeds_when_change_is_within_the_ceiling(tmp_path: Path) -> None:
    """A reversible change under the ceiling is merged unchanged."""
    repo = _repo_with_agent_branch(tmp_path, filename=_REVERSIBLE_FILE, body=_REVERSIBLE_BODY)
    _set_ceiling_via_run_flag(0.9)

    result, merge_fn = _merge(repo)

    assert merge_fn.calls == [_SESSION_ID], "an in-budget change was blocked"
    assert result is not None
    assert result.success is True


def test_merge_proceeds_when_no_ceiling_was_requested(tmp_path: Path) -> None:
    """With no ``--max-blast-radius`` the gate is a pass-through."""
    repo = _repo_with_agent_branch(tmp_path, filename=_HARD_ONE_WAY_FILE, body=_HARD_ONE_WAY_BODY)
    os.environ.pop(ENV_MAX_BLAST_RADIUS, None)

    result, merge_fn = _merge(repo)

    assert merge_fn.calls == [_SESSION_ID]
    assert result is not None
    assert result.success is True


def test_unusable_ceiling_refuses_rather_than_continuing(tmp_path: Path) -> None:
    """A ceiling that cannot be installed must not read as a passed gate.

    An operator who exported a malformed ceiling asked for enforcement.
    Continuing would teach them the ceiling held when it was never
    evaluated, which is the defect this issue is about.
    """
    repo = _repo_with_agent_branch(tmp_path, filename=_REVERSIBLE_FILE, body=_REVERSIBLE_BODY)
    os.environ[ENV_MAX_BLAST_RADIUS] = "not-a-float"

    result, merge_fn = _merge(repo)

    assert merge_fn.calls == [], "the merge ran with an unusable ceiling in force"
    assert result is not None
    assert result.success is False
    assert "not-a-float" in (result.error or "")


# ---------------------------------------------------------------------------
# The change being scored has to be readable before a score means anything
# ---------------------------------------------------------------------------


def _git_diff_fails(*, timeout: bool) -> Any:
    """Stand in for ``run_git`` so only the change read fails.

    The two failures the merge path actually sees: a non-zero ``git diff``,
    and the timeout a very large diff hits on the file-list call while
    ``git merge`` itself still succeeds -- the case where the merge lands and
    the ceiling was never evaluated against it.
    """
    from bernstein.core.git_ops import run_git as real_run_git

    def _fake(args: list[str], cwd: Path, **kwargs: Any) -> Any:
        if args[:2] == ["diff", "--name-only"]:
            if timeout:
                raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=30)
            return type("_Result", (), {"returncode": 128, "stdout": "", "stderr": "fatal: bad revision\n"})()
        return real_run_git(args, cwd, **kwargs)

    return _fake


@pytest.mark.parametrize("timeout", [False, True], ids=["non-zero-exit", "timed-out"])
def test_merge_refused_when_the_incoming_change_cannot_be_read(tmp_path: Path, timeout: bool) -> None:
    """An unscorable change must not score as a harmless one.

    An empty file list and an empty diff body are what a zero-risk change
    looks like, so a failed read walks straight through the ceiling. The
    merge itself does not depend on that read, so the change lands unjudged
    while the operator's ceiling reports that it held.
    """
    repo = _repo_with_agent_branch(tmp_path, filename=_HARD_ONE_WAY_FILE, body=_HARD_ONE_WAY_BODY)
    _set_ceiling_via_run_flag(0.2)

    with patch("bernstein.core.git_ops.run_git", _git_diff_fails(timeout=timeout)):
        result, merge_fn = _merge(repo)

    assert merge_fn.calls == [], "the merge ran on a change the ceiling never saw"
    assert result is not None
    assert result.success is False
    assert "could not be read" in (result.error or "")


def test_an_unreadable_change_is_inert_when_no_ceiling_was_requested(tmp_path: Path) -> None:
    """The asymmetry to preserve: no ceiling, no gate, nothing to fail closed.

    Refusing here would turn every flaky ``git diff`` into a lost merge for
    every run that never asked for a ceiling at all.
    """
    repo = _repo_with_agent_branch(tmp_path, filename=_REVERSIBLE_FILE, body=_REVERSIBLE_BODY)
    os.environ.pop(ENV_MAX_BLAST_RADIUS, None)

    with patch("bernstein.core.git_ops.run_git", _git_diff_fails(timeout=True)):
        result, merge_fn = _merge(repo)

    assert merge_fn.calls == [_SESSION_ID], "a run with no ceiling was gated anyway"
    assert result is not None
    assert result.success is True


def test_an_unreadable_change_is_recorded_under_its_own_reason(tmp_path: Path) -> None:
    """A ceiling that was exceeded and a change that could not be read are
    different findings, and an operator reading the refusal journal has to be
    able to tell which one they are looking at."""
    repo = _repo_with_agent_branch(tmp_path, filename=_HARD_ONE_WAY_FILE, body=_HARD_ONE_WAY_BODY)
    _set_ceiling_via_run_flag(0.2)

    with patch("bernstein.core.git_ops.run_git", _git_diff_fails(timeout=False)):
        _merge(repo)

    journal = repo / ".sdd" / "runtime" / "refused_merges.jsonl"
    reasons = [json.loads(line)["reason"] for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert reasons == ["blast-radius-unreadable"]
