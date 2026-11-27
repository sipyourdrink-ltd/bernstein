"""Manager LLM response parsing.

Parses JSON responses from the LLM for task planning, review, and
queue review operations, converting them into domain models.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, cast

from bernstein.core.models import (
    CompletionSignal,
    Complexity,
    RiskAssessment,
    RollbackPlan,
    Scope,
    Task,
    TaskStatus,
    TaskType,
    UpgradeProposalDetails,
)
from bernstein.core.orchestration.manager_models import (
    _VALID_VERDICTS,
    QueueCorrection,
    QueueReviewResult,
)
from bernstein.core.tasks.models import DispatchTier
from bernstein.core.tasks.schema_retry import (
    AskAgain,
    SchemaRetryContext,
    validate_with_retry,
)

logger = logging.getLogger(__name__)


def _extract_json(raw: str) -> str:
    """Extract JSON from LLM output, stripping markdown fences if present.

    Args:
        raw: Raw LLM response text.

    Returns:
        Cleaned string that should be valid JSON.
    """
    text = raw.strip()
    # Strip markdown code fences.
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


def parse_queue_review_response(raw: str) -> QueueReviewResult:
    """Parse the LLM queue review response.

    Args:
        raw: Raw LLM response (should be a JSON object).

    Returns:
        QueueReviewResult with corrections to apply.

    Raises:
        ValueError: If the response is not valid JSON.
    """
    cleaned = _extract_json(raw)
    try:
        parsed: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Queue review response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")
    result_dict = cast("dict[str, Any]", parsed)

    corrections: list[QueueCorrection] = []
    raw_corrections = cast("list[dict[str, Any]]", result_dict.get("corrections", []))
    for raw_c in raw_corrections:
        action: str = raw_c.get("action", "")
        if action not in ("reassign", "cancel", "change_priority", "add_task"):
            logger.warning("Skipping unknown correction action: %r", action)
            continue
        corrections.append(
            QueueCorrection(
                action=action,  # type: ignore[arg-type]  # narrowed by guard above
                task_id=str(raw_c.get("task_id", "")),
                new_role=str(raw_c.get("new_role", "")),
                new_priority=int(raw_c.get("new_priority", 0)),
                reason=str(raw_c.get("reason", "")),
                new_task={
                    "title": str(raw_c.get("title", "")),
                    "role": str(raw_c.get("role", "backend")),
                    "description": str(raw_c.get("description", raw_c.get("title", ""))),
                    "priority": int(raw_c.get("priority", 2)),
                }
                if action == "add_task"
                else None,
            )
        )
    return QueueReviewResult(
        corrections=corrections,
        reasoning=str(result_dict.get("reasoning", "")),
    )


def _decode_tasks_payload(raw: str) -> list[dict[str, Any]]:
    """Pure validator: cleans and decodes a JSON-array tasks payload.

    Used as the ``validate`` argument to :func:`validate_with_retry`
    and as the implementation of :func:`parse_tasks_response` for the
    one-shot path.
    """
    cleaned = _extract_json(raw)
    try:
        parsed: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("LLM failed to produce valid JSON. Raw response:\n%s", raw)
        raise ValueError(f"LLM response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array, got {type(parsed).__name__}")
    return cast("list[dict[str, Any]]", parsed)


def parse_tasks_response(
    raw: str,
    *,
    ctx: SchemaRetryContext | None = None,
    ask_again: AskAgain | None = None,
    base_prompt: str = "",
) -> list[dict[str, Any]]:
    """Parse the LLM planning response into raw task dicts.

    When ``ctx`` and ``ask_again`` are supplied, malformed payloads are
    retried via :func:`validate_with_retry`; the validation error is
    fed back into the next attempt's prompt.  Without them, the call
    behaves as before (one-shot, raises ``ValueError`` on first failure).

    Args:
        raw: Raw LLM response (should be a JSON array of task objects).
        ctx: Optional retry context for cross-step error accumulation.
        ask_again: Optional spawner used to re-prompt the agent on failure.
        base_prompt: Optional prompt body appended after the error preamble.

    Returns:
        List of parsed task dictionaries.

    Raises:
        ValueError: If the response is not valid JSON or not a list and
            no retry path is configured.
        SchemaRetryExhausted: If the retry path is configured and all
            attempts fail.
    """
    if ctx is not None and ask_again is not None:
        return validate_with_retry(
            initial_response=raw,
            validate=_decode_tasks_payload,
            ctx=ctx,
            ask_again=ask_again,
            step_id="manager.plan",
            base_prompt=base_prompt,
        )
    return _decode_tasks_payload(raw)


def _parse_completion_signal(raw: dict[str, str]) -> CompletionSignal:
    """Parse a single completion signal from a JSON dict.

    Args:
        raw: Dict with 'type' and 'value' keys.

    Returns:
        CompletionSignal dataclass.

    Raises:
        ValueError: If required keys are missing or type is invalid.
    """
    valid_types = {"path_exists", "glob_exists", "test_passes", "file_contains", "llm_review"}
    sig_type = raw.get("type", "")
    sig_value = raw.get("value", "")
    if sig_type not in valid_types:
        raise ValueError(f"Invalid completion signal type: {sig_type!r}")
    if not sig_value:
        raise ValueError(f"Completion signal value cannot be empty (type={sig_type})")
    return CompletionSignal(
        type=sig_type,  # type: ignore[arg-type]
        value=sig_value,
    )


def _parse_risk_assessment(raw: dict[str, Any]) -> RiskAssessment:
    """Parse risk assessment from a JSON dict.

    Args:
        raw: Dict with risk assessment fields.

    Returns:
        RiskAssessment dataclass.
    """
    return RiskAssessment(
        level=raw.get("level", "medium"),
        breaking_changes=raw.get("breaking_changes", False),
        affected_components=raw.get("affected_components", []),
        mitigation=raw.get("mitigation", ""),
    )


def _parse_rollback_plan(raw: dict[str, Any]) -> RollbackPlan:
    """Parse rollback plan from a JSON dict.

    Args:
        raw: Dict with rollback plan fields.

    Returns:
        RollbackPlan dataclass.
    """
    return RollbackPlan(
        steps=raw.get("steps", []),
        revert_commit=raw.get("revert_commit"),
        data_migration=raw.get("data_migration", ""),
        estimated_rollback_minutes=int(raw.get("estimated_rollback_minutes", 30)),
    )


def _parse_upgrade_details(raw: dict[str, Any]) -> UpgradeProposalDetails:
    """Parse upgrade proposal details from a JSON dict.

    Args:
        raw: Dict with upgrade proposal fields.

    Returns:
        UpgradeProposalDetails dataclass.
    """
    risk_raw = raw.get("risk_assessment", {})
    rollback_raw = raw.get("rollback_plan", {})
    return UpgradeProposalDetails(
        current_state=raw.get("current_state", ""),
        proposed_change=raw.get("proposed_change", ""),
        benefits=raw.get("benefits", []),
        risk_assessment=_parse_risk_assessment(risk_raw),
        rollback_plan=_parse_rollback_plan(rollback_raw),
        cost_estimate_usd=float(raw.get("cost_estimate_usd", 0.0)),
        performance_impact=raw.get("performance_impact", ""),
    )


_RESEARCH_PURPOSE = re.compile(
    r"^(?:research|survey|open-ended analysis|feasibility study|(?:assess|evaluate) (?:the )?feasibility)\b", re.I
)
_SEARCH_PURPOSE = re.compile(
    r"^(?:search (?:the web|online)|web (?:search|lookup)|look up\b.*\bonline|"
    r"fetch (?:the )?(?:docs|documentation)|"
    r"extract (?:the )?urls?)\b",
    re.I,
)
_IMPLEMENTATION_FOLLOWUP = re.compile(
    r"\b(?:and|then|to)\s+(?:implement|fix|repair|modify|change|patch|add|remove|update|refactor|write|edit|build)\b",
    re.I,
)
_LOCAL_SOURCE = re.compile(
    r"\b(?:in|from)\s+(?:the\s+)?(?:local\b|workspace\b|repo(?:sitory)?\b|[\w./-]+\.[\w-]+\b)",
    re.I,
)


def _read_dispatch_hints(raw: object, index: int) -> dict[str, int | bool]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring malformed dispatch_hints in task %d", index)
        return {}
    hints: dict[str, int | bool] = {}
    lines = raw.get("estimated_changed_lines")
    if type(lines) is int and lines >= 0:
        hints["estimated_changed_lines"] = lines
    elif lines is not None:
        logger.warning("Ignoring invalid dispatch_hints.estimated_changed_lines in task %d", index)
    shell = raw.get("requires_shell")
    if isinstance(shell, bool):
        hints["requires_shell"] = shell
    elif shell is not None:
        logger.warning("Ignoring invalid dispatch_hints.requires_shell in task %d", index)
    return hints


def _one_concrete_file(files: object) -> bool:
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], str):
        return False
    path = files[0]
    return (
        bool(path.strip())
        and not any(char in path for char in "*?[]{}\\:\0")
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def _select_dispatch_tier(task: Task, raw: dict[str, Any], hints: dict[str, int | bool], index: int) -> DispatchTier:
    if "dispatch_tier" in raw:
        explicit = raw["dispatch_tier"]
        if isinstance(explicit, str):
            try:
                return DispatchTier(explicit)
            except ValueError:
                pass
        raise ValueError(f"Invalid dispatch_tier in task {index}: expected a supported tier string")

    if hints.get("requires_shell") is True or any(s.type == "test_passes" for s in task.completion_signals):
        return DispatchTier.SANDBOX
    title = task.title.strip() if isinstance(task.title, str) else ""
    if not _IMPLEMENTATION_FOLLOWUP.search(title):
        if task.task_type is TaskType.RESEARCH or _RESEARCH_PURPOSE.match(title):
            return DispatchTier.RESEARCH
        if _SEARCH_PURPOSE.match(title) and not _LOCAL_SOURCE.search(title):
            return DispatchTier.SEARCH
    lines = hints.get("estimated_changed_lines")
    if (
        lines is not None
        and lines < 10
        and hints.get("requires_shell") is False
        and _one_concrete_file(task.owned_files)
    ):
        return DispatchTier.EDGE
    return DispatchTier.LOCAL


def dispatch_distribution(tasks: list[Task]) -> str:
    """Summarize selected generated tasks in stable tier order."""
    counts = Counter(task.dispatch_tier for task in tasks)
    return f"{len(tasks)} tasks: " + ", ".join(f"{counts[tier]} {tier.value}" for tier in DispatchTier)


def raw_dicts_to_tasks(
    raw_tasks: list[dict[str, Any]], id_prefix: str = "task", *, classify_dispatch: bool = False
) -> list[Task]:
    """Convert parsed JSON task dicts into domain Task objects.

    Invalid tasks are logged and skipped rather than causing a hard failure.

    Args:
        raw_tasks: List of dicts from ``parse_tasks_response``.
        id_prefix: Prefix for generated task IDs.
        classify_dispatch: Select dispatch tiers for generated plan tasks.

    Returns:
        List of valid Task objects.
    """
    tasks: list[Task] = []
    for i, raw in enumerate(raw_tasks):
        try:
            task = _parse_single_task(raw, i, id_prefix)
            if task is not None:
                if classify_dispatch:
                    hints = _read_dispatch_hints(raw.get("dispatch_hints"), i)
                    if hints:
                        task.metadata["dispatch_hints"] = hints
                    task.dispatch_tier = _select_dispatch_tier(task, raw, hints, i)
                tasks.append(task)
        except (ValueError, KeyError) as exc:
            logger.warning("Skipping task %d due to parse error: %s", i, exc)

    return tasks


def _parse_completion_signals_safe(raw: dict[str, Any], index: int) -> list[CompletionSignal]:
    """Parse completion signals from a raw task dict, skipping invalid ones."""
    signals: list[CompletionSignal] = []
    for sig_raw in raw.get("completion_signals", []):
        try:
            signals.append(_parse_completion_signal(sig_raw))
        except ValueError as exc:
            logger.warning("Skipping invalid signal in task %d: %s", index, exc)
    return signals


def _parse_task_type_safe(raw: dict[str, Any], index: int) -> TaskType:
    """Parse task type with fallback to STANDARD."""
    task_type_raw = raw.get("task_type", "standard")
    try:
        return TaskType(task_type_raw)
    except ValueError:
        logger.warning("Invalid task_type %r in task %d, defaulting to standard", task_type_raw, index)
        return TaskType.STANDARD


def _parse_single_task(raw: dict[str, Any], index: int, id_prefix: str) -> Task | None:
    """Parse a single raw dict into a Task, or return None if invalid."""
    title = raw.get("title", "")
    if not title:
        logger.warning("Skipping task %d: missing title", index)
        return None

    signals = _parse_completion_signals_safe(raw, index)

    depends_on_raw: Any = raw.get("depends_on", [])
    depends_on: list[Any] = cast("list[Any]", depends_on_raw) if isinstance(depends_on_raw, list) else []

    task_type = _parse_task_type_safe(raw, index)

    upgrade_details = None
    if task_type == TaskType.UPGRADE_PROPOSAL and "upgrade_details" in raw:
        try:
            upgrade_details = _parse_upgrade_details(raw["upgrade_details"])
        except (ValueError, KeyError) as exc:
            logger.warning("Failed to parse upgrade_details in task %d: %s", index, exc)

    return Task(
        id=f"{id_prefix}-{index + 1:03d}",
        title=title,
        description=raw.get("description", title),
        role=raw.get("role", "backend"),
        priority=int(raw.get("priority", 2)),
        scope=Scope(raw.get("scope", "medium")),
        complexity=Complexity(raw.get("complexity", "medium")),
        estimated_minutes=int(raw.get("estimated_minutes", 60)),
        status=TaskStatus.OPEN,
        task_type=task_type,
        upgrade_details=upgrade_details,
        depends_on=[str(d) for d in depends_on],
        owned_files=raw.get("owned_files", []),
        completion_signals=signals,
    )


def _resolve_depends_on(tasks: list[Task]) -> None:
    """Resolve depends_on from titles to task IDs in-place.

    The LLM outputs dependency titles (because it doesn't know IDs yet).
    This maps them to the generated IDs so the server can enforce ordering.

    Args:
        tasks: List of tasks with depends_on containing titles.
    """
    title_to_id: dict[str, str] = {}
    for task in tasks:
        title_to_id[task.title] = task.id
        # Also index lowercase for fuzzy matching.
        title_to_id[task.title.lower()] = task.id

    for task in tasks:
        resolved: list[str] = []
        for dep in task.depends_on:
            # Try exact match, then case-insensitive.
            dep_id = title_to_id.get(dep) or title_to_id.get(dep.lower())
            if dep_id:
                resolved.append(dep_id)
            else:
                logger.warning(
                    "Task %s depends on %r which was not found - dropping dependency",
                    task.id,
                    dep,
                )
        task.depends_on = resolved


def parse_review_response(raw: str) -> dict[str, Any]:
    """Parse the LLM review response into a result dict.

    Args:
        raw: Raw LLM response (should be a JSON object).

    Returns:
        Parsed review dict with verdict, reasoning, feedback, follow_up_tasks.

    Raises:
        ValueError: If the response is not valid JSON or missing required keys.
    """
    cleaned = _extract_json(raw)
    try:
        parsed: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM review response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")

    result: dict[str, Any] = cast("dict[str, Any]", parsed)
    verdict: str = str(result.get("verdict", ""))
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict!r}. Must be one of {sorted(_VALID_VERDICTS)}")

    return result
