"""Draft stage: extract structured requirements from a spec document.

The draft stage is the *only* stage in the pipeline that may invoke a model,
and it invokes it exactly once (issue #2361, AC5). A :class:`Drafter` is any
callable that maps the raw spec text to an ordered list of acceptance-line
strings; :func:`draft_requirements` calls it a single time and hashes the
result into a :class:`RequirementSet`.

Two drafters ship here:

* :class:`StructuralDrafter` -- a deterministic, zero-model extractor that
  lifts EARS-shaped acceptance lines (markdown checkboxes / bullets that read
  as ``... shall ...``) straight out of the document. It is the default so the
  pipeline runs offline and byte-reproducibly.
* :class:`InstrumentedDrafter` -- a counting wrapper used by tests (and callers
  that want to assert the one-call budget) around any other drafter.

A model-backed drafter is plugged in by the caller; because only the draft
stage ever touches a drafter, and only once, the whole pipeline stays within
the single-model-call budget regardless of which drafter is supplied.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from bernstein.sdd.spec_pipeline.requirements import (
    RequirementSet,
    build_requirement_set,
    canonical_text,
    is_ears,
)

__all__ = [
    "Drafter",
    "InstrumentedDrafter",
    "StructuralDrafter",
    "draft_requirements",
]


@runtime_checkable
class Drafter(Protocol):
    """A callable that extracts ordered acceptance lines from spec text."""

    def __call__(self, spec_text: str) -> list[str]: ...


# Leading markdown list / checkbox / numbering decoration to strip.
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s+)?(?:\d+[.)]\s+)?")


def _strip_decoration(line: str) -> str:
    """Remove leading bullet / checkbox / numbering decoration from *line*."""
    return _BULLET_RE.sub("", line).strip()


class StructuralDrafter:
    """Deterministic, zero-model extractor of EARS-shaped acceptance lines.

    A line is taken when, after stripping markdown decoration, it reads as an
    EARS clause (contains a ``shall`` modal). Headings, prose, and blank lines
    are ignored. The extractor makes no network or model call, so two runs
    over identical text yield byte-identical output.
    """

    def __call__(self, spec_text: str) -> list[str]:
        lines: list[str] = []
        for raw in spec_text.splitlines():
            candidate = _strip_decoration(raw)
            if not candidate or candidate.startswith("#"):
                continue
            if is_ears(candidate):
                lines.append(canonical_text(candidate))
        return lines


class InstrumentedDrafter:
    """Wrap a drafter and count invocations (one-call-budget assertions).

    Attributes:
        calls: Number of times the wrapped drafter has been invoked.
    """

    def __init__(self, inner: Drafter) -> None:
        self._inner = inner
        self.calls = 0

    def __call__(self, spec_text: str) -> list[str]:
        self.calls += 1
        return self._inner(spec_text)


def draft_requirements(spec_text: str, drafter: Drafter) -> RequirementSet:
    """Run the draft stage: one drafter call, then hash into a requirement set.

    Args:
        spec_text: The raw spec document.
        drafter: The extraction callable. Invoked exactly once.

    Returns:
        The content-addressed :class:`RequirementSet`.
    """
    lines = drafter(spec_text)
    return build_requirement_set(lines, source_text=spec_text)
