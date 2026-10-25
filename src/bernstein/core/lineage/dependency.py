"""Cross-task effect footprints and pairwise dependency detection.

The per-change gate evaluates one task at a time; two tasks can jointly
achieve what each alone is blocked for. This module defines the effect
vocabulary recorded per task (ChangeFact, TaskEffects — produced by the
runner's lineage records in production, declared directly in eval fixtures)
and the pairwise predicate deciding which task pairs the cross-task
invariants in bernstein.core.quality.collusion evaluate at merge admission
(#5398).

Deliberately not a general analyser: facts are limited to the shapes the
invariants consume — test weakening, definition vs. reference removal,
config writes/reads. Adding a shape requires a new invariant that consumes
it plus a fixture that exercises it.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

FACT_WEAKEN_TEST = "weaken-test"
FACT_MODIFY_SOURCE = "modify-source"
FACT_REMOVE_DEFINITION = "remove-definition"
FACT_REMOVE_REFERENCE = "remove-reference"
FACT_CONFIG_WRITE = "config-write"
FACT_CONFIG_READ = "config-read"


@dataclass(frozen=True)
class ChangeFact:
    kind: str
    path: str
    symbol: str | None = None
    key: str | None = None
    value: str | None = None
    test_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "symbol": self.symbol,
            "key": self.key,
            "value": self.value,
            "test_target": self.test_target,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ChangeFact:
        return cls(
            kind=raw["kind"],
            path=raw["path"],
            symbol=raw.get("symbol"),
            key=raw.get("key"),
            value=raw.get("value"),
            test_target=raw.get("test_target"),
        )


@dataclass(frozen=True)
class TaskEffects:
    """Lineage-recorded effect footprint of a single task."""

    task_id: str
    facts: tuple[ChangeFact, ...] = ()
    writes: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()

    def facts_of(self, kind: str) -> tuple[ChangeFact, ...]:
        return tuple(f for f in self.facts if f.kind == kind)

    def reads_key(self, key: str) -> bool:
        return any(f.kind == FACT_CONFIG_READ and f.key == key for f in self.facts)

    def touched_paths(self) -> set[str]:
        return set(self.writes) | {f.path for f in self.facts}

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "facts": [f.to_dict() for f in self.facts],
            "writes": sorted(self.writes),
            "reads": sorted(self.reads),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskEffects:
        return cls(
            task_id=raw["task_id"],
            facts=tuple(ChangeFact.from_dict(f) for f in raw.get("facts", [])),
            writes=frozenset(raw.get("writes", ())),
            reads=frozenset(raw.get("reads", ())),
        )


def _removed_definitions(effects: TaskEffects) -> set[str]:
    return {f.symbol for f in effects.facts_of(FACT_REMOVE_DEFINITION) if f.symbol}


def _removed_references(effects: TaskEffects) -> set[str]:
    return {f.symbol for f in effects.facts_of(FACT_REMOVE_REFERENCE) if f.symbol}


def _written_keys(effects: TaskEffects) -> set[str]:
    return {f.key for f in effects.facts_of(FACT_CONFIG_WRITE) if f.key}


def _read_keys(effects: TaskEffects) -> set[str]:
    return {f.key for f in effects.facts_of(FACT_CONFIG_READ) if f.key}


def _weakened_targets(effects: TaskEffects) -> set[str]:
    return {(f.test_target or f.path) for f in effects.facts_of(FACT_WEAKEN_TEST)}


def coupled(a: TaskEffects, b: TaskEffects) -> bool:
    """True when the pair's combined effect can differ from either alone."""
    if a.writes & b.reads or b.writes & a.reads:
        return True  # path-level read-of-write
    if a.writes & b.writes:
        return True  # overlapping writes
    if _removed_definitions(a) & _removed_references(b):
        return True  # symbol-level: definition vs. call site
    if _removed_definitions(b) & _removed_references(a):
        return True
    if _written_keys(a) & _read_keys(b) or _written_keys(b) & _read_keys(a):
        return True  # config write under a reader
    if _weakened_targets(a) & b.touched_paths():
        return True  # weakened test vs. the module it targets
    return bool(_weakened_targets(b) & a.touched_paths())


def detect_dependencies(
    effects: list[TaskEffects],
) -> list[tuple[TaskEffects, TaskEffects]]:
    """All dependent pairs, in submission order (i < j)."""
    return [(a, b) for a, b in combinations(effects, 2) if coupled(a, b)]
