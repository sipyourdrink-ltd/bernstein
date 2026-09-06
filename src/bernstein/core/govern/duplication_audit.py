"""How many capabilities currently have more than one implementation (issue #5105).

A guard test answers "did THIS PR introduce a second implementation". It cannot
answer "how many are there now, and did that number move since last week" --
that needs something which runs standalone and reports state rather than gating
a diff. This is that collector.

Three properties it has to have, and each one is a reaction to how the last
attempt failed.

**Findings, not a pass/fail bit.** Each count above its expected value becomes a
named finding with a stable id and the offending paths, so it survives being
read out of context: a number in a CI log means nothing a week later, and a bare
red means nothing at all.

**"Not yet measurable" is its own verdict.** A check whose subject does not
exist yet is not passing. Reporting it as a pass is the failure mode that makes
an aggregate report worse than no report, because the total looks like coverage
it does not have.

**No score and no grade.** ``core/security/security_posture.py`` computed a
letter from weighted metrics and has zero callers -- the proof that this
codebase has built this report once already and never wired it. Its A-F model is
explicitly rejected here: a count against an expected count is auditable, and a
letter is a number nobody can argue with because nobody can reconstruct it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class Verdict(StrEnum):
    """What the collector was able to say about one check."""

    #: Counted, and the count is what it should be.
    MEASURED_PASSED = "measured_passed"
    #: Counted, and there is more than one implementation.
    MEASURED_FAILED = "measured_failed"
    #: The subject does not exist yet, so nothing was counted. NOT a pass.
    NOT_YET_MEASURABLE = "not_yet_measurable"


@dataclass(frozen=True, slots=True)
class DuplicationFinding:
    """One capability's implementation count, and what that means.

    Attributes:
        check_id: Stable identifier. The thing a tracker keys on, so a finding
            read next week is the same finding.
        summary: One line an operator can act on.
        verdict: :class:`Verdict`.
        count: What was counted, or ``None`` when nothing was.
        expected: What it should be. ``1`` for a capability that should have one
            implementation, ``0`` for a thing that should not exist at all.
        paths: The offending locations, sorted. Empty when the check passed or
            could not run -- a finding names WHERE, or the reader is left doing
            the search the collector just did.
    """

    check_id: str
    summary: str
    verdict: Verdict
    expected: int
    count: int | None = None
    paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "check_id": self.check_id,
            "summary": self.summary,
            "verdict": self.verdict.value,
            "expected": self.expected,
            "count": self.count,
            "paths": list(self.paths),
        }


@dataclass(frozen=True, slots=True)
class DuplicationReport:
    """Every check, in a fixed order.

    Attributes:
        findings: One per check, ordered by ``check_id`` so two runs over one
            tree serialize identically.
    """

    findings: tuple[DuplicationFinding, ...]

    @property
    def failed(self) -> tuple[DuplicationFinding, ...]:
        """The checks that counted more than they should have."""
        return tuple(f for f in self.findings if Verdict.MEASURED_FAILED is f.verdict)

    @property
    def not_yet_measurable(self) -> tuple[DuplicationFinding, ...]:
        """The checks whose subject does not exist yet.

        Reported separately from failures, and never folded into a total: a
        denominator that silently includes what was not measured is the thing
        that makes an aggregate look like coverage it does not have.
        """
        return tuple(f for f in self.findings if Verdict.NOT_YET_MEASURABLE is f.verdict)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization.

        Deliberately carries no score, no grade and no percentage -- see the
        module docstring.
        """
        return {
            "measured": len(self.findings) - len(self.not_yet_measurable),
            "failed": len(self.failed),
            "not_yet_measurable": len(self.not_yet_measurable),
            "findings": [f.to_dict() for f in self.findings],
        }


def _source_files(root: Path) -> Iterator[tuple[str, ast.Module]]:
    """Every parseable module under *root*, as ``(relative path, tree)``.

    Sorted, because the paths land in a finding and a finding has to be
    byte-identical across runs. A file that does not parse is skipped rather
    than raised: this is a report about the tree, and one unparseable file must
    not take it down.
    """
    for path in sorted(root.rglob("*.py")):
        try:
            yield str(path.relative_to(root)), ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue


def _private_hmac_loaders(root: Path) -> tuple[str, ...]:
    """Every ``def _load_hmac_key`` -- one canonical loader, or twenty (#5095)."""
    found: list[str] = []
    for rel, tree in _source_files(root):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_load_hmac_key":
                found.append(f"{rel}:{node.lineno}")
    return tuple(sorted(found))


def _inline_canonical_bytes_sites(root: Path) -> tuple[str, ...]:
    """Every place canonical bytes are built INLINE rather than through one helper (#5094).

    Counted as call SITES, not as distinct function bodies, and named for what it
    actually measures. The issue's headline figure -- "91 copies with 60 distinct
    bodies" -- is about canonical-JSON *encoders*; a walk of this tree cannot
    tell an encoder from a large function that happens to canonicalize as one
    step, so claiming that figure here would be reporting a number this check
    did not measure. What it does measure is exact and is the same duplication
    from the other side: how many places rebuild the bytes instead of calling
    the one thing that owns them.

    A site qualifies when it calls ``json.dumps`` with BOTH ``sort_keys=True``
    and compact ``separators``. Both, deliberately: sorted keys alone is how a
    great deal of ordinary logging and diffing serializes, and counting those
    reports a number several times larger than the duplication it is supposed to
    measure. Compact separators are what make the output BYTES rather than text,
    which is the property a signature depends on.
    """
    found: list[str] = []
    for rel, tree in _source_files(root):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_canonical_dumps(node):
                found.append(f"{rel}:{node.lineno}")
    return tuple(sorted(found))


def _is_canonical_dumps(call: ast.Call) -> bool:
    """Is this a ``json.dumps`` with sorted keys AND compact separators?"""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "dumps"):
        return False
    sorted_keys = False
    compact = False
    for keyword in call.keywords:
        if keyword.arg == "sort_keys":
            sorted_keys = isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        elif keyword.arg == "separators":
            compact = _is_compact_separators(keyword.value)
    return sorted_keys and compact


def _is_compact_separators(value: ast.expr) -> bool:
    """Is this a ``(",", ":")`` literal -- the no-whitespace form?"""
    if not isinstance(value, ast.Tuple) or len(value.elts) != 2:
        return False
    parts = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return parts == [",", ":"]


def _measured(check_id: str, summary: str, paths: tuple[str, ...], expected: int) -> DuplicationFinding:
    """Build a finding from a count, choosing the verdict from the count itself."""
    over = len(paths) > expected
    return DuplicationFinding(
        check_id=check_id,
        summary=summary,
        verdict=Verdict.MEASURED_FAILED if over else Verdict.MEASURED_PASSED,
        expected=expected,
        count=len(paths),
        # Only on a failure. A passing check listing its one legitimate
        # implementation is noise that makes the failures harder to see.
        paths=paths if over else (),
    )


def _not_yet(check_id: str, summary: str, expected: int) -> DuplicationFinding:
    """A check whose subject does not exist yet. Not a pass."""
    return DuplicationFinding(
        check_id=check_id,
        summary=summary,
        verdict=Verdict.NOT_YET_MEASURABLE,
        expected=expected,
        count=None,
    )


def collect_duplication(source_root: Path) -> DuplicationReport:
    """Count the capabilities that have more than one implementation.

    Args:
        source_root: The package root to walk, e.g. ``src/bernstein``.

    Returns:
        One finding per check, ordered by ``check_id``. Two runs over an
        unchanged tree produce byte-identical output.
    """
    findings = [
        _measured(
            "inline-canonical-bytes-sites",
            "places that build canonical signing bytes inline instead of through one shared helper; "
            "bytes rebuilt at each site are bytes that can disagree, and a signature made under one "
            "does not verify under another (#5094)",
            _inline_canonical_bytes_sites(source_root),
            expected=1,
        ),
        _measured(
            "private-hmac-key-loaders",
            "private `_load_hmac_key` wrappers; the canonical loader is core.security.audit",
            _private_hmac_loaders(source_root),
            expected=0,
        ),
        _not_yet(
            "receipt-verify-kinds",
            "receipt kinds registered through one verify pair (#5096: seven independent verifiers)",
            expected=1,
        ),
        _not_yet(
            "verification-result-shapes",
            "verification-result classes declaring one field set under one name (#5099)",
            expected=1,
        ),
        _not_yet(
            "registry-duplicate-ids",
            "ids registered twice across the registries (#5104)",
            expected=0,
        ),
        _not_yet(
            "orphan-modules",
            "modules under core/ that no non-test importer reaches (#5100)",
            expected=0,
        ),
    ]
    return DuplicationReport(findings=tuple(sorted(findings, key=lambda f: f.check_id)))


__all__ = [
    "DuplicationFinding",
    "DuplicationReport",
    "Verdict",
    "collect_duplication",
]
