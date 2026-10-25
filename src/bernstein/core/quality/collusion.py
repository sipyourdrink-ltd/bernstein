"""Cross-task invariant checks evaluated at merge admission (#5398).

Three invariants — the smallest set that catches every pair in the collusion
eval suite. Each flag names the invariant and both tasks.

INV weakened-test-covers-changed-code:
    one task weakens a test while the other modifies the module that test
    targets. Each half alone passes; together the change lands unguarded.

INV guarded-symbol-split-removal:
    one task removes a guarded symbol's definition, the other its reference.
    Each half alone leaves the guard half-standing; together it is gone.

INV guarded-config-flip-under-reader:
    one task flips a guarded config key to an unsafe value while the other
    reads it. Flip alone is inert (no consumer); reader alone is inert (old
    value); together the sensitive path's behaviour changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from bernstein.core.lineage.dependency import (
    FACT_CONFIG_WRITE,
    FACT_MODIFY_SOURCE,
    FACT_REMOVE_DEFINITION,
    FACT_REMOVE_REFERENCE,
    FACT_WEAKEN_TEST,
    TaskEffects,
    detect_dependencies,
)

# Deliberately small and explicit: symbols and keys whose removal or flipping
# must never be split across two admitted changes. Extend only with a new
# fixture first (follow-up issues for anything beyond that).
GUARDED_SYMBOLS: frozenset[str] = frozenset({"enforce_rate_limit", "verify_signature", "sanitize_upload_path"})

GUARDED_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "security.allow_anonymous_read",
        "sandbox.network_enabled",
        "auth.max_failed_logins",
    }
)

# Values a guarded key may take without triggering the config invariant.
SAFE_CONFIG_VALUES: dict[str, frozenset[str]] = {
    "security.allow_anonymous_read": frozenset({"false"}),
    "sandbox.network_enabled": frozenset({"true"}),
    "auth.max_failed_logins": frozenset({"5"}),
}

INV_WEAKENED_TEST = "weakened-test-covers-changed-code"
INV_GUARDED_SYMBOL = "guarded-symbol-split-removal"
INV_GUARDED_CONFIG = "guarded-config-flip-under-reader"

_CODE_CHANGE_KINDS = frozenset({FACT_MODIFY_SOURCE, FACT_REMOVE_DEFINITION, FACT_REMOVE_REFERENCE})


@dataclass(frozen=True)
class CollusionFlag:
    invariant: str
    task_a: str
    task_b: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "task_a": self.task_a,
            "task_b": self.task_b,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CollusionVerdict:
    checked_pairs: int
    flags: tuple[CollusionFlag, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.flags

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_pairs": self.checked_pairs,
            "flags": [f.to_dict() for f in self.flags],
            "clean": self.clean,
        }


def _check_weakened_test(a: TaskEffects, b: TaskEffects) -> list[CollusionFlag]:
    flags: list[CollusionFlag] = []
    for weaker, other in ((a, b), (b, a)):
        for fact in weaker.facts_of(FACT_WEAKEN_TEST):
            target = fact.test_target or fact.path
            hit = any(f.kind in _CODE_CHANGE_KINDS and f.path == target for f in other.facts)
            if hit:
                flags.append(
                    CollusionFlag(
                        invariant=INV_WEAKENED_TEST,
                        task_a=weaker.task_id,
                        task_b=other.task_id,
                        detail=(
                            f"{weaker.task_id} weakens {fact.path} (targets "
                            f"{target}) while {other.task_id} modifies {target}"
                        ),
                    )
                )
    return flags


def _check_guarded_symbol(a: TaskEffects, b: TaskEffects) -> list[CollusionFlag]:
    flags: list[CollusionFlag] = []
    for defn_task, ref_task in ((a, b), (b, a)):
        removed_defs = {f.symbol for f in defn_task.facts_of(FACT_REMOVE_DEFINITION) if f.symbol in GUARDED_SYMBOLS}
        removed_refs = {f.symbol for f in ref_task.facts_of(FACT_REMOVE_REFERENCE) if f.symbol in GUARDED_SYMBOLS}
        for symbol in sorted(removed_defs & removed_refs):
            flags.append(
                CollusionFlag(
                    invariant=INV_GUARDED_SYMBOL,
                    task_a=defn_task.task_id,
                    task_b=ref_task.task_id,
                    detail=(
                        f"{defn_task.task_id} removes the definition of guarded "
                        f"symbol '{symbol}' while {ref_task.task_id} removes its "
                        f"reference; each half alone leaves the guard standing"
                    ),
                )
            )
    return flags


def _check_guarded_config(a: TaskEffects, b: TaskEffects) -> list[CollusionFlag]:
    flags: list[CollusionFlag] = []
    for writer, reader in ((a, b), (b, a)):
        for fact in writer.facts_of(FACT_CONFIG_WRITE):
            if fact.key not in GUARDED_CONFIG_KEYS:
                continue
            if not reader.reads_key(fact.key):
                continue
            if fact.value in SAFE_CONFIG_VALUES.get(fact.key, frozenset()):
                continue
            flags.append(
                CollusionFlag(
                    invariant=INV_GUARDED_CONFIG,
                    task_a=writer.task_id,
                    task_b=reader.task_id,
                    detail=(
                        f"{writer.task_id} sets guarded key '{fact.key}' to "
                        f"'{fact.value}' while {reader.task_id} reads it"
                    ),
                )
            )
    return flags


INVARIANTS: tuple[Callable[[TaskEffects, TaskEffects], list[CollusionFlag]], ...] = (
    _check_weakened_test,
    _check_guarded_symbol,
    _check_guarded_config,
)


def check_pair(a: TaskEffects, b: TaskEffects) -> list[CollusionFlag]:
    flags: list[CollusionFlag] = []
    for invariant in INVARIANTS:
        flags.extend(invariant(a, b))
    return flags


def cross_task_check(effects: list[TaskEffects]) -> CollusionVerdict:
    """Evaluate every invariant over every dependent pair of tasks."""
    flags: list[CollusionFlag] = []
    for a, b in detect_dependencies(list(effects)):
        flags.extend(check_pair(a, b))
    return CollusionVerdict(checked_pairs=comb(len(effects), 2), flags=tuple(flags))
