"""Cheaper model retry — use sonnet retry when opus fails."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Model downgrade mapping for retries.
CHEAPER_RETRY_MODEL: dict[str, str] = {
    "opus": "sonnet",
    "sonnet": "haiku",
}

#: Effort downgrade for retries.
CHEAPER_RETRY_EFFORT: dict[str, str] = {
    "max": "high",
    "high": "medium",
}


def get_retry_model(prev_model: str, prev_effort: str) -> tuple[str, str]:
    """Get cheaper model and effort for retry.

    Args:
        prev_model: Previous model that failed.
        prev_effort: Previous effort level.

    Returns:
        Tuple of (retry_model, retry_effort).
    """
    retry_model = CHEAPER_RETRY_MODEL.get(prev_model, prev_model)
    retry_effort = CHEAPER_RETRY_EFFORT.get(prev_effort, prev_effort)

    return retry_model, retry_effort


def should_use_cheaper_retry(task_data: dict[str, Any], retry_count: int) -> bool:
    """Determine if cheaper retry should be used.

    Args:
        task_data: Task data dictionary.
        retry_count: Number of retries so far.

    Returns:
        True if cheaper retry should be used.
    """
    # Only use cheaper retry on first retry
    if retry_count != 1:
        return False

    # Check if previous model was expensive
    prev_model = task_data.get("model", "")
    return prev_model in CHEAPER_RETRY_MODEL


def apply_cheaper_retry(task_data: dict[str, Any]) -> dict[str, Any]:
    """Apply cheaper retry to task data.

    Args:
        task_data: Task data dictionary.

    Returns:
        Modified task data with cheaper model/effort.
    """
    prev_model = task_data.get("model", "sonnet")
    prev_effort = task_data.get("effort", "high")

    retry_model, retry_effort = get_retry_model(prev_model, prev_effort)

    task_data["model"] = retry_model
    task_data["effort"] = retry_effort

    logger.info(
        "Task %s failed on %s/%s, retrying with %s/%s",
        task_data.get("id", "unknown"),
        prev_model,
        prev_effort,
        retry_model,
        retry_effort,
    )

    return task_data
