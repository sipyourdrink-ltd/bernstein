"""Golden benchmark suite — curated tasks for eval.

Tasks are stored as markdown files with YAML frontmatter in
.sdd/eval/golden/{tier}/. Each file defines a task with
expected outcomes for the harness to verify.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

logger = logging.getLogger(__name__)

Tier = Literal["smoke", "standard", "stretch", "adversarial"]

_TIERS: tuple[Tier, ...] = ("smoke", "standard", "stretch", "adversarial")


@dataclass(frozen=True)
class GoldenTask:
    """A golden benchmark task loaded from disk.

    Attributes:
        id: Unique task identifier.
        tier: Difficulty tier.
        title: Short task title.
        description: Full task description for the agent.
        role: Agent role to assign.
        expected_files_modified: Files the agent should modify.
        expected_test_outcomes: Test commands and expected pass/fail.
        completion_signals: Signals for verifying task completion.
        max_cost_usd: Cost budget for this task.
        max_duration_s: Time budget in seconds.
        owned_files: Files the agent is allowed to modify.
    """

    id: str
    tier: Tier
    title: str
    description: str
    role: str = "backend"
    expected_files_modified: list[str] = field(default_factory=list[str])
    expected_test_outcomes: dict[str, bool] = field(default_factory=dict[str, bool])
    completion_signals: list[str] = field(default_factory=list[str])
    max_cost_usd: float = 1.0
    max_duration_s: int = 300
    owned_files: list[str] = field(default_factory=list[str])


def _parse_golden_file(path: Path, tier: Tier) -> GoldenTask | None:
    """Parse a single golden task markdown file.

    Expected format: YAML frontmatter between --- markers, followed by
    the task description in markdown body.

    Args:
        path: Path to the markdown file.
        tier: The tier directory this task belongs to.

    Returns:
        Parsed GoldenTask, or None if parsing fails.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Cannot read golden task file: %s", path)
        return None

    # Split frontmatter from body
    if not text.startswith("---"):
        logger.warning("Golden task missing YAML frontmatter: %s", path)
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.warning("Golden task has malformed frontmatter: %s", path)
        return None

    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        logger.warning("YAML parse error in %s: %s", path, exc)
        return None

    if not isinstance(meta, dict):
        logger.warning("Golden task frontmatter is not a dict: %s", path)
        return None

    m: dict[str, Any] = dict(cast("dict[str, Any]", meta))
    body = parts[2].strip()
    task_id: str = str(m.get("id", path.stem))

    return GoldenTask(
        id=task_id,
        tier=tier,
        title=str(m.get("title", path.stem)),
        description=body or str(m.get("description", "")),
        role=str(m.get("role", "backend")),
        expected_files_modified=[str(x) for x in m.get("expected_files_modified", [])],
        expected_test_outcomes=dict({str(k): bool(v) for k, v in dict(m.get("expected_test_outcomes", {})).items()}),
        completion_signals=[str(x) for x in m.get("completion_signals", [])],
        max_cost_usd=float(m.get("max_cost_usd", 1.0)),
        max_duration_s=int(m.get("max_duration_s", 300)),
        owned_files=[str(x) for x in m.get("owned_files", [])],
    )


def load_golden_tasks(
    golden_dir: Path | None = None,
    tier_filter: Tier | None = None,
) -> list[GoldenTask]:
    """Load all golden benchmark tasks from disk.

    Args:
        golden_dir: Root directory containing tier subdirectories.
            Defaults to .sdd/eval/golden/ in the current directory.
        tier_filter: If set, only load tasks from this tier.

    Returns:
        List of parsed GoldenTask objects, sorted by tier then id.
    """
    if golden_dir is None:
        golden_dir = Path(".sdd/eval/golden")

    tasks: list[GoldenTask] = []
    tiers_to_scan = (tier_filter,) if tier_filter else _TIERS

    for tier in tiers_to_scan:
        tier_dir = golden_dir / tier
        if not tier_dir.is_dir():
            logger.debug("Golden tier directory missing: %s", tier_dir)
            continue

        for md_file in sorted(tier_dir.glob("*.md")):
            task = _parse_golden_file(md_file, tier)
            if task is not None:
                tasks.append(task)

    logger.info("Loaded %d golden tasks from %s", len(tasks), golden_dir)
    return tasks


def load_single_task(golden_dir: Path, task_id: str) -> GoldenTask | None:
    """Load a single golden task by ID.

    Searches across all tier directories.

    Args:
        golden_dir: Root golden directory.
        task_id: Task ID to find.

    Returns:
        The matching GoldenTask, or None if not found.
    """
    for tier in _TIERS:
        tier_dir = golden_dir / tier
        if not tier_dir.is_dir():
            continue
        for md_file in tier_dir.glob("*.md"):
            task = _parse_golden_file(md_file, tier)
            if task is not None and task.id == task_id:
                return task
    return None
