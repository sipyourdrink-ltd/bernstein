"""Nightly contamination-resistant eval driver - minimal viable slice.

This module produces a single leaderboard JSON entry from one (potentially
synthetic) task. Full HuggingFace dataset loaders and the multi-instance
runner are deferred - see ``.sdd/backlog/closed/`` for the parent ticket.

The output shape is the canonical Terminal-Bench 2.0 ``harness@version +
model@version`` pair record (see ticket lines 95-100). All three target
suites - SWE-bench Pro, Terminal-Bench 2.0, SWE-rebench - share this
record; the suite name disambiguates.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bernstein import __version__ as _bernstein_version

EvalSuite = Literal["swe-bench-pro", "terminal-bench-2", "swe-rebench"]
"""The three contamination-resistant suites this harness targets."""

_HARNESS_NAME = "bernstein"

# Schema version for the leaderboard JSON entry. Bump when fields change in a
# way that breaks downstream consumers (the public bernstein-eval repo, the
# Stanford submission script, or `/eval/leaderboard` once shipped).
_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """A single contamination-resistant eval result.

    Captures the four-field harness-model pair record required by the
    Terminal-Bench 2.0 submission contract (``harness_name``,
    ``harness_version``, ``model``, ``model_version``) and reuses it for
    SWE-bench Pro and SWE-rebench so a single dashboard renders all three.

    Attributes:
        schema_version: JSON shape version; bump on breaking changes.
        suite: Which contamination-resistant suite this entry came from.
        task_id: Stable identifier for the single task that was run.
        harness_name: Always ``bernstein`` for entries we produce.
        harness_version: ``bernstein.__version__`` at run time.
        model: Adapter identifier (e.g. ``claude-opus-4-7``).
        model_version: Provider-reported model version string.
        resolved: Whether the agent's patch passed all required tests.
        cost_usd: Reported cost in USD for this task.
        duration_seconds: End-to-end wall-clock time for this single task.
        run_at: ISO-8601 UTC timestamp attached to the result.
        extra: Free-form per-suite metadata (language, repo visibility,
            rebench-month, etc.). Kept loose intentionally - full schema
            lands when the per-suite loaders do.
    """

    schema_version: str
    suite: EvalSuite
    task_id: str
    harness_name: str
    harness_version: str
    model: str
    model_version: str
    resolved: bool
    cost_usd: float
    duration_seconds: float
    run_at: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render as a plain dict for JSON serialisation.

        Returns:
            A dict suitable for ``json.dumps(..., sort_keys=True)``; the
            shape is the public leaderboard contract.
        """
        return dataclasses.asdict(self)

    def fingerprint(self) -> str:
        """Stable SHA-256 hash of the harness-model pair for dedup keys.

        The leaderboard route deduplicates entries by this fingerprint so
        a re-run against the same pair updates rather than appends.

        Returns:
            64-char lower-case hex digest.
        """
        material = "|".join(
            [
                self.suite,
                self.task_id,
                self.harness_name,
                self.harness_version,
                self.model,
                self.model_version,
            ],
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def run_single_task(
    *,
    suite: EvalSuite,
    task_id: str,
    model: str,
    model_version: str,
    resolved: bool,
    cost_usd: float = 0.0,
    duration_seconds: float = 0.0,
    extra: dict[str, Any] | None = None,
    run_at: str | None = None,
) -> LeaderboardEntry:
    """Build a leaderboard entry for one already-executed task.

    This is the smallest viable slice: callers (the nightly workflow, a
    real loader, or a test) handle the actual benchmark execution and pass
    in the outcome. The full integrated runner (download → run → verify)
    is deferred until the per-suite loaders land.

    Args:
        suite: Which contamination-resistant suite the task is from.
        task_id: Stable identifier for the task.
        model: Adapter / model identifier (e.g. ``claude-opus-4-7``).
        model_version: Provider-reported model version string.
        resolved: True iff the agent's patch satisfied the suite's grading.
        cost_usd: Cost in USD supplied by the caller. Defaults to 0.0.
        duration_seconds: Wall-clock time; defaults to 0.0 for synthetic
            inputs.
        extra: Optional free-form per-suite metadata.
        run_at: ISO-8601 UTC timestamp. None uses the current UTC time
            formatted as an ISO-8601 string.

    Returns:
        A populated :class:`LeaderboardEntry`.
    """
    if run_at is None:
        run_at = datetime.now(UTC).isoformat()
    return LeaderboardEntry(
        schema_version=_SCHEMA_VERSION,
        suite=suite,
        task_id=task_id,
        harness_name=_HARNESS_NAME,
        harness_version=_bernstein_version,
        model=model,
        model_version=model_version,
        resolved=resolved,
        cost_usd=cost_usd,
        duration_seconds=duration_seconds,
        run_at=run_at,
        extra=dict(extra) if extra else {},
    )


def write_leaderboard_entry(entry: LeaderboardEntry, out_dir: Path) -> Path:
    """Append a leaderboard entry to the canonical JSONL file.

    The nightly job appends; the public bernstein-eval repo (deferred)
    will replay this file to produce the dashboard JSON.

    Args:
        entry: Result to persist.
        out_dir: Directory under which the suite-specific JSONL lives.
            Typically ``.sdd/runtime/eval/<suite>/``.

    Returns:
        Path to the JSONL that was appended to.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{entry.suite}.jsonl"
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.to_dict(), sort_keys=True))
        handle.write("\n")
    return out_path


def render_badge_markdown(entries: list[LeaderboardEntry]) -> str:
    """Render a small markdown badge fragment summarising the latest run.

    The nightly workflow commits this fragment so the README / docs site
    can ``include`` it without parsing JSON.

    Args:
        entries: Latest entry per (suite, model) pair.

    Returns:
        Markdown source - three rows, one per suite, with a resolve-rate
        column. Empty input yields a placeholder.
    """
    if not entries:
        return "_No contamination-resistant runs recorded yet._\n"
    by_suite: dict[str, list[LeaderboardEntry]] = {}
    for e in entries:
        by_suite.setdefault(e.suite, []).append(e)
    lines = [
        "| Suite | Harness | Model | Resolved | Run at |",
        "|---|---|---|---|---|",
    ]
    # Stable suite ordering: Pro, Terminal, rebench (matches docs narrative).
    suite_order: tuple[EvalSuite, ...] = (
        "swe-bench-pro",
        "terminal-bench-2",
        "swe-rebench",
    )
    for suite in suite_order:
        for e in by_suite.get(suite, []):
            lines.append(
                f"| {e.suite} | {e.harness_name}@{e.harness_version} | "
                f"{e.model}@{e.model_version} | "
                f"{'yes' if e.resolved else 'no'} | {e.run_at} |",
            )
    return "\n".join(lines) + "\n"


def synthetic_smoke_entry() -> LeaderboardEntry:
    """Produce a deterministic synthetic entry for CI smoke / docs.

    Used by the nightly workflow when ``EVAL_ENABLED`` is unset so that
    the workflow self-tests the JSON shape without spending API budget.

    Returns:
        A :class:`LeaderboardEntry` against ``swe-bench-pro`` with the
        ``synthetic`` model identifier.
    """
    return run_single_task(
        suite="swe-bench-pro",
        task_id="synthetic__smoke-001",
        model="synthetic",
        model_version="0",
        resolved=True,
        cost_usd=0.0,
        duration_seconds=0.0,
        extra={"note": "synthetic smoke entry; replace with real run"},
        run_at="2026-01-01T00:00:00+00:00",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the nightly workflow.

    Wired so the workflow can run ``python -m bernstein.evals.driver``
    even before the full ``bernstein eval bench`` Typer command lands.

    Args:
        argv: Optional argv override (for testing).

    Returns:
        Process exit code (always ``0`` on the smoke path).
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Run a synthetic smoke entry through the eval driver.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path.cwd() / ".sdd" / "runtime" / "eval",
        help="Output directory for the leaderboard JSONL.",
    )
    parser.add_argument(
        "--badge",
        type=Path,
        default=None,
        help="Optional path to write the markdown badge fragment.",
    )
    args = parser.parse_args(argv)

    entry = synthetic_smoke_entry()
    out_path = write_leaderboard_entry(entry, args.out / entry.suite)
    sys.stdout.write(f"wrote {out_path}\n")
    if args.badge:
        args.badge.parent.mkdir(parents=True, exist_ok=True)
        args.badge.write_text(render_badge_markdown([entry]), encoding="utf-8")
        sys.stdout.write(f"wrote badge {args.badge}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())


__all__ = [
    "EvalSuite",
    "LeaderboardEntry",
    "main",
    "render_badge_markdown",
    "run_single_task",
    "synthetic_smoke_entry",
    "write_leaderboard_entry",
]
