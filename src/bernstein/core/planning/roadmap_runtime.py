"""Rolling-wave roadmap emitter for scenario-driven backlog generation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import yaml

from bernstein.core.planning.scenario_library import ScenarioLibrary, ScenarioRecipe, load_scenario_library

if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

# Roadmap emission runs on every normal orchestrator tick. Keep a scenario that
# cannot currently be consumed visible without repeating the same warning until
# the process exits. A different workspace or blocking reason remains visible.
_WARNED_SCENARIO_SKIPS: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class RoadmapSpec:
    """Roadmap definition that sequences scenario IDs."""

    roadmap_id: str
    title: str
    scenario_ids: tuple[str, ...]
    wave_size: int = 10


@dataclass
class RoadmapCursor:
    """Persistent cursor for rolling-wave generation."""

    scenario_index: int = 0
    task_index: int = 0


#: Closed set of reasons a wave produced what it produced.
#:
#: Every exit from :func:`emit_roadmap_wave_outcome` carries one, so a tick
#: that emits nothing is never silent about why (issue #5573).
WAVE_REASONS: frozenset[str] = frozenset(
    {
        # Tickets were written.
        "emitted",
        # ``.sdd/backlog/open`` does not exist: nowhere to put a ticket.
        "backlog-missing",
        # Already at or over the open-ticket ceiling.
        "ticket-ceiling",
        # No scenario library: nothing exists to sequence.
        "no-scenarios",
        # Scenarios exist, but no ``.sdd/roadmaps/open`` sequences them.
        # This is the fresh-workspace case: the library was found and is
        # unreachable, which used to be indistinguishable from an empty one.
        "no-roadmap",
        # Roadmaps and scenarios both exist, but no roadmap entry resolved
        # to a ticket this tick (cursor exhausted, ids absent from the
        # library, or every file failed to parse).
        "no-eligible-scenarios",
    }
)


@dataclass(frozen=True)
class RoadmapWaveOutcome:
    """What one wave did, and -- when it did nothing -- why.

    ``emit_roadmap_wave`` returns only the emitted paths, so an empty list
    conflated six different situations. Callers that need to tell them apart
    read this instead.

    Attributes:
        emitted: Ticket files written this wave, in emission order.
        reason: A member of :data:`WAVE_REASONS`.
        scenarios_found: Scenarios loaded from ``.bernstein/scenarios``.
            Non-zero with ``reason="no-roadmap"`` is the case worth
            reporting: the operator wrote scenarios that nothing consumes.
        detail: One sentence a human can act on.
    """

    emitted: tuple[Path, ...]
    reason: str
    scenarios_found: int
    detail: str

    def __post_init__(self) -> None:
        """Keep ``reason`` inside the closed set it advertises."""
        if self.reason not in WAVE_REASONS:
            raise ValueError(f"RoadmapWaveOutcome.reason={self.reason!r} is not in WAVE_REASONS={sorted(WAVE_REASONS)}")


def emit_roadmap_wave(workdir: Path, *, max_open_tickets: int = 10) -> list[Path]:
    """Emit next wave of roadmap tickets into ``.sdd/backlog/open``.

    Thin wrapper kept for the existing call sites; see
    :func:`emit_roadmap_wave_outcome` for the reason a wave was empty.
    """
    outcome = emit_roadmap_wave_outcome(workdir, max_open_tickets=max_open_tickets)
    if not outcome.emitted and outcome.scenarios_found:
        _warn_scenarios_skipped(workdir, outcome.reason, outcome.detail)
    return list(outcome.emitted)


def emit_roadmap_wave_outcome(workdir: Path, *, max_open_tickets: int = 10) -> RoadmapWaveOutcome:
    """Emit the next wave and report what happened, including nothing.

    The scenario library is loaded before every roadmap-emission precondition.
    That ordering is the fix for #5573: a fresh workspace can contain valid
    scenarios while still lacking the backlog or roadmap directories needed to
    emit them. The guards retain their original order and meaning after
    discovery, and each names itself in the outcome instead of hiding a library
    that was never read.
    """
    library_root = workdir / ".bernstein" / "scenarios"
    library = load_scenario_library(library_root)
    scenarios_found = len(library.scenarios)

    backlog_open = workdir / ".sdd" / "backlog" / "open"
    if not backlog_open.exists():
        return RoadmapWaveOutcome(
            emitted=(),
            reason="backlog-missing",
            scenarios_found=scenarios_found,
            detail=f"{backlog_open} does not exist, so there is nowhere to write a ticket.",
        )
    current_open = len(list(backlog_open.glob("*.yaml")))
    if current_open >= max_open_tickets:
        return RoadmapWaveOutcome(
            emitted=(),
            reason="ticket-ceiling",
            scenarios_found=scenarios_found,
            detail=(
                f"{current_open} open ticket(s) at a ceiling of {max_open_tickets}; "
                "close or complete some before the next wave."
            ),
        )

    roadmaps_dir = workdir / ".sdd" / "roadmaps" / "open"
    if not roadmaps_dir.exists():
        return RoadmapWaveOutcome(
            emitted=(),
            reason="no-roadmap",
            scenarios_found=scenarios_found,
            detail=(
                f"Found {scenarios_found} scenario(s) under {library_root} and skipped "
                f"them: {roadmaps_dir} does not exist, and a scenario is only emitted "
                "as a ticket when a roadmap sequences it."
            ),
        )

    roadmap_files = sorted(list(roadmaps_dir.glob("*.yaml")) + list(roadmaps_dir.glob("*.yml")))
    if not library.scenarios:
        return RoadmapWaveOutcome(
            emitted=(),
            reason="no-scenarios",
            scenarios_found=0,
            detail=f"No scenarios under {library_root}, so the roadmaps have nothing to sequence.",
        )
    if not roadmap_files:
        return RoadmapWaveOutcome(
            emitted=(),
            reason="no-roadmap",
            scenarios_found=scenarios_found,
            detail=(
                f"Found {scenarios_found} scenario(s) under {library_root} and skipped them: "
                f"{roadmaps_dir} contains no roadmap YAML definitions to sequence them."
            ),
        )

    runtime_dir = workdir / ".sdd" / "runtime" / "roadmaps"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    emitted: list[Path] = []
    available_slots = max(0, max_open_tickets - current_open)
    for roadmap_file in roadmap_files:
        if available_slots <= 0:
            break
        spec = _load_roadmap(roadmap_file)
        if spec is None:
            continue
        cursor_path = runtime_dir / f"{spec.roadmap_id}.json"
        cursor = _load_cursor(cursor_path)
        new_files = _emit_for_spec(spec, cursor, cursor_path, library, backlog_open, max_items=available_slots)
        emitted.extend(new_files)
        available_slots -= len(new_files)
    if not emitted:
        return RoadmapWaveOutcome(
            emitted=(),
            reason="no-eligible-scenarios",
            scenarios_found=scenarios_found,
            detail=(
                f"{len(roadmap_files)} "
                f"roadmap file(s) and {scenarios_found} scenario(s), but no entry resolved to a "
                "ticket: every cursor is exhausted, or the ids they name are absent from the library."
            ),
        )
    return RoadmapWaveOutcome(
        emitted=tuple(emitted),
        reason="emitted",
        scenarios_found=scenarios_found,
        detail=f"Emitted {len(emitted)} ticket(s) into {backlog_open}.",
    )


def _warn_scenarios_skipped(workdir: Path, reason: str, detail: str) -> None:
    """Warn once when discovered scenarios cannot reach roadmap emission."""
    key = (str(workdir.absolute()), reason)
    if key in _WARNED_SCENARIO_SKIPS:
        return
    _WARNED_SCENARIO_SKIPS.add(key)
    logger.warning("Found workspace scenarios but skipped roadmap emission: %s", detail)


def _emit_for_spec(
    spec: RoadmapSpec,
    cursor: RoadmapCursor,
    cursor_path: Path,
    library: ScenarioLibrary,
    backlog_open: Path,
    *,
    max_items: int,
) -> list[Path]:
    emitted: list[Path] = []
    remaining = min(spec.wave_size, max_items)
    while remaining > 0:
        if cursor.scenario_index >= len(spec.scenario_ids):
            break
        scenario_id = spec.scenario_ids[cursor.scenario_index]
        scenario = library.get(scenario_id)
        if scenario is None:
            cursor.scenario_index += 1
            cursor.task_index = 0
            continue

        while remaining > 0 and cursor.task_index < len(scenario.tasks):
            file_path = _write_ticket(backlog_open, spec, scenario, cursor.task_index)
            if file_path is not None:
                emitted.append(file_path)
                remaining -= 1
            cursor.task_index += 1

        if cursor.task_index >= len(scenario.tasks):
            cursor.scenario_index += 1
            cursor.task_index = 0

    _save_cursor(cursor_path, cursor)
    return emitted


def _write_ticket(backlog_open: Path, spec: RoadmapSpec, scenario: ScenarioRecipe, task_idx: int) -> Path | None:
    tpl = scenario.tasks[task_idx]
    slug = _slugify(tpl.title)
    filename = f"{spec.roadmap_id}-{scenario.scenario_id}-{task_idx + 1:02d}-{slug}.yaml"
    path = backlog_open / filename
    if path.exists():
        return None
    content = (
        f"# {tpl.title}\n\n"
        f"**Role:** {tpl.role}\n"
        f"**Priority:** {tpl.priority}\n"
        f"**Scope:** {tpl.scope}\n"
        f"**Complexity:** {tpl.complexity}\n\n"
        f"## Scenario context\n\n"
        f"- Roadmap: `{spec.roadmap_id}`\n"
        f"- Scenario: `{scenario.scenario_id}` ({scenario.name})\n"
        f"- Step: {task_idx + 1}/{len(scenario.tasks)}\n\n"
        f"{tpl.description}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _load_roadmap(path: Path) -> RoadmapSpec | None:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    raw = cast("dict[str, object]", loaded)
    roadmap_id = str(raw.get("id", "")).strip()
    title = str(raw.get("title", "")).strip()
    scenario_ids_raw = raw.get("scenarios")
    if not roadmap_id or not title or not isinstance(scenario_ids_raw, list):
        return None
    scenario_ids = tuple(str(s).strip() for s in cast("list[object]", scenario_ids_raw) if str(s).strip())
    if not scenario_ids:
        return None
    wave_size = _coerce_int(raw.get("wave_size"), default=10)
    return RoadmapSpec(roadmap_id=roadmap_id, title=title, scenario_ids=scenario_ids, wave_size=max(1, wave_size))


def _load_cursor(path: Path) -> RoadmapCursor:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RoadmapCursor()
    if not isinstance(loaded, dict):
        return RoadmapCursor()
    raw = cast("dict[str, object]", loaded)
    return RoadmapCursor(
        scenario_index=max(0, _coerce_int(raw.get("scenario_index"), default=0)),
        task_index=max(0, _coerce_int(raw.get("task_index"), default=0)),
    )


def _save_cursor(path: Path, cursor: RoadmapCursor) -> None:
    path.write_text(
        json.dumps({"scenario_index": cursor.scenario_index, "task_index": cursor.task_index}, indent=2),
        encoding="utf-8",
    )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "task"


def _coerce_int(value: object, *, default: int) -> int:
    """Coerce a loosely-typed YAML/JSON value to ``int`` with fallback."""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default
