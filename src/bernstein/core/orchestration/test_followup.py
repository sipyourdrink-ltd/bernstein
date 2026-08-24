"""One bounded test-authoring follow-up for a run that changed src/ without tests/ (#4462).

An unattended run that touches ``src/*`` but never ``tests/*`` is a dead end
today: the merge gate correctly refuses a source change with no test
evidence, the branch parks, and nothing re-drives a second run until an
operator notices. This module supplies the missing step: at the quiescence
self-stop in ``Orchestrator._tick_internal`` (see
``core.orchestration.run_stall`` for the sibling terminal-state fix at the
same call site), decide whether the run's branch needs exactly one bounded
follow-up task that writes the missing tests, and never more than one.

Two layers, deliberately kept apart so the decision itself needs no git, no
HTTP client, and no clock:

* :func:`evaluate_test_followup` is the pure criterion. It is the single
  place the four required behaviors are pinned: trigger on src-without-tests,
  don't trigger when tests are already present, never trigger a second time
  in the same run (the ``already_scheduled`` latch), and don't trigger at all
  when disabled.
* :func:`resolve_run_branch` and :func:`diff_name_only` do the actual git
  work an orchestrator needs to turn a completed run into the
  ``changed_files`` the criterion reads. They are the only impure functions
  here; everything else takes already-resolved data.

Which way the criterion errs: toward NOT scheduling. A branch whose diff
cannot be resolved, or a follow-up that itself lands src-only changes, both
fall through to "run continues to its ordinary self-stop" rather than
retrying - matching the "one attempt only" requirement in the issue.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import run_git
from bernstein.core.path_scope import normalise_repo_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from bernstein.core.tasks.models import Task

logger = logging.getLogger(__name__)

#: Overrides ``orchestration.test_followup`` from bernstein.yaml at runtime,
#: for headless operators who cannot edit the seed file. Same truthy/falsy
#: vocabulary as every other boolean env override in this project.
ENV_TEST_FOLLOWUP = "BERNSTEIN_TEST_FOLLOWUP"

_TRUTHY = frozenset({"1", "true", "yes", "on", "enable", "enabled"})
_FALSY = frozenset({"0", "false", "no", "off", "disable", "disabled"})


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


def _is_src_path(path: str) -> bool:
    return path == "src" or path.startswith("src/")


def _is_test_path(path: str) -> bool:
    return path == "tests" or path.startswith("tests/")


@dataclass(frozen=True)
class DiffClassification:
    """Which side of the src/tests boundary each changed path falls on.

    Paths outside both ``src/`` and ``tests/`` (docs, config, workflow files)
    are dropped: they never satisfy the "tests already present" exemption,
    so scoping to only these two directories is equivalent to tracking them
    and ignoring them at every read site.
    """

    src_files: tuple[str, ...]
    test_files: tuple[str, ...]

    @property
    def needs_followup(self) -> bool:
        """True when the diff touched src/ and never touched tests/."""
        return bool(self.src_files) and not self.test_files


def classify_diff(changed_files: Iterable[str]) -> DiffClassification:
    """Partition a diff's changed paths into src/tests, deduped and ordered.

    Paths are normalised the same way ``git diff --name-only`` output is
    normalised everywhere else in this codebase (see
    :func:`bernstein.core.path_scope.normalise_repo_path`), so a leading
    ``./`` or a Windows-style backslash cannot hide a path from either
    bucket.
    """
    src: list[str] = []
    tests: list[str] = []
    seen: set[str] = set()
    for raw in changed_files:
        path = normalise_repo_path(raw)
        if not path or path in seen:
            continue
        seen.add(path)
        if _is_test_path(path):
            tests.append(path)
        elif _is_src_path(path):
            src.append(path)
    return DiffClassification(src_files=tuple(src), test_files=tuple(tests))


@dataclass(frozen=True)
class TestFollowupDecision:
    """Outcome of :func:`evaluate_test_followup`.

    Attributes:
        should_schedule: True only when a bounded follow-up task should be
            created. Callers must not self-stop the run this tick when True
            -- the run just gained one more task to execute.
        reason: Machine-readable verdict tag, logged by the caller so a
            grep of the logs always finds an explicit answer for why a run
            did or did not get a follow-up.
        src_files: The src/ paths the follow-up's goal should name. Empty
            unless ``should_schedule`` is True.
    """

    should_schedule: bool
    reason: str
    src_files: tuple[str, ...] = ()


def evaluate_test_followup(
    *,
    enabled: bool,
    already_scheduled: bool,
    changed_files: Iterable[str],
) -> TestFollowupDecision:
    """Decide whether this run's completion warrants one test-authoring follow-up.

    Pure: reads only its arguments, performs no IO.

    Args:
        enabled: Effective ``orchestration.test_followup`` setting (see
            :func:`resolve_test_followup_enabled`).
        already_scheduled: True once a follow-up has already been scheduled
            for this run. This is the bounding latch: it stays True for the
            rest of the run's lifetime, including across the quiescence the
            follow-up task's OWN completion produces, so a follow-up that
            itself lands src-only changes can never trigger a second one.
        changed_files: The run branch's changed paths (``git diff
            --name-only`` shape).

    Returns:
        The verdict. ``should_schedule`` is True only for a first-ever,
        enabled, src-without-tests diff.
    """
    if not enabled:
        return TestFollowupDecision(should_schedule=False, reason="disabled")
    if already_scheduled:
        return TestFollowupDecision(should_schedule=False, reason="already_scheduled")

    classification = classify_diff(changed_files)
    if not classification.src_files:
        return TestFollowupDecision(should_schedule=False, reason="no_src_changes")
    if classification.test_files:
        return TestFollowupDecision(should_schedule=False, reason="tests_present")
    return TestFollowupDecision(
        should_schedule=True,
        reason="src_without_tests",
        src_files=classification.src_files,
    )


def build_followup_goal(src_files: Sequence[str]) -> str:
    """Deterministic goal text for the bounded test-authoring follow-up.

    Carries the file list so the follow-up agent - and anyone reading the
    task - knows exactly which change needs coverage without re-deriving
    the diff. Deterministic in file order (callers pass the already-ordered
    tuple :func:`classify_diff` produced), so the same diff always produces
    the same goal text.
    """
    file_list = "\n".join(f"- {f}" for f in src_files)
    return (
        "This branch changed source files with no corresponding test changes. "
        "Write tests that assert the behavior this diff introduced or changed - "
        "assert the defect it would leave uncaught, not the implementation. "
        "Touch only tests/; do not modify src/.\n\n"
        f"Source files changed without tests:\n{file_list}"
    )


def resolve_test_followup_enabled(config_value: bool, env: Mapping[str, str] | None = None) -> bool:
    """Resolve whether the test-authoring follow-up is enabled, env-first.

    Precedence: an explicit ``BERNSTEIN_TEST_FOLLOWUP`` env value wins
    (truthy enables, falsy disables); otherwise ``config_value`` (the seed's
    ``orchestration.test_followup``, default ``True``) applies.

    Args:
        config_value: ``OrchestratorConfig.test_followup_enabled``, itself
            sourced from ``orchestration.test_followup`` in bernstein.yaml.
        env: Environment mapping to consult; defaults to ``os.environ``.

    Returns:
        The effective enabled state for this process.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_TEST_FOLLOWUP)
    if raw is None:
        return bool(config_value)
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    logger.warning(
        "%s=%r not understood; using orchestration.test_followup from config",
        ENV_TEST_FOLLOWUP,
        raw,
    )
    return bool(config_value)


# ---------------------------------------------------------------------------
# Git integration (impure - the only functions here that touch the repo)
# ---------------------------------------------------------------------------


def resolve_run_branch(workdir: Path, done_tasks: Sequence[Task]) -> str | None:
    """Return the agent branch of the most recently completed task that still exists.

    Each spawned agent works on its own ``agent/<session_id>`` branch (see
    ``core.git.worktree.WorktreeManager.create``); there is no single branch
    that spans a whole run. At run completion the most recently completed
    task's branch is the one a merge gate would currently be evaluating, so
    that is what "the run's branch" means here. A task whose branch has
    already been merged and deleted is skipped in favour of an earlier task
    whose branch still resolves.

    Args:
        workdir: Repository root to resolve branches in.
        done_tasks: The run's completed tasks (any status/order).

    Returns:
        The branch name (``agent/<session_id>``), or ``None`` if no done
        task has a resolvable branch.
    """
    candidates = sorted(
        (t for t in done_tasks if t.assigned_agent and t.completed_at),
        key=lambda t: t.completed_at or 0.0,
        reverse=True,
    )
    for task in candidates:
        branch = f"agent/{task.assigned_agent}"
        probe = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], workdir, timeout=5)
        if probe.ok:
            return branch
    return None


def diff_name_only(workdir: Path, base_branch: str, branch: str) -> tuple[str, ...]:
    """Return the paths changed on ``branch`` relative to its merge-base with ``base_branch``.

    Three-dot diff (symmetric difference from the merge base) - the same
    shape the dashboard's per-task diff view and the merge gate itself read,
    so this reports exactly what a human or the gate would see for this
    branch, not a two-dot diff against base's current tip.

    Returns an empty tuple (rather than raising) on any git failure, so a
    transient git error degrades to "no follow-up this tick" instead of
    crashing the tick loop.
    """
    result = run_git(["diff", "--name-only", f"{base_branch}...{branch}"], workdir, timeout=15)
    if not result.ok:
        logger.warning(
            "test_followup: git diff --name-only %s...%s failed: %s",
            base_branch,
            branch,
            result.stderr.strip(),
        )
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
