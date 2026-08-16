#!/usr/bin/env python
"""Per-module mutation tester for a fixed critical-path module list.

The repo-wide mutmut config doesn't compose cleanly with our heavyweight
``tests/conftest.py`` (it imports the whole orchestrator); the existing
``scripts/mutmut_lineage.py`` runner side-steps that by applying a small
set of mutation operators directly to a target file and re-running a
focused test suite. This script generalises that harness to a fixed set
of critical-path modules so CI can gate mutation score per-module.

Targets and per-module thresholds live in :data:`MODULES` below. Each
entry pairs one source file with the unit-test path(s) that should kill
mutations in that file. A module is "passing" when

    kill_rate >= threshold

where ``kill_rate = killed / total`` and ``total = killed + survivors``
(timeouts count as kills - an infinite loop is a meaningful signal).

CLI:

    python scripts/mutmut_critical.py                # all modules
    python scripts/mutmut_critical.py --only NAME    # one module
    python scripts/mutmut_critical.py --list         # list module keys
    python scripts/mutmut_critical.py --json PATH    # write summary JSON

Exit codes:

    0 - every module met its threshold
    1 - at least one module below threshold (gate failed)
    2 - baseline tests fail (cannot trust mutation results)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Module:
    """One critical-path module under mutation gate."""

    key: str
    source: str  # repo-relative
    tests: tuple[str, ...]  # repo-relative test paths
    threshold: float  # required kill rate (0..1)
    budget_seconds: int  # wall-clock budget for this module
    max_candidates: int = 80  # cap per-file candidates to bound wall-clock
    note: str = ""  # short rationale / scope marker


# Fixed critical-path modules. Paths verified against the worktree layout
# (see CLAUDE.md "Module map"). Thresholds start at 0.70 per TC-D brief -
# this is the *gate setup* PR, scores get raised once humans backfill
# tests for any module that falls short.
MODULES: tuple[Module, ...] = (
    Module(
        key="claim_next",
        source="src/bernstein/core/tasks/claim.py",
        tests=("tests/unit/test_claim_next.py",),
        threshold=0.70,
        budget_seconds=1200,
        max_candidates=80,
        note="Atomic file-backed claim primitive (issue #1292).",
    ),
    Module(
        key="audit_log",
        source="src/bernstein/core/security/audit.py",
        tests=(
            "tests/unit/test_audit_chain_byteflip_regression.py",
            "tests/unit/test_audit_key.py",
            "tests/unit/test_audit_log_mutation_kill.py",
            "tests/unit/test_audit_log_chain_verifier_invariants.py",
        ),
        threshold=0.70,
        budget_seconds=1200,
        max_candidates=80,
        note=("HMAC-chained audit log writer."),
    ),
    Module(
        key="audit_integrity",
        source="src/bernstein/core/security/audit_integrity.py",
        tests=(
            "tests/unit/test_audit_integrity.py",
            "tests/unit/test_audit_integrity_mutation_kill.py",
        ),
        threshold=0.70,
        budget_seconds=900,
        max_candidates=60,
        note="Audit log integrity verifier on startup.",
    ),
    Module(
        key="lineage_gate",
        source="src/bernstein/core/lineage/gate.py",
        tests=("tests/unit/lineage/",),
        threshold=0.75,
        budget_seconds=900,
        max_candidates=60,
        note="Lineage v1 admission gate.",
    ),
    Module(
        key="lineage_tips",
        source="src/bernstein/core/lineage/tips.py",
        tests=("tests/unit/lineage/",),
        threshold=0.75,
        budget_seconds=600,
        max_candidates=60,
        note="Lineage v1 tip tracker.",
    ),
    Module(
        key="lineage_merge",
        source="src/bernstein/core/lineage/merge.py",
        tests=("tests/unit/lineage/",),
        threshold=0.75,
        budget_seconds=600,
        max_candidates=60,
        note="Lineage v1 merge resolution.",
    ),
    Module(
        key="config_seed_parser",
        source="src/bernstein/core/config/seed_parser.py",
        tests=(
            "tests/unit/test_config_schema.py",
            "tests/unit/test_seed_parser_mutation_kill.py",
        ),
        threshold=0.70,
        budget_seconds=1200,
        max_candidates=80,
        note="bernstein.yaml seed parser + ${ENV} reference resolution.",
    ),
)

# (search, replace) pairs applied one-at-a-time per line. Mirrors the
# small high-signal set used by scripts/mutmut_lineage.py.
MUTATIONS: tuple[tuple[str, str], ...] = (
    (" < ", " <= "),
    (" <= ", " < "),
    (" > ", " >= "),
    (" >= ", " > "),
    (" == ", " != "),
    (" != ", " == "),
    (" and ", " or "),
    (" or ", " and "),
    ("True", "False"),
    ("False", "True"),
    ("not ", ""),
    (" 0", " 1"),
    (" 1", " 2"),
    (" 2", " 1"),
    ("[]", "[None]"),
    ("return True", "return False"),
    ("return False", "return True"),
    ("len(", "0 * len("),
)

#: Subset of :data:`MUTATIONS` that flips an English boolean word rather than
#: a code operator. Excluded on pure f-string-literal lines - see
#: :func:`_is_fstring_literal_continuation`.
_PROSE_WORD_MUTATIONS: frozenset[tuple[str, str]] = frozenset({(" and ", " or "), (" or ", " and "), ("not ", "")})


def _is_fstring_literal_continuation(line: str) -> bool:
    """Whether *line* is entirely (part of) an f-string literal, no code.

    A physical source line whose stripped text begins with ``f"`` or ``f'``
    is, by Python's grammar, one piece of a string-literal token - either a
    continuation line of a multi-line implicit concatenation (the style this
    codebase uses for long f-string messages, one literal segment per line
    inside the enclosing parens) or a lone literal. It cannot carry any other
    expression or statement on the same line.

    :data:`_PROSE_WORD_MUTATIONS` on such a line therefore only reword the
    rendered text of a log line or exception message - never program logic -
    so a mutation gate measuring verifier *behaviour* gains nothing by
    proposing them, and every caller downstream would otherwise need to pin
    exact message wording to kill what is really a copy-edit, not a defect.
    Candidates on every other line (including the ``{expr}`` portions
    embedded in an f-string, which sit on their own line only when the
    expression itself is multi-line - rare in this codebase) are unaffected.
    """
    stripped = line.lstrip()
    return stripped.startswith(('f"', "f'"))


@dataclass
class ModuleResult:
    key: str
    total: int = 0
    killed: int = 0
    timeouts: int = 0
    survivors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    threshold: float = 0.0
    baseline_ok: bool = True
    timed_out: bool = False  # wall-clock budget exceeded mid-run

    @property
    def kill_rate(self) -> float:
        return (self.killed / self.total) if self.total else 0.0

    @property
    def passed(self) -> bool:
        return self.baseline_ok and self.kill_rate >= self.threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "module": self.key,
            "total": self.total,
            "killed": self.killed,
            "timeouts": self.timeouts,
            "survivors": self.survivors,
            "kill_rate": round(self.kill_rate, 4),
            "threshold": self.threshold,
            "passed": self.passed,
            "baseline_ok": self.baseline_ok,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "budget_exceeded": self.timed_out,
        }


def _run_tests(test_paths: tuple[str, ...]) -> bool:
    """Return True iff tests pass (i.e. mutation NOT killed)."""
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-x",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            *test_paths,
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=180,
    )
    return res.returncode == 0


def _candidates(target: Path, limit: int) -> list[tuple[int, str, str, str]]:
    out: list[tuple[int, str, str, str]] = []
    text = target.read_text().splitlines(keepends=True)
    in_docstring = False
    for i, line in enumerate(text):
        stripped = line.lstrip()
        triple_count = line.count('"""') + line.count("'''")
        if triple_count and triple_count % 2 == 1:
            in_docstring = not in_docstring
            continue
        if in_docstring or stripped.startswith("#") or stripped.startswith("@"):
            continue
        if line.rstrip().endswith(",") and "=" in line and "(" not in line:
            continue
        is_fstring_line = _is_fstring_literal_continuation(line)
        for search, replace in MUTATIONS:
            if search in line and search != replace:
                if is_fstring_line and (search, replace) in _PROSE_WORD_MUTATIONS:
                    continue
                out.append((i, line, search, replace))
        if len(out) >= limit:
            break
    return out[:limit]


def _mutate_module(mod: Module, *, verbose: bool = True) -> ModuleResult:
    res = ModuleResult(key=mod.key, threshold=mod.threshold)
    target = REPO / mod.source
    if not target.exists():
        print(f"[{mod.key}] missing source {target}; skipping", file=sys.stderr)
        res.baseline_ok = False
        return res

    if verbose:
        print(f"[{mod.key}] baseline tests: {mod.tests}", flush=True)
    if not _run_tests(mod.tests):
        print(f"[{mod.key}] baseline fails - cannot trust mutation run", file=sys.stderr)
        res.baseline_ok = False
        return res

    original = target.read_text()
    candidates = _candidates(target, mod.max_candidates)
    if verbose:
        print(f"[{mod.key}] {len(candidates)} candidate(s); budget {mod.budget_seconds}s", flush=True)

    deadline = time.monotonic() + mod.budget_seconds
    try:
        for idx, (line_no, line, search, replace) in enumerate(candidates):
            if time.monotonic() >= deadline:
                res.timed_out = True
                print(f"[{mod.key}] wall-clock budget exceeded after {idx} mutations", flush=True)
                break
            res.total += 1
            new_line = line.replace(search, replace, 1)
            lines = original.splitlines(keepends=True)
            lines[line_no] = new_line
            target.write_text("".join(lines))
            try:
                survived = _run_tests(mod.tests)
            except subprocess.TimeoutExpired:
                res.timeouts += 1
                res.killed += 1
                continue
            if survived:
                res.survivors.append(f"{mod.source}:{line_no + 1} '{search}' -> '{replace}': {line.rstrip()}")
                if verbose:
                    print(f"  [{idx + 1}/{len(candidates)}] SURVIVED line {line_no + 1}", flush=True)
            else:
                res.killed += 1
    finally:
        target.write_text(original)

    res.elapsed_seconds = mod.budget_seconds - max(deadline - time.monotonic(), 0.0)
    return res


def _print_summary(results: list[ModuleResult]) -> None:
    print()
    print("=== Mutation gate summary ===")
    print(f"{'module':<20} {'kill rate':>10} {'thr':>6} {'status':>8}  notes")
    for r in results:
        status = "PASS" if r.passed else ("BASE-FAIL" if not r.baseline_ok else "FAIL")
        rate = f"{100 * r.kill_rate:5.1f}%" if r.total else "  n/a"
        thr = f"{100 * r.threshold:4.0f}%"
        suffix = " (budget exceeded)" if r.timed_out else ""
        print(f"{r.key:<20} {rate:>10} {thr:>6} {status:>8}  {r.killed}/{r.total} killed{suffix}")
    for r in results:
        if r.survivors:
            print()
            print(f"Survivors in {r.key}:")
            for s in r.survivors:
                print(f"  - {s}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Run a single module key", default=None)
    parser.add_argument("--list", action="store_true", help="List configured module keys and exit")
    parser.add_argument("--json", help="Write per-module summary JSON to this path", default=None)
    parser.add_argument("--quiet", action="store_true", help="Suppress per-mutation logs")
    args = parser.parse_args(argv)

    if args.list:
        for m in MODULES:
            print(f"{m.key}\t{m.source}\tthr={m.threshold:.2f}\tbudget={m.budget_seconds}s")
        return 0

    selected: tuple[Module, ...]
    if args.only:
        match = [m for m in MODULES if m.key == args.only]
        if not match:
            print(f"unknown module: {args.only!r}", file=sys.stderr)
            return 2
        selected = tuple(match)
    else:
        selected = MODULES

    results: list[ModuleResult] = []
    for mod in selected:
        results.append(_mutate_module(mod, verbose=not args.quiet))

    _print_summary(results)

    if args.json:
        Path(args.json).write_text(json.dumps([r.to_dict() for r in results], indent=2) + "\n")

    any_baseline_fail = any(not r.baseline_ok for r in results)
    if any_baseline_fail:
        return 2
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
