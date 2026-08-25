"""A crash-recovery orphan must not pass the janitor unexamined.

Covers the per-task skip at the top of ``run_janitor``
(``src/bernstein/core/quality/janitor.py``). It used to drop every task that
declared no completion signals, which made the empty-diff guard twenty lines
below unreachable for the case its own comment names:

    # Empty-diff guard + attribution: a non-no-op task with zero attributable
    # changed files must NOT pass -- catches both the 0-file manager
    # rubber-stamp and crash-recovery orphan auto-completions with no diff.

A crash-recovery orphan auto-completion *is* a task nobody attached signals
to. So the guard never ran for it, and the task produced no ``JanitorResult``
at all - neither accepted nor rejected. The auto-completion simply stood, with
no evidence anything had been produced (#4562).

The distinction the fix rests on is between "no signals" and "nothing to
check". A signal-less task in a git workdir still has attributable work - or
demonstrably none - so it can be judged. A signal-less task in a non-git
workdir genuinely cannot be, and is still skipped; ``test_janitor.py``'s
``test_skips_tasks_without_signals`` pins that unchanged behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.quality.janitor import run_janitor
from bernstein.core.tasks.models import CompletionSignal, Task


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, capture_output=True, check=True)


def _task(
    task_id: str,
    *,
    signals: list[CompletionSignal] | None = None,
    parent: str | None = None,
    role: str = "qa",
) -> Task:
    return Task(
        id=task_id,
        title="Test task",
        description="A task for testing.",
        role=role,
        completion_signals=signals or [],
        parent_task_id=parent,
    )


@pytest.fixture
def git_workdir(tmp_path: Path) -> Path:
    """A git repo whose branch sits at its base commit - no task work landed."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


@pytest.mark.asyncio
async def test_signalless_task_with_no_diff_is_judged_not_skipped(git_workdir: Path) -> None:
    """The orphan from the issue: no signals, no commits, nothing produced.

    Reproduces the archived records quoted in #4562 - two qa tasks
    ``Auto-completed after agent qa-b58416c3 died; janitor passed`` whose run
    branch was still at its base commit.
    """
    orphan = _task("T-orphan")

    results = await run_janitor([orphan], git_workdir)

    assert len(results) == 1, "a signal-less task in a git repo must produce a verdict"
    assert results[0].task_id == "T-orphan"
    assert results[0].passed is False, "an orphan with no attributable work must not pass"


@pytest.mark.asyncio
async def test_the_rejection_names_the_empty_diff(git_workdir: Path) -> None:
    """The verdict has to say *why*, or an operator cannot act on it."""
    results = await run_janitor([_task("T-orphan")], git_workdir)

    failed = [desc for desc, passed, _ in results[0].signal_results if not passed]

    assert any("empty_diff" in desc for desc in failed), failed


@pytest.mark.asyncio
async def test_signalless_task_is_still_skipped_without_a_git_repo(tmp_path: Path) -> None:
    """No signals *and* no repo means nothing can be checked; skipping is right.

    This is the half of the old behaviour worth keeping - without it the fix
    would manufacture verdicts for unit tests and dry runs that have no work
    to attribute in the first place.
    """
    results = await run_janitor([_task("T-nosignals")], tmp_path)

    assert results == []


@pytest.mark.asyncio
async def test_a_signalled_task_is_unaffected(git_workdir: Path) -> None:
    """The green path for ordinary signalled tasks does not move."""
    (git_workdir / "done.py").write_text("pass\n", encoding="utf-8")
    signalled = _task("T-ok", signals=[CompletionSignal(type="path_exists", value="done.py")])

    results = await run_janitor([signalled], git_workdir)

    assert len(results) == 1
    assert results[0].task_id == "T-ok"


@pytest.mark.asyncio
async def test_orphan_alongside_a_real_task_does_not_mask_it(git_workdir: Path) -> None:
    """Both tasks get verdicts; the orphan's failure is its own."""
    (git_workdir / "done.py").write_text("pass\n", encoding="utf-8")
    tasks = [
        _task("T-orphan"),
        _task("T-ok", signals=[CompletionSignal(type="path_exists", value="done.py")]),
    ]

    results = await run_janitor(tasks, git_workdir)

    by_id = {r.task_id: r for r in results}
    assert set(by_id) == {"T-orphan", "T-ok"}
    assert by_id["T-orphan"].passed is False


@pytest.mark.asyncio
async def test_a_planner_that_decomposed_is_accepted_on_its_children(git_workdir: Path) -> None:
    """A planning task's artefact is the task graph, not a commit.

    Planning tasks are ``task_type == standard`` today, so before the skip was
    narrowed they never reached the empty-diff guard. Reaching it must not turn
    a silent false pass into a loud false failure: a planner that really planned
    would be reopened every run and re-decompose work it had already done.
    """
    planner = _task("T-manager", role="manager")
    child = _task("T-child", parent="T-manager", role="backend")

    results = await run_janitor([planner, child], git_workdir)
    by_id = {r.task_id: r for r in results}

    assert by_id["T-manager"].passed is True
    accepted = [desc for desc, passed, _ in by_id["T-manager"].signal_results if passed]
    assert any("decomposed_children" in desc for desc in accepted), accepted


@pytest.mark.asyncio
async def test_a_planner_that_decomposed_nothing_still_fails(git_workdir: Path) -> None:
    """The pairing that stops the exemption becoming a hole.

    Without this, "planning task" would be a way to pass with no evidence at
    all - which is the same silence #4562 closed, wearing a different label.
    """
    planner = _task("T-manager", role="manager")

    results = await run_janitor([planner], git_workdir)

    assert results[0].passed is False
    failed = [desc for desc, passed, _ in results[0].signal_results if not passed]
    assert any("empty_diff" in desc for desc in failed), failed


@pytest.mark.asyncio
async def test_children_of_another_parent_are_not_evidence(git_workdir: Path) -> None:
    """Evidence must name *this* task, or any planner passes off any graph."""
    planner = _task("T-manager", role="manager")
    unrelated_child = _task("T-child", parent="T-someone-else", role="backend")

    results = await run_janitor([planner, unrelated_child], git_workdir)
    by_id = {r.task_id: r for r in results}

    assert by_id["T-manager"].passed is False
