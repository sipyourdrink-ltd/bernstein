"""Two-phase attested task tags: admission predicate + conformance (#2544).

Tags are two-phase attested contracts:

1. **Declared** tags gate the claim deterministically -- a task is admissible
   only when *every* declared tag is under its concurrency limit (AND-of-all).
2. **After completion** the worktree diff and tool-call log are checked against
   the declared contract and the result is sealed into a signed
   tag-conformance receipt (:mod:`bernstein.core.admission.receipts`). A task
   that declared ``docs-only`` and wrote ``src/`` gets a signed violation
   receipt visible to merge gates.

This module owns the pure predicates. The declared-tag *contracts* (what a tag
promises about the worktree) live in :data:`TAG_CONTRACTS`; a tag with no
contract is a free-form concurrency label that gates admission but asserts
nothing about the diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "TAG_CONTRACTS",
    "TagContract",
    "TagViolation",
    "check_conformance",
    "tags_admissible",
]


@dataclass(frozen=True, slots=True)
class TagContract:
    """What a declared tag promises about a task's effect.

    Attributes:
        tag: The tag name.
        allowed_prefixes: Path prefixes the task may touch. A changed path
            outside every allowed prefix is a violation. Empty means the tag
            makes no path claim (pure concurrency label).
        forbidden_prefixes: Path prefixes the task must not touch, checked
            even when ``allowed_prefixes`` is empty.
    """

    tag: str
    allowed_prefixes: tuple[str, ...] = ()
    forbidden_prefixes: tuple[str, ...] = ()

    def violations_for(self, changed_paths: Iterable[str]) -> tuple[str, ...]:
        """Return the changed paths that break this contract."""
        offenders: list[str] = []
        for path in changed_paths:
            norm = path.lstrip("./")
            if any(norm.startswith(bad) for bad in self.forbidden_prefixes):
                offenders.append(path)
                continue
            if self.allowed_prefixes and not any(norm.startswith(ok) for ok in self.allowed_prefixes):
                offenders.append(path)
        return tuple(offenders)


#: Built-in contracts. ``docs-only`` is the canonical example from the issue:
#: a task declaring it whose diff touches ``src/`` is in violation.
TAG_CONTRACTS: dict[str, TagContract] = {
    "docs-only": TagContract(
        tag="docs-only",
        allowed_prefixes=("docs/", "README", "CHANGELOG", "mkdocs.yml"),
        forbidden_prefixes=("src/", "tests/"),
    ),
    "tests-only": TagContract(
        tag="tests-only",
        allowed_prefixes=("tests/",),
        forbidden_prefixes=("src/",),
    ),
    "no-src": TagContract(
        tag="no-src",
        forbidden_prefixes=("src/",),
    ),
}


@dataclass(frozen=True, slots=True)
class TagViolation:
    """One declared tag whose contract the task's diff broke."""

    tag: str
    offending_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"offending_paths": list(self.offending_paths), "tag": self.tag}


def check_conformance(
    declared_tags: Iterable[str],
    changed_paths: Iterable[str],
    *,
    contracts: Mapping[str, TagContract] | None = None,
) -> tuple[TagViolation, ...]:
    """Return the tag violations for a completed task's diff.

    Deterministic: depends only on the declared tags, the changed-path set,
    and the contract table. Two operators checking the same inputs derive the
    identical violation tuple, so the conformance receipt is replay-stable.

    Args:
        declared_tags: Tags the task declared at claim time.
        changed_paths: Worktree diff paths (and, optionally, tool-call
            targets) observed after completion.
        contracts: Contract table override; defaults to :data:`TAG_CONTRACTS`.
    """
    table = dict(TAG_CONTRACTS if contracts is None else contracts)
    paths = list(changed_paths)
    violations: list[TagViolation] = []
    for tag in sorted(set(declared_tags)):
        contract = table.get(tag)
        if contract is None:
            continue
        offenders = contract.violations_for(paths)
        if offenders:
            violations.append(TagViolation(tag=tag, offending_paths=offenders))
    return tuple(violations)


def tags_admissible(
    declared_tags: Iterable[str], tag_occupancy: Mapping[str, int], tag_limits: Mapping[str, int]
) -> bool:
    """Return ``True`` when every declared tag is under its limit (AND-of-all).

    A tag with no configured limit is unbounded. A tag whose limit is ``0`` is
    quarantined and always blocks. Admission requires *all* declared tags to
    pass, mirroring the intersection semantics an operator expects from "this
    task touches the migration lock AND the staging env".

    Args:
        declared_tags: Tags the candidate task declares.
        tag_occupancy: Current count of active grants holding each tag.
        tag_limits: Configured concurrency ceiling per tag.
    """
    for tag in declared_tags:
        if tag not in tag_limits:
            continue
        limit = tag_limits[tag]
        if tag_occupancy.get(tag, 0) >= limit:
            return False
    return True
