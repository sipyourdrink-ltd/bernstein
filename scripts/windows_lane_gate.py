#!/usr/bin/env python3
"""Self-promoting gate for the Windows CI lane.

The Windows test job historically ran with a blanket ``continue-on-error:
true`` mask: every red Windows step was swallowed, so the lane could never
block a merge even once it had proven green. That is a required check with
a permanent hole -- a regression on a Windows host lands silently.

This gate replaces the blanket mask with a deterministic projection of two
inputs onto a blocking decision:

* ``result_code``  -- the exit code of the Windows test invocation.
* ``established``  -- whether a committed baseline marker records that the
  Windows lane has an established green history.

Projection (pure, host-independent, offline-verifiable):

======================  ===========  ===================================
result                  established  decision
======================  ===========  ===================================
green (``0``)           either       pass (exit 0)
red (non-zero)          ``False``    advisory (exit 0, ``::warning::``)
red (non-zero)          ``True``     blocked (exit 1, ``::error::``)
======================  ===========  ===================================

The ``non-blocking-if-no-history`` branch keeps a brand-new lane from
wedging the merge queue on its first red run; the ``else`` branch makes
the lane a real gate once it has earned a baseline. Promotion is therefore
a one-line data change (flip ``established`` in the committed baseline),
not a workflow edit, and it is reversible the same way.

Usage (from ``ci.yml``, after running the Windows suite and capturing its
exit code)::

    python scripts/windows_lane_gate.py --result "$rc"

The default baseline path is ``.github/windows-lane-baseline.json`` at the
repository root.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASELINE = ".github/windows-lane-baseline.json"

LEVEL_PASS = "pass"
LEVEL_ADVISORY = "advisory"
LEVEL_BLOCKED = "blocked"


@dataclass(frozen=True)
class GateDecision:
    """Deterministic outcome of the Windows-lane gate.

    Attributes:
        result_code: The exit code of the Windows test invocation.
        established: Whether a green baseline history was recorded.
        blocking: Whether the gate blocks the merge (fails the check).
        exit_code: Process exit code the gate itself returns.
        level: One of ``pass`` / ``advisory`` / ``blocked``.
        reason: Human-readable one-line justification.
    """

    result_code: int
    established: bool
    blocking: bool
    exit_code: int
    level: str
    reason: str

    def to_projection(self) -> dict[str, object]:
        """Return the decision as a plain dict for offline verification."""
        return {
            "result_code": self.result_code,
            "established": self.established,
            "blocking": self.blocking,
            "exit_code": self.exit_code,
            "level": self.level,
            "reason": self.reason,
        }


def decide(*, result_code: int, established: bool) -> GateDecision:
    """Project ``(result_code, established)`` onto a gate decision.

    Pure and side-effect free so the outcome is identical on every host and
    can be replayed offline from the recorded inputs.

    Args:
        result_code: Exit code of the Windows test invocation (0 == green).
        established: Whether the committed baseline records a green history.

    Returns:
        The :class:`GateDecision` for these inputs.
    """
    if result_code == 0:
        return GateDecision(
            result_code=result_code,
            established=established,
            blocking=False,
            exit_code=0,
            level=LEVEL_PASS,
            reason="Windows lane green",
        )
    if not established:
        return GateDecision(
            result_code=result_code,
            established=established,
            blocking=False,
            exit_code=0,
            level=LEVEL_ADVISORY,
            reason="Windows lane red but no green baseline yet - advisory, not blocking",
        )
    return GateDecision(
        result_code=result_code,
        established=established,
        blocking=True,
        exit_code=1,
        level=LEVEL_BLOCKED,
        reason="Windows lane red with an established green baseline - blocking",
    )


def _within(root: Path, candidate: Path) -> bool:
    """Return ``True`` when ``candidate`` is contained within ``root``.

    Uses realpath containment (the form CodeQL ``py/path-injection``
    recognises) so a baseline path that escapes the repo root is refused
    rather than opened.
    """
    try:
        root_real = os.path.realpath(root)
        cand_real = os.path.realpath(candidate)
    except OSError:
        return False
    return cand_real == root_real or cand_real.startswith(root_real + os.sep)


def load_baseline(path: str | Path, *, repo_root: str | Path | None = None) -> bool:
    """Return whether the committed baseline records an established history.

    A missing, malformed, or out-of-tree baseline is treated as *not
    established* (fail-open to advisory), so the gate degrades to
    non-blocking rather than wedging the queue on a read error.

    Args:
        path: Path to the baseline JSON file.
        repo_root: Optional containment root; when given, a ``path`` that
            resolves outside it is refused.

    Returns:
        ``True`` only when the file exists, parses, and sets
        ``established`` truthy.
    """
    candidate = Path(path)
    if repo_root is not None and not _within(Path(repo_root), candidate):
        return False
    try:
        raw = candidate.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("established", False))


def _emit(decision: GateDecision) -> None:
    """Print the decision as a GitHub-annotation-aware projection line."""
    projection = json.dumps(decision.to_projection(), sort_keys=True)
    if decision.level == LEVEL_BLOCKED:
        print(f"::error::windows-lane-gate: {decision.reason}")
    elif decision.level == LEVEL_ADVISORY:
        print(f"::warning::windows-lane-gate: {decision.reason}")
    print(f"windows-lane-gate: {projection}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the gate exit code."""
    parser = argparse.ArgumentParser(description="Self-promoting Windows CI lane gate.")
    parser.add_argument(
        "--result",
        type=int,
        required=True,
        help="Exit code of the Windows test invocation (0 == green).",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help=f"Path to the baseline marker (default: <repo>/{DEFAULT_BASELINE}).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    baseline_path = Path(args.baseline) if args.baseline else repo_root / DEFAULT_BASELINE
    # Only enforce repo containment for the built-in default location; an
    # operator-supplied --baseline (e.g. a test fixture path) is trusted.
    established = load_baseline(
        baseline_path,
        repo_root=repo_root if args.baseline is None else None,
    )

    decision = decide(result_code=args.result, established=established)
    _emit(decision)
    return decision.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
