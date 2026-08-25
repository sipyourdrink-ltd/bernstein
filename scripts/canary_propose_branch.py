#!/usr/bin/env python3
"""Build the canary's proposal branch on a freshly fetched default branch.

The nightly adapter-conformance canary proposes its regeneration through a
long-lived pull request on ``bot/adapter-canary-last-green``. The step used to
build that branch with ``git checkout -B`` off the *workflow checkout*, then
force-push. The checkout is taken at the start of the run; the default branch
keeps moving while the matrix probes every adapter. A night that regenerates
nothing exits before pushing, so the branch can also sit at an older commit
across nights.

Either way the branch's base drifts behind the default branch, and anything
merged in that window shows up in the pull request as a *revert* of work the
canary never touched. #4496 caught exactly that: `docs/security/receipt-format-
spec.md` came back to a form predating the edits #4489 landed, and a squash
merge would have shipped the revert.

So the branch is rebuilt here off ``origin/<default>`` as fetched at commit
time, the regenerated blobs are replayed onto that tree, and the resulting
changed-file set is asserted against the merge base before anything is
committed. A stray path fails the step: the alternative - dropping it with a
warning - would let a genuine projection bug leave the tree silently, which is
the failure mode this exists to stop.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: Paths the canary regenerates, and the only paths its proposal may carry.
PROJECTION_PATHS = (
    "src/bernstein/adapters/last_green.json",
    "docs/adapters/conformance-canary.md",
)


class ProposalError(RuntimeError):
    """The proposal branch could not be built safely."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run ``git`` inside ``repo`` and return stdout stripped."""
    proc = subprocess.run(
        ("git", *args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise ProposalError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def changed_paths(repo: Path, base_ref: str) -> tuple[str, ...]:
    """Paths that differ between ``base_ref``'s merge base and the index."""
    merge_base = _git(repo, "merge-base", base_ref, "HEAD")
    out = _git(repo, "diff", "--cached", "--name-only", merge_base)
    return tuple(sorted(line for line in out.splitlines() if line))


def build_proposal(
    repo: Path,
    *,
    branch: str,
    base_ref: str,
    remote: str = "origin",
    expected: tuple[str, ...] = PROJECTION_PATHS,
    fetch: bool = True,
) -> tuple[str, ...]:
    """Rebuild ``branch`` on a freshly fetched ``base_ref`` carrying only ``expected``.

    Reads the regenerated blobs out of the working tree first, resets the
    branch onto the fetched base, replays them, and returns the staged
    changed-file set. Raises :class:`ProposalError` if that set is not a
    subset of ``expected`` - a path the canary does not regenerate must never
    ride along.
    """
    staged = {path: (repo / path).read_bytes() for path in expected if (repo / path).exists()}
    if not staged:
        raise ProposalError(f"none of the projection paths exist under {repo}: {expected}")

    if fetch:
        _git(repo, "fetch", remote, base_ref)
        base = f"{remote}/{base_ref}"
    else:
        base = base_ref

    _git(repo, "checkout", "-B", branch, base)

    for path, blob in staged.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)

    _git(repo, "add", *staged)
    changed = changed_paths(repo, base)

    stray = tuple(path for path in changed if path not in expected)
    if stray:
        raise ProposalError(
            "proposal would carry paths the canary does not regenerate: "
            + ", ".join(stray)
            + f" (expected only {', '.join(expected)})"
        )
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--branch", default="bot/adapter-canary-last-green")
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--message", default="docs: regenerate adapter last-green table from canary receipts")
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Build and check the branch but leave the commit to the caller.",
    )
    args = parser.parse_args(argv)

    try:
        changed = build_proposal(
            args.repo,
            branch=args.branch,
            base_ref=args.base_ref,
            remote=args.remote,
        )
    except ProposalError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if not changed:
        print("projection matches the fetched base; nothing to propose")
        return 0

    print("proposal carries exactly: " + ", ".join(changed))
    if not args.no_commit:
        _git(args.repo, "commit", "-m", args.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
