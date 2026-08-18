#!/usr/bin/env python3
"""Total-coverage monotonic ratchet for Bernstein.

Two levers, both **advisory** until an operator promotes them (see
``docs/operations/coverage-ratchet.md``):

LEVEL 2 - total coverage ratchet (this script's ``check`` command).
    Reads the line-coverage total out of the Cobertura ``coverage.xml``
    that the CI coverage shard already produces, and compares it to the
    committed high-water mark in ``.coverage-baseline.json``.

    Every mark the ratchet writes records the raw ``line-rate`` it was
    rounded from, plus the commit and CI run it was measured on, so the
    committed percentage can be re-derived and checked from the committed
    file alone (``verify``). ``check`` refuses to run against a baseline
    whose percentage does not follow from its own line-rate.

    - measured < baseline (beyond a small float tolerance): the ratchet
      reports a drop and exits non-zero. The CI job keeps this advisory
      via ``continue-on-error`` so a drop never wedges the merge queue.
    - measured > baseline: the ratchet *clicks* - it rewrites the
      baseline to the new high-water mark and exits zero. The push-side
      workflow commits the bumped baseline back to ``main``.
    - measured == baseline (within tolerance): pass, no write.

LEVEL 1 - diff-coverage floor (this script's ``bump-floor`` command).
    The per-PR diff-cover gate reads its ``--fail-under`` floor from the
    same baseline file (``diff_coverage_floor_percent``) so there is one
    source of truth. The weekly workflow nudges that floor up by a gentle
    increment, capped, and opens a review PR.

The module is import-safe (no work at import time) so the unit tests can
drive the pure functions directly.

Usage
-----

    # LEVEL 2: compare coverage.xml total to the baseline; bump on a rise.
    python scripts/coverage_ratchet.py check \\
        --coverage-xml coverage.xml \\
        --baseline .coverage-baseline.json \\
        [--head-sha <sha>] [--run-id <id>] \\
        [--tolerance 0.05] [--no-bump]

    # Re-derive the committed percentage offline (no coverage.xml needed).
    python scripts/coverage_ratchet.py verify \\
        --baseline .coverage-baseline.json \\
        [--require-provenance]

    # LEVEL 1: raise the committed diff-coverage floor by one step.
    python scripts/coverage_ratchet.py bump-floor \\
        --baseline .coverage-baseline.json \\
        [--step 1] [--cap 90]

    # Seed the baseline from a freshly-measured coverage.xml.
    python scripts/coverage_ratchet.py init \\
        --coverage-xml coverage.xml \\
        --baseline .coverage-baseline.json \\
        [--diff-floor 80]

    # Print the current diff-coverage floor (for the CI step to consume).
    python scripts/coverage_ratchet.py show-floor \\
        --baseline .coverage-baseline.json

Exit codes
----------

- 0: success - coverage held or rose, or a non-``check`` command ran.
- 1: ``check`` found a coverage drop beyond tolerance (advisory in CI).
- 2: misconfiguration - bad args, missing baseline, unreadable input.
- 3: ``coverage.xml`` was missing or malformed (treated as soft-skip by
     the workflow; a missing report must not be read as a drop).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

# defusedxml is a drop-in for xml.etree.ElementTree that re-exports the
# stdlib ``ParseError`` and additionally raises ``DefusedXmlException``
# (a ValueError subclass) on disallowed constructs (DTDs, entity
# expansion). The coverage report is locally produced, but parsing it
# with the hardened parser keeps this script aligned with the repo's
# XML-handling convention and silences bandit's B314.
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

# Float jitter between two coverage runs of the same tree is normally
# << 0.05 percentage points; anything inside this band is treated as
# "flat" so noise never trips the gate or churns the baseline.
DEFAULT_TOLERANCE: float = 0.05

# LEVEL 1 weekly-bump knobs. Operator-tunable in the weekly workflow.
DEFAULT_FLOOR_STEP: int = 1
DEFAULT_FLOOR_CAP: int = 90

# Seed value for a brand-new baseline's diff floor when none is given.
DEFAULT_DIFF_FLOOR: int = 80


class CoverageParseError(Exception):
    """Raised when ``coverage.xml`` is missing, malformed, or unreadable."""


class BaselineConsistencyError(ValueError):
    """Raised when a baseline's percentage does not follow from its line-rate."""


@dataclasses.dataclass
class Baseline:
    """The committed coverage high-water mark and diff floor.

    Attributes:
        total_coverage_percent: Highest total line coverage observed on
            ``main`` so far, as a percentage (0-100).
        diff_coverage_floor_percent: Minimum diff coverage every PR's
            changed lines must hit, as an integer percentage (0-100).
        updated_at: ISO-8601 UTC timestamp of the last write, for audit.
        line_rate: The raw Cobertura root ``line-rate`` (a 0-1 fraction)
            that ``total_coverage_percent`` was rounded from. Storing the
            unrounded input is what makes the committed percentage
            re-derivable offline; see :func:`verify_baseline_consistency`.
        head_sha: Commit on ``main`` the measurement was taken against.
        run_id: GitHub Actions run id whose ``coverage-report`` artifact
            supplied the measurement, as an opaque string.

    The three provenance fields are optional so a baseline written before
    they existed still loads. They are absent, never null, when unknown.
    """

    total_coverage_percent: float
    diff_coverage_floor_percent: int
    updated_at: str | None = None
    line_rate: float | None = None
    head_sha: str | None = None
    run_id: str | None = None


@dataclasses.dataclass
class Decision:
    """Outcome of comparing measured coverage to the baseline."""

    baseline_pct: float
    measured_pct: float
    dropped: bool
    should_bump: bool
    new_total_pct: float
    exit_code: int


@dataclasses.dataclass(frozen=True)
class GuardVerdict:
    """Whether the open ratchet PR may be updated, and the reason either way."""

    proceed: bool
    notice: str


def parse_line_rate(coverage_xml: Path) -> float:
    """Read the raw root ``line-rate`` fraction from a Cobertura report.

    This is the unrounded number the report itself states, kept separate
    from the percentage so the baseline can store the input alongside the
    rounded output and stay checkable without the report.

    Args:
        coverage_xml: Path to the ``coverage.xml`` file.

    Returns:
        The root ``line-rate`` as a fraction in the range [0, 1].

    Raises:
        CoverageParseError: If the file is missing, not valid XML, lacks a
            ``line-rate`` attribute, or that attribute is not a finite
            number in ``[0, 1]``.
    """
    if not coverage_xml.exists():
        raise CoverageParseError(f"coverage report not found: {coverage_xml}")

    try:
        tree = ET.parse(coverage_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise CoverageParseError(f"coverage report is not valid XML: {coverage_xml}: {exc}") from exc

    root = tree.getroot()
    if root is None:
        raise CoverageParseError(f"coverage report has no root element: {coverage_xml}")

    raw = root.get("line-rate")
    if raw is None:
        raise CoverageParseError(f"coverage report has no root line-rate attribute: {coverage_xml}")

    try:
        fraction = float(raw)
    except ValueError as exc:
        raise CoverageParseError(f"coverage report line-rate is not numeric: {raw!r}") from exc

    # ``float()`` happily returns nan/inf and any magnitude, none of which
    # is a coverage fraction. Left unchecked they reach the baseline: nan
    # makes every comparison in decide() false (so the ratchet silently
    # does nothing forever), and a value like 2.0 derives to 200%, clears
    # the high-water mark, and is written as the new one - after which
    # every honest measurement reads as a catastrophic drop. Reject here,
    # where it is still a parse error the workflow soft-skips on.
    if not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
        raise CoverageParseError(f"coverage report line-rate is not a fraction in [0, 1]: {raw!r}")

    return fraction


def derive_percent_from_line_rate(line_rate: float) -> float:
    """Convert a Cobertura 0-1 ``line-rate`` into the committed percentage.

    The single place the rounding happens, so the value the ratchet writes
    and the value a verifier re-derives cannot drift apart.

    Args:
        line_rate: Root ``line-rate`` fraction in the range [0, 1].

    Returns:
        Total line coverage as a percentage in the range [0, 100],
        rounded to two decimal places.
    """
    return round(line_rate * 100.0, 2)


def parse_total_coverage(coverage_xml: Path) -> float:
    """Read the total line-coverage percentage from a Cobertura report.

    Args:
        coverage_xml: Path to the ``coverage.xml`` file.

    Returns:
        Total line coverage as a percentage in the range [0, 100].

    Raises:
        CoverageParseError: If the file is missing, not valid XML, lacks a
            ``line-rate`` attribute, or that attribute is not numeric.
    """
    return derive_percent_from_line_rate(parse_line_rate(coverage_xml))


def verify_baseline_consistency(baseline: Baseline) -> None:
    """Check that the stored percentage follows from the stored line-rate.

    The baseline is a generated value, so it should be reproducible from
    what is committed. Re-deriving the percentage from the recorded
    ``line_rate`` catches a hand-edited number, and a half-applied edit
    that moved one field without the other.

    A baseline with no ``line_rate`` predates provenance and has nothing
    to check; that is a warning at the call site, not an error here.

    Args:
        baseline: The baseline to check.

    Raises:
        BaselineConsistencyError: If ``total_coverage_percent`` is not what
            ``line_rate`` rounds to.
    """
    if baseline.line_rate is None:
        return

    derived = derive_percent_from_line_rate(baseline.line_rate)
    # Both sides are round(_, 2) results, so equality is exact; the epsilon
    # only absorbs JSON float round-tripping.
    if abs(derived - baseline.total_coverage_percent) > 1e-9:
        raise BaselineConsistencyError(
            f"baseline is not self-consistent: line_rate {baseline.line_rate!r} re-derives to "
            f"{derived:.2f}%, but total_coverage_percent says "
            f"{baseline.total_coverage_percent:.2f}%. The committed percentage must be "
            f"reproducible from the committed line-rate; re-run the ratchet against a real "
            f"coverage.xml rather than editing either field by hand."
        )


def read_baseline(baseline_path: Path) -> Baseline:
    """Load the committed baseline.

    Args:
        baseline_path: Path to ``.coverage-baseline.json``.

    Returns:
        The parsed :class:`Baseline`.

    Raises:
        FileNotFoundError: If the baseline file does not exist.
        ValueError: If the baseline JSON is malformed or missing keys.
    """
    if not baseline_path.exists():
        raise FileNotFoundError(f"coverage baseline not found: {baseline_path}")

    try:
        data: dict[str, Any] = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"coverage baseline is not valid JSON: {baseline_path}: {exc}") from exc

    try:
        raw_line_rate = data.get("line_rate")
        raw_run_id = data.get("run_id")
        return Baseline(
            total_coverage_percent=float(data["total_coverage_percent"]),
            diff_coverage_floor_percent=int(data["diff_coverage_floor_percent"]),
            updated_at=data.get("updated_at"),
            line_rate=None if raw_line_rate is None else float(raw_line_rate),
            head_sha=data.get("head_sha"),
            # Written as a string, but tolerate a JSON number so a
            # hand-seeded run id still loads instead of failing the read.
            run_id=None if raw_run_id is None else str(raw_run_id),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"coverage baseline is missing or has bad keys: {baseline_path}: {exc}") from exc


def write_baseline(baseline_path: Path, baseline: Baseline) -> None:
    """Atomically write the baseline to disk.

    Serialises to a temp file in the same directory, then ``os.replace``
    onto the target so a crash mid-write can never leave a partial or
    corrupt baseline (the prior committed value survives intact).

    Args:
        baseline_path: Destination ``.coverage-baseline.json`` path.
        baseline: The :class:`Baseline` to persist.

    Raises:
        TypeError: If a field is not JSON-serialisable (the existing file,
            if any, is left untouched because the temp file is discarded
            before the replace).
    """
    payload: dict[str, Any] = {
        "total_coverage_percent": baseline.total_coverage_percent,
        "diff_coverage_floor_percent": baseline.diff_coverage_floor_percent,
        "updated_at": baseline.updated_at or _utc_now_iso(),
    }
    # Provenance is omitted rather than written as null, so "absent" reads
    # unambiguously as "this measurement predates provenance" instead of
    # "we had a run id and lost it".
    if baseline.line_rate is not None:
        payload["line_rate"] = baseline.line_rate
    if baseline.head_sha is not None:
        payload["head_sha"] = baseline.head_sha
    if baseline.run_id is not None:
        payload["run_id"] = baseline.run_id
    # Serialise first so a TypeError aborts before we touch the filesystem.
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    parent = baseline_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".coverage-baseline.", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, baseline_path)
    finally:
        # If the replace already happened the temp file is gone; otherwise
        # discard the partial temp so no turds are left behind.
        tmp_path.unlink(missing_ok=True)


def decide(baseline_pct: float, measured_pct: float, tolerance: float = DEFAULT_TOLERANCE) -> Decision:
    """Compare measured coverage to the baseline high-water mark.

    Args:
        baseline_pct: Committed high-water coverage percentage.
        measured_pct: Freshly-measured coverage percentage.
        tolerance: Band (in percentage points) treated as "flat" to absorb
            float jitter between runs.

    Returns:
        A :class:`Decision` describing whether coverage dropped, whether the
        baseline should be bumped, and the process exit code to use.
    """
    delta = measured_pct - baseline_pct

    if delta < -tolerance:
        return Decision(
            baseline_pct=baseline_pct,
            measured_pct=measured_pct,
            dropped=True,
            should_bump=False,
            new_total_pct=baseline_pct,
            exit_code=1,
        )

    if delta > tolerance:
        return Decision(
            baseline_pct=baseline_pct,
            measured_pct=measured_pct,
            dropped=False,
            should_bump=True,
            new_total_pct=measured_pct,
            exit_code=0,
        )

    # Within tolerance: flat. Hold the baseline, do not churn it.
    return Decision(
        baseline_pct=baseline_pct,
        measured_pct=measured_pct,
        dropped=False,
        should_bump=False,
        new_total_pct=baseline_pct,
        exit_code=0,
    )


def guard_decision(
    measured_pct: float,
    open_pct: float | None,
    queued_branches: Sequence[str],
    ratchet_branch: str,
    base_pct: float | None = None,
) -> GuardVerdict:
    """Decide whether this fire may open or update the ratchet PR.

    Three refusals, for three unrelated reasons.

    The mechanical one: while the ratchet PR sits in the merge queue
    GitHub locks its head branch, and the push fails with ``GH006``
    whatever the number says. Skipping costs nothing - the ratchet is
    monotonic and idempotent, so once the queued PR merges the next fire
    opens a fresh PR carrying the high-water mark as of then.

    The stale-read one: ``check`` compares the measurement against the
    baseline committed in the tree it checked out, which is the *measured
    commit* - deliberately, so the report and the tree agree - and that
    commit is routinely behind ``main``. A mark already ratcheted onto
    ``main`` since then is therefore re-read as if it were still pending,
    and ``check`` bumps to a value ``main`` already carries. The write is
    correct against what it read and a no-op against the base the PR is
    opened onto, so the PR renders as a provenance-only diff: identical
    ``line_rate`` and ``total_coverage_percent``, moved ``head_sha`` and
    ``run_id``. That is issue #4087, and ``base_pct`` is what closes it.

    The directional one: the open ratchet PR's own mark is above
    ``main``'s for as long as it stays open, so a measurement landing
    between the two clears ``base_pct`` and would still rewrite the open
    PR *downward* if force-pushed.

    Args:
        measured_pct: Freshly-measured total coverage percentage.
        open_pct: Percentage carried by the open ratchet PR, or ``None``
            when the ratchet branch does not exist (no PR is open).
        queued_branches: Head branches of the pull requests currently in
            the merge queue.
        ratchet_branch: The ratchet's own stable head branch.
        base_pct: Percentage committed on the branch the PR is opened
            onto (``main``). ``None`` skips the stale-read refusal, which
            the CLI never does - it requires the file and fails loudly
            rather than reaching this with ``None``.

    Returns:
        A :class:`GuardVerdict` carrying the decision and the single log
        line explaining it.
    """
    # Ordered deliberately: more than one refusal can hold at once, and
    # the branch lock is the one that makes the push impossible rather
    # than merely unwanted. Reporting it first keeps the message
    # deterministic instead of an artifact of evaluation order.
    if ratchet_branch in queued_branches:
        return GuardVerdict(
            proceed=False,
            notice=(
                f"the open ratchet PR is in the merge queue, which locks {ratchet_branch} "
                f"against pushes; leaving it alone."
            ),
        )

    # Before the open PR is considered at all: is there anything left to
    # raise? This one is checked even when no ratchet branch exists,
    # because a provenance-only diff opens a *fresh* PR just as readily as
    # it rewrites an existing one.
    if base_pct is not None and measured_pct <= base_pct:
        return GuardVerdict(
            proceed=False,
            notice=(
                f"measured {measured_pct:g}% is not above the {base_pct:g}% already committed on "
                f"the base branch, so the bump is a no-op there and the PR would carry only "
                f"provenance; leaving it alone."
            ),
        )

    if open_pct is None:
        return GuardVerdict(
            proceed=True,
            notice=f"no {ratchet_branch} baseline (branch absent); opening a fresh ratchet PR.",
        )

    if measured_pct > open_pct:
        return GuardVerdict(
            proceed=True,
            notice=f"measured {measured_pct:g}% > open ratchet PR {open_pct:g}%; updating it.",
        )

    return GuardVerdict(
        proceed=False,
        notice=f"measured {measured_pct:g}% is not above the open ratchet PR's {open_pct:g}%; leaving it alone.",
    )


def next_floor(current: int, step: int = DEFAULT_FLOOR_STEP, cap: int = DEFAULT_FLOOR_CAP) -> int:
    """Compute the next diff-coverage floor for the weekly bump.

    Raises the floor by ``step`` percentage points without exceeding
    ``cap``. A floor already at or above the cap is clamped down to the
    cap (never raised), so a manually-edited over-cap value self-heals.

    Args:
        current: Current diff-coverage floor percentage.
        step: Increment in percentage points (must be positive).
        cap: Hard ceiling the floor may never exceed.

    Returns:
        The next floor, an integer in the range [current_or_lower, cap].

    Raises:
        ValueError: If ``step`` is not a positive integer.
    """
    if step <= 0:
        raise ValueError(f"weekly floor step must be positive, got {step}")
    if current >= cap:
        return cap
    return min(current + step, cap)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cmd_check(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    try:
        baseline = read_baseline(baseline_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    # Before trusting the committed mark, confirm it is the number its own
    # line-rate produces. A baseline that cannot be re-derived is a broken
    # input, not a coverage verdict, so it exits 2 and writes nothing.
    try:
        verify_baseline_consistency(baseline)
    except BaselineConsistencyError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    if baseline.line_rate is None:
        print(
            "::warning::baseline carries no line_rate, so its percentage cannot be "
            "re-derived from the committed file; the next ratchet click records one."
        )

    try:
        measured_line_rate = parse_line_rate(Path(args.coverage_xml))
    except CoverageParseError as exc:
        # A missing/broken report is NOT a coverage drop. Soft-skip (exit 3)
        # so the advisory workflow can decide to ignore rather than fail red.
        print(f"::warning::{exc}; skipping coverage ratchet for this run")
        return 3

    measured = derive_percent_from_line_rate(measured_line_rate)
    decision = decide(baseline.total_coverage_percent, measured, tolerance=args.tolerance)

    print(f"baseline total coverage : {baseline.total_coverage_percent:.2f}%")
    print(f"measured total coverage : {measured:.2f}%")
    print(f"delta                   : {measured - baseline.total_coverage_percent:+.2f} pp")

    if decision.dropped:
        print(
            f"::warning::coverage dropped from {baseline.total_coverage_percent:.2f}% to "
            f"{measured:.2f}% (tolerance {args.tolerance} pp). Add tests for the new/changed "
            f"lines, or see docs/operations/coverage-ratchet.md for the coverage-neutral override."
        )
        _emit_github_output(coverage_dropped="true", baseline_bumped="false", measured=measured)
        return decision.exit_code

    if decision.should_bump and not args.no_bump:
        if not args.head_sha:
            print(
                "::warning::bumping without --head-sha; the new baseline will not name the commit it was measured on."
            )
        bumped = dataclasses.replace(
            baseline,
            total_coverage_percent=decision.new_total_pct,
            updated_at=_utc_now_iso(),
            # Provenance always travels with the measurement it describes,
            # so the file never states a percentage from one run next to a
            # line-rate or commit from another.
            line_rate=measured_line_rate,
            head_sha=args.head_sha or None,
            run_id=args.run_id or None,
        )
        write_baseline(baseline_path, bumped)
        print(f"ratchet click: baseline bumped to {decision.new_total_pct:.2f}%")
        _emit_github_output(coverage_dropped="false", baseline_bumped="true", measured=measured)
        return 0

    print("coverage held at baseline; no bump.")
    _emit_github_output(coverage_dropped="false", baseline_bumped="false", measured=measured)
    return 0


def _cmd_bump_floor(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    try:
        baseline = read_baseline(baseline_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    current = baseline.diff_coverage_floor_percent
    new_floor = next_floor(current, step=args.step, cap=args.cap)
    if new_floor == current:
        print(f"diff-coverage floor already at cap {args.cap}%; no bump.")
        _emit_github_output(floor_changed="false", new_floor=new_floor)
        return 0

    bumped = dataclasses.replace(
        baseline,
        diff_coverage_floor_percent=new_floor,
        updated_at=_utc_now_iso(),
    )
    write_baseline(baseline_path, bumped)
    print(f"diff-coverage floor bumped: {current}% -> {new_floor}% (cap {args.cap}%)")
    _emit_github_output(floor_changed="true", new_floor=new_floor, old_floor=current)
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        line_rate = parse_line_rate(Path(args.coverage_xml))
    except CoverageParseError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 3

    measured = derive_percent_from_line_rate(line_rate)
    baseline_path = Path(args.baseline)
    baseline = Baseline(
        total_coverage_percent=measured,
        diff_coverage_floor_percent=args.diff_floor,
        updated_at=_utc_now_iso(),
        line_rate=line_rate,
        head_sha=args.head_sha or None,
        run_id=args.run_id or None,
    )
    write_baseline(baseline_path, baseline)
    print(f"seeded baseline at {baseline_path}: total={measured:.2f}%, diff_floor={args.diff_floor}%")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Re-derive the committed percentage from the committed file alone.

    Needs no ``coverage.xml`` and no network: everything it checks is in
    ``.coverage-baseline.json``. That is the point - the high-water mark
    should be reproducible by anyone holding the repo.
    """
    baseline_path = Path(args.baseline)
    try:
        baseline = read_baseline(baseline_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    if baseline.line_rate is None:
        message = f"{baseline_path} has no line_rate, so its percentage cannot be re-derived"
        if args.require_provenance:
            print(f"::error::{message}", file=sys.stderr)
            return 2
        print(f"::warning::{message}")
        return 0

    try:
        verify_baseline_consistency(baseline)
    except BaselineConsistencyError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    if args.require_provenance and not baseline.head_sha:
        print(
            f"::error::{baseline_path} does not record the head_sha it was measured on",
            file=sys.stderr,
        )
        return 2

    print(f"line-rate              : {baseline.line_rate!r}")
    print(f"re-derived coverage    : {derive_percent_from_line_rate(baseline.line_rate):.2f}%")
    print(f"committed coverage     : {baseline.total_coverage_percent:.2f}%")
    print(f"measured at head_sha   : {baseline.head_sha or '(not recorded)'}")
    print(f"from CI run            : {baseline.run_id or '(not recorded)'}")
    print("baseline is self-consistent.")
    return 0


def _cmd_guard(args: argparse.Namespace) -> int:
    """Gate the ratchet-PR step on the open PR being both lower and pushable.

    The workflow does the two API reads (it already holds the token) and
    hands this command their results as files; the decision itself lives
    here so it can be driven by tests rather than only by a live queue.
    """
    open_baseline = Path(args.open_baseline)
    open_pct: float | None = None
    if open_baseline.is_file():
        # Present only when the ratchet branch exists and carries a baseline.
        try:
            open_pct = float(json.loads(open_baseline.read_text(encoding="utf-8"))["total_coverage_percent"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"::error::open ratchet baseline is malformed: {exc!r}", file=sys.stderr)
            return 1

    # Unlike the ratchet branch, the base branch always carries a baseline:
    # it is committed in the repo. An absent or malformed file here is a
    # broken read, not a state worth guessing at - and guessing would
    # silently retire the stale-read refusal while the log stayed green.
    try:
        base_pct = float(json.loads(Path(args.base_baseline).read_text(encoding="utf-8"))["total_coverage_percent"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            f"::error::could not read the base branch's baseline: {exc!r}; refusing to guess.",
            file=sys.stderr,
        )
        return 1

    # An unreadable queue must never collapse to "empty". Empty means "the
    # branch is pushable", and reaching that from a failed read is exactly
    # how the push starts hard-failing on GH006 again while the log claims
    # everything is fine.
    try:
        queued = json.loads(Path(args.queued_branches).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"::error::could not read the merge-queue state: {exc!r}; refusing to guess.", file=sys.stderr)
        return 1
    if not isinstance(queued, list) or any(not isinstance(item, str) for item in queued):
        print("::error::merge-queue state is not a list of branch names; refusing to guess.", file=sys.stderr)
        return 1

    verdict = guard_decision(
        measured_pct=float(args.measured),
        open_pct=open_pct,
        queued_branches=queued,
        ratchet_branch=args.ratchet_branch,
        base_pct=base_pct,
    )
    print(f"::notice::{verdict.notice}")
    _emit_github_output(proceed="true" if verdict.proceed else "false")
    return 0


def _cmd_show_floor(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    try:
        baseline = read_baseline(baseline_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    # Bare integer on stdout so a CI step can capture it directly.
    print(baseline.diff_coverage_floor_percent)
    return 0


def _emit_github_output(**pairs: object) -> None:
    """Append key=value pairs to ``$GITHUB_OUTPUT`` when running in CI."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    try:
        with open(out, "a", encoding="utf-8") as handle:
            for key, value in pairs.items():
                handle.write(f"{key}={value}\n")
    except OSError:
        # Never let an observability write break the gate.
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Total-coverage monotonic ratchet")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="compare coverage.xml total to baseline; bump on a rise")
    p_check.add_argument("--coverage-xml", default="coverage.xml", help="path to Cobertura coverage.xml")
    p_check.add_argument("--baseline", default=".coverage-baseline.json", help="path to baseline JSON")
    p_check.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE, help="flat-band in pp")
    p_check.add_argument("--no-bump", action="store_true", help="report only; never rewrite the baseline")
    p_check.add_argument("--head-sha", default="", help="commit the coverage report was measured on")
    p_check.add_argument("--run-id", default="", help="CI run id the coverage artifact came from")
    p_check.set_defaults(func=_cmd_check)

    p_bump = sub.add_parser("bump-floor", help="raise the diff-coverage floor by one step (weekly)")
    p_bump.add_argument("--baseline", default=".coverage-baseline.json", help="path to baseline JSON")
    p_bump.add_argument("--step", type=int, default=DEFAULT_FLOOR_STEP, help="increment in pp")
    p_bump.add_argument("--cap", type=int, default=DEFAULT_FLOOR_CAP, help="hard ceiling for the floor")
    p_bump.set_defaults(func=_cmd_bump_floor)

    p_init = sub.add_parser("init", help="seed the baseline from a measured coverage.xml")
    p_init.add_argument("--coverage-xml", default="coverage.xml", help="path to Cobertura coverage.xml")
    p_init.add_argument("--baseline", default=".coverage-baseline.json", help="path to baseline JSON")
    p_init.add_argument("--diff-floor", type=int, default=DEFAULT_DIFF_FLOOR, help="initial diff floor pp")
    p_init.add_argument("--head-sha", default="", help="commit the coverage report was measured on")
    p_init.add_argument("--run-id", default="", help="CI run id the coverage artifact came from")
    p_init.set_defaults(func=_cmd_init)

    p_verify = sub.add_parser("verify", help="re-derive the committed percentage from the baseline alone")
    p_verify.add_argument("--baseline", default=".coverage-baseline.json", help="path to baseline JSON")
    p_verify.add_argument(
        "--require-provenance",
        action="store_true",
        help="also fail when the baseline records no line_rate/head_sha",
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_guard = sub.add_parser("guard", help="decide whether the open ratchet PR may be updated")
    p_guard.add_argument("--measured", required=True, help="freshly-measured total coverage percentage")
    p_guard.add_argument(
        "--open-baseline",
        required=True,
        help="baseline read off the ratchet branch; an absent file means the branch does not exist",
    )
    p_guard.add_argument(
        "--base-baseline",
        required=True,
        help=(
            "baseline read off the branch the PR is opened onto; required, because a bump "
            "measured against a stale checked-out tree is a no-op against this one"
        ),
    )
    p_guard.add_argument(
        "--queued-branches",
        required=True,
        help="JSON array of the head branches currently in the merge queue",
    )
    p_guard.add_argument("--ratchet-branch", required=True, help="the ratchet's own stable head branch")
    p_guard.set_defaults(func=_cmd_guard)

    p_show = sub.add_parser("show-floor", help="print the current diff-coverage floor")
    p_show.add_argument("--baseline", default=".coverage-baseline.json", help="path to baseline JSON")
    p_show.set_defaults(func=_cmd_show_floor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = args.func
    result = func(args)
    return int(result)


if __name__ == "__main__":
    sys.exit(main())
