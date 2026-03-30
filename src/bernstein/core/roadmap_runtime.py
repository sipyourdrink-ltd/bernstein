"""Rolling-wave roadmap emitter for scenario-driven backlog generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import yaml

from bernstein.core.scenario_library import ScenarioLibrary, ScenarioRecipe, load_scenario_library

if TYPE_CHECKING:
    from pathlib import Path


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


def emit_roadmap_wave(workdir: Path, *, max_open_tickets: int = 10) -> list[Path]:
    """Emit next wave of roadmap tickets into ``.sdd/backlog/open``."""
    backlog_open = workdir / ".sdd" / "backlog" / "open"
    if not backlog_open.exists():
        return []
    current_open = len(list(backlog_open.glob("*.yaml")))
    if current_open >= max_open_tickets:
        return []

    roadmaps_dir = workdir / ".sdd" / "roadmaps" / "open"
    if not roadmaps_dir.exists():
        return []

    library_root = workdir / ".bernstein" / "scenarios"
    library = load_scenario_library(library_root)
    if not library.scenarios:
        return []

    runtime_dir = workdir / ".sdd" / "runtime" / "roadmaps"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    emitted: list[Path] = []
    available_slots = max(0, max_open_tickets - current_open)
    for roadmap_file in sorted(list(roadmaps_dir.glob("*.yaml")) + list(roadmaps_dir.glob("*.yml"))):
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
    return emitted


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
    filename = f"{spec.roadmap_id}-{scenario.scenario_id}-{task_idx + 1:02d}-{slug}.md"
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
