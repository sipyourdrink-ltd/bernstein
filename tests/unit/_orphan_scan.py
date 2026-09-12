"""Shared caller-less-module ratchet: a combined, self-explaining failure (#5552).

``test_token_orphans.py`` and ``test_compliance_module_reachability.py`` each
carry a frozen ``KNOWN_ORPHANS`` snapshot compared against a freshly computed
set, in two separate ``assert`` statements. That shape has two independent
defects, both filed under #5552:

1. **It stops at the first assertion.** A snapshot that has drifted in both
   directions at once -- some entries newly caller-less, others no longer
   caller-less -- passes the first ``assert`` and only then fails the
   second, so a naive refresh that adds the missing names still reds on the
   very next run. :func:`describe_ratchet_drift` reports both directions in
   one message.
2. **The message accuses the wrong side.** The snapshot is a frozen picture
   of the tree at the moment it was captured; the tree keeps moving after
   that. When an unrelated commit on the default branch adds or removes a
   caller between the snapshot and this run, the failure message reads
   identically to "this change broke something" -- there is nothing in a
   two-set comparison that can tell the two apart. :func:`resolve_branch_only_ref`
   and :func:`scan_at_ref` recover the missing signal, when the environment
   makes it available: a merge-queue run's ``HEAD`` is a merge commit whose
   second parent is the PR branch's own tip, unmerged with whatever landed
   on the default branch since. Scanning *that* tree in isolation and
   comparing it to the baseline is what lets the message say, correctly,
   "this branch's tree already matched the baseline; look at the default
   branch instead."

That second signal is best-effort by construction, not a hard dependency.
The most common CI shape for these guards -- a shallow, single-commit
checkout on a plain (non-merge-queue) PR-lane run -- has no second parent to
find, and a shallow clone may not have the object even when one exists. Both
functions return ``None`` on any such failure rather than raising, and
``describe_ratchet_drift`` treats ``None`` as "cannot tell", never as
"the branch is at fault". The combined-message improvement in point 1 above
holds unconditionally, with or without this signal.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

#: Timeout for the git plumbing calls below. Generous for a local operation
#: that only ever touches this repository's own object store.
_GIT_TIMEOUT_SECONDS = 30
#: `git worktree add` clones a working tree, not just reads an object -
#: proportionally larger repositories or slow disks want more room than a
#: `rev-parse`.
_WORKTREE_TIMEOUT_SECONDS = 90


def resolve_branch_only_ref(repo_root: Path) -> str | None:
    """Return HEAD's second parent SHA, or ``None`` when there isn't one.

    A merge-queue run's ``HEAD`` is a merge of the PR branch onto the
    up-to-date default branch: parent 1 is that default-branch tip, parent 2
    is the PR branch's own tip, unmerged. A plain PR-lane run's ``HEAD`` has
    no second parent at all -- it has not been merged with anything yet, so
    there is nothing to distinguish it from.

    Returns ``None``, never raises, when there is no second parent, when git
    itself is unavailable, or when the object exists in history but a
    shallow clone never fetched it. All three read identically to a caller:
    "this signal is not available right now".
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "-q", "HEAD^2"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def scan_at_ref(
    ref: str,
    repo_root: Path,
    scan_fn: Callable[[Path], set[str]],
) -> set[str] | None:
    """Run ``scan_fn`` against a disposable worktree checked out at ``ref``.

    ``scan_fn`` receives the worktree's root and returns the computed
    orphan-name set; it is the guard's own ``_current_orphans``-equivalent,
    reused unchanged against a different tree.

    Returns ``None`` on any git failure -- ``ref`` unreachable in a shallow
    clone, ``worktree add`` refused, no space left, and so on -- rather than
    raising, for the same reason :func:`resolve_branch_only_ref` returns
    ``None``: an inability to check must never be misread as evidence.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="bernstein-orphan-scan-"))
    tmp_dir.rmdir()  # `worktree add` requires the path not to exist yet.
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                "--detach",
                "--quiet",
                str(tmp_dir),
                ref,
            ],
            capture_output=True,
            text=True,
            timeout=_WORKTREE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        try:
            return scan_fn(tmp_dir)
        except OSError:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(tmp_dir)],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)


def describe_ratchet_drift(
    *,
    baseline: frozenset[str],
    current: set[str],
    branch_only: set[str] | None,
    guard_name: str,
    wire_hint: str,
) -> str | None:
    """Return one combined failure message covering both drift directions.

    Returns ``None`` when ``current == baseline`` -- nothing to report.
    Otherwise the message names every entry that appeared and every entry
    that disappeared, in the same message, so fixing one direction cannot
    hide the other until a later run.

    When ``branch_only`` is available (see :func:`resolve_branch_only_ref` /
    :func:`scan_at_ref`) and it matches ``baseline`` exactly while ``current``
    does not, the message states that this branch's own tree is clean and
    names the default branch as the source of the drift instead. When
    ``branch_only`` is available and itself differs from ``baseline``, the
    entries it newly introduces are named as this branch's own doing.
    ``branch_only is None`` adds no such claim either way -- the base
    message from the first paragraph is everything that can be said.

    Args:
        baseline: The frozen, committed set of known caller-less names.
        current: The set computed against the tree this test is running on
            (the merge result in a merge-queue run, the branch tree itself
            in a plain PR-lane run).
        branch_only: The set computed against the PR branch's own tip in
            isolation, or ``None`` when that signal could not be obtained.
        guard_name: Short label identifying which guard is reporting,
            e.g. ``"core/tokens/"``.
        wire_hint: One sentence telling the reader what to do about a newly
            caller-less module -- wire it to a real consumer, or delete it.
    """
    baseline_set: set[str] = set(baseline)
    if current == baseline_set:
        return None

    appeared = sorted(current - baseline_set)
    disappeared = sorted(baseline_set - current)

    lines = [f"{guard_name}: the caller-less-module baseline no longer matches the tree."]
    if appeared:
        lines.append(f"  new caller-less modules: {appeared}. {wire_hint}")
    if disappeared:
        lines.append(
            f"  no longer caller-less (or gone from the tree): {disappeared}. Strike these "
            "from the known-orphans set so it keeps shrinking."
        )

    if branch_only is not None:
        if branch_only == baseline_set:
            lines.append(
                "  This branch's own tree matches the baseline exactly -- the drift above "
                "comes from commits that landed on the default branch after the baseline was "
                "captured, not from this change. Re-take the baseline; do not look for what "
                "this branch broke."
            )
        else:
            branch_appeared = sorted(branch_only - baseline_set)
            branch_disappeared = sorted(baseline_set - branch_only)
            if branch_appeared or branch_disappeared:
                lines.append(
                    "  This branch's own tree already differs from the baseline "
                    f"(new: {branch_appeared}, resolved: {branch_disappeared}) -- at least "
                    "part of the drift above belongs to this change."
                )

    return "\n".join(lines)
