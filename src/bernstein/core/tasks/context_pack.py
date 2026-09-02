"""Assembly point for the task context pack (#4522).

A spawned agent is handed the goal and the repo layout, and rediscovers -- or
does not -- what the repository already records about this kind of change.
The evidence exists: :mod:`bernstein.core.tasks.context_extractors` derives
co-change neighbours and the test-to-source map from the commit graph, reads
the nearest ``AGENTS.md`` verbatim, and reports the tests the gate has
quarantined. Nothing assembled them.

This module is that assembly step and nothing more. It adds no storage, calls
no model, and every entry it carries is explainable as "these commits say so"
or "this file says so".

Three properties the callers depend on:

* **Deterministic and content-addressed.** Targets are normalised to a sorted
  set and sections to a fixed order before serialisation, so two assemblies
  over the same repository state produce the same bytes and therefore the same
  address. The address is over :meth:`ContextPack.canonical_bytes`, in the same
  canonical form :mod:`bernstein.core.observability.ticket_bundle` and
  :mod:`bernstein.core.tasks.task_pack` already use -- a repository with two
  canonical forms effectively has none.
* **Bounded, and it says what it cut.** Both the per-list cap and the byte
  budget record what they dropped *inside* the pack. A silently shortened list
  reads as "there was nothing else", which is the failure this feature exists
  to fix.
* **Fail-open.** A repository with no history, a missing ``AGENTS.md`` or an
  unreadable quarantine yields a smaller pack and a logged reason. Assembly
  never raises into the spawn path.

The pack reaches the agent as one named prompt section, so the per-section
hash in :class:`~bernstein.core.agents.context_receipt.ContextReceipt` is the
run record for the spawn that consumed it: re-derive the pack from the same
repository state, re-render, and the hash has to match what the receipt holds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from bernstein.core.tasks.context_extractors import (
    extract_co_change_neighbours,
    extract_test_to_source_map,
    find_nearest_agents_md,
    get_known_flaky_tests,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Operator flag. Off unless explicitly set, for the first release, so a run
#: can be compared with and without the pack.
CONTEXT_PACK_FLAG = "BERNSTEIN_TASK_CONTEXT_PACK"

#: Label the pack carries in the spawn prompt and in the context receipt.
PACK_SECTION_LABEL = "task_context_pack"

DEFAULT_ITEM_LIMIT = 10
DEFAULT_BYTE_BUDGET = 8192

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_CO_CHANGE = "co_change"
_TESTS = "tests"
_AGENTS_MD = "agents_md"
_FLAKY = "flaky_tests"

# Assembly order, which is also the eviction order: the byte budget drops from
# the tail, so the small high-value evidence (the invariants an agent is most
# likely to break, and the tests the gate is already skipping) survives a tight
# budget and the long commit-graph lists are what gives way.
_KIND_RANK = {_AGENTS_MD: 0, _FLAKY: 1, _TESTS: 2, _CO_CHANGE: 3}

_HEADINGS = {
    _CO_CHANGE: "Files that change together with {target}",
    _TESTS: "Tests that changed with {target}",
    _AGENTS_MD: "Nearest AGENTS.md for {target}",
    _FLAKY: "Known flaky tests (the gate deselects these)",
}


def context_pack_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the operator has turned the pack on."""
    source = os.environ if env is None else env
    return source.get(CONTEXT_PACK_FLAG, "").strip().lower() in _TRUTHY


@dataclass(frozen=True, slots=True)
class PackSection:
    """One evidence list, with the count it was cut down from.

    ``available`` is the number of items the evidence actually held. It is
    stored rather than derived so the pack can state the size of what it left
    out, not merely that something was left out.
    """

    kind: str
    target: str
    items: tuple[str, ...]
    available: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target}"

    @property
    def dropped_count(self) -> int:
        return max(0, self.available - len(self.items))

    def replace_items(self, items: tuple[str, ...]) -> PackSection:
        """Return a copy carrying different items, for verification tests."""
        return replace(self, items=items)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "items": list(self.items),
        }
        if self.dropped_count:
            payload["truncated"] = {"kept": len(self.items), "available": self.available}
        return payload


@dataclass(frozen=True, slots=True)
class ContextPack:
    """The assembled pack: sections, what was dropped, and what was missing."""

    sections: tuple[PackSection, ...] = ()
    dropped: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()

    def with_sections(self, sections: tuple[PackSection, ...]) -> ContextPack:
        return replace(self, sections=sections)

    def is_empty(self) -> bool:
        return not any(section.items for section in self.sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sections": [section.to_dict() for section in _ordered(self.sections)],
            "dropped": sorted(self.dropped),
            "unavailable": sorted(self.unavailable),
        }

    def canonical_bytes(self) -> bytes:
        """Serialise to the repository's canonical JSON form."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def content_address(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def render(self) -> str:
        """Render the prompt section, or ``""`` when there is nothing to say.

        An empty pack renders to nothing so the caller appends no section at
        all and the spawn prompt stays byte-identical to what it is without
        the feature.
        """
        if self.is_empty():
            return ""
        lines = [
            "\n## Repository evidence (task context pack)",
            "Derived from this repository's commit graph and tree; no model produced it.",
            f"pack: {self.content_address()}",
        ]
        for section in _ordered(self.sections):
            if not section.items:
                continue
            lines.append("")
            lines.append(f"### {_HEADINGS[section.kind].format(target=section.target)}")
            if section.kind == _AGENTS_MD:
                lines.append(section.items[0].rstrip("\n"))
            else:
                lines.extend(f"- {item}" for item in section.items)
            if section.dropped_count:
                lines.append(
                    f"({section.dropped_count} more not shown; kept {len(section.items)} of {section.available})"
                )
        if self.dropped:
            lines.append("")
            lines.append(f"Dropped to stay inside the size budget: {', '.join(sorted(self.dropped))}")
        if self.unavailable:
            lines.append("")
            lines.append(f"Evidence unavailable for: {', '.join(sorted(self.unavailable))}")
        return "\n".join(lines) + "\n"


def assemble_context_pack(
    repo_root: Path,
    targets: Sequence[str],
    *,
    item_limit: int = DEFAULT_ITEM_LIMIT,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
) -> ContextPack:
    """Assemble the pack for *targets* from the repository's own history.

    *targets* is normalised to a sorted set first, so the caller's ordering --
    which follows task order, not anything about the repository -- cannot reach
    the bytes.
    """
    normalised = sorted({target for target in targets if target})
    unavailable: list[str] = []
    sections: list[PackSection] = []

    co_change = _safe(_CO_CHANGE, unavailable, lambda: extract_co_change_neighbours(repo_root, normalised, limit=None))
    tests = _safe(_TESTS, unavailable, lambda: extract_test_to_source_map(repo_root, normalised, limit=None))
    flaky = _safe(_FLAKY, unavailable, lambda: get_known_flaky_tests(repo_root))

    for target in normalised:
        sections.append(_cut(_CO_CHANGE, target, (co_change or {}).get(target, []), item_limit))
        sections.append(_cut(_TESTS, target, (tests or {}).get(target, []), item_limit))
        agents_md = _safe(
            f"{_AGENTS_MD}:{target}",
            unavailable,
            lambda t=target: find_nearest_agents_md(repo_root / t, repo_root),
        )
        if agents_md:
            sections.append(PackSection(kind=_AGENTS_MD, target=target, items=(agents_md,), available=1))
    sections.append(_cut(_FLAKY, "", flaky or [], item_limit))

    populated = tuple(section for section in sections if section.items)
    return _fit(_ordered(populated), tuple(unavailable), byte_budget)


def render_pack_section(
    workdir: Path,
    targets: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the prompt section for *targets*, or ``""``.

    Empty whenever the flag is off, the evidence is empty, or assembly hit
    something it could not read: a spawn is never blocked by this feature.
    """
    if not context_pack_enabled(env):
        return ""
    try:
        return assemble_context_pack(workdir, targets).render()
    except Exception as exc:  # fail open into the spawn path
        logger.warning("task context pack omitted from the spawn prompt: %s", exc)
        return ""


def _ordered(sections: tuple[PackSection, ...]) -> tuple[PackSection, ...]:
    return tuple(sorted(sections, key=lambda section: (_KIND_RANK[section.kind], section.target, section.kind)))


def _cut(kind: str, target: str, items: Sequence[str], item_limit: int) -> PackSection:
    return PackSection(kind=kind, target=target, items=tuple(items[:item_limit]), available=len(items))


def _fit(sections: tuple[PackSection, ...], unavailable: tuple[str, ...], byte_budget: int) -> ContextPack:
    """Drop sections from the tail of the order until the pack fits.

    Measured against the serialised pack including its own dropped list, so
    the recorded truncation is inside the bound rather than pushing past it.
    """
    kept = list(sections)
    dropped: list[str] = []
    while kept:
        pack = ContextPack(tuple(kept), tuple(dropped), unavailable)
        if len(pack.canonical_bytes()) <= byte_budget:
            return pack
        dropped.append(kept.pop().key)
    return ContextPack((), tuple(dropped), unavailable)


def _safe[Evidence](label: str, unavailable: list[str], produce: Callable[[], Evidence]) -> Evidence | None:
    """Run one extractor, recording the reason instead of raising."""
    try:
        return produce()
    except Exception as exc:  # fail open, one source at a time
        logger.warning("task context pack: %s unavailable: %s", label, exc)
        unavailable.append(label)
        return None
