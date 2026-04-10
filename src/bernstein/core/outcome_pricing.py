"""Outcome-based pricing models.

Extends the existing cost tracker with pricing models that charge based
on task outcomes (success/failure) rather than raw token consumption.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class PricingModel(enum.Enum):
    """Supported pricing strategies."""

    PAY_PER_TOKEN = "pay_per_token"
    PAY_PER_TASK = "pay_per_task"
    PAY_PER_SUCCESS = "pay_per_success"


@dataclass
class OutcomePricingConfig:
    """Configuration for outcome-based pricing.

    Attributes:
        model: Which pricing strategy to apply.
        base_rate_per_task: Flat rate charged per task (used by
            ``PAY_PER_TASK`` and ``PAY_PER_SUCCESS``).
        success_multiplier: Multiplier applied on successful tasks.
        failure_rebate: Fraction of cost refunded on failure (0-1).
    """

    model: PricingModel = PricingModel.PAY_PER_TOKEN
    base_rate_per_task: float = 0.0
    success_multiplier: float = 1.0
    failure_rebate: float = 0.5


def calculate_task_cost(
    config: OutcomePricingConfig,
    token_cost: float,
    task_succeeded: bool,
) -> float:
    """Compute the actual charge for a completed task.

    Args:
        config: Active pricing configuration.
        token_cost: Raw token cost in USD (from the cost tracker).
        task_succeeded: Whether the task completed successfully.

    Returns:
        Charge amount in USD (>= 0).
    """
    mult = config.success_multiplier if task_succeeded else config.failure_rebate
    if config.model == PricingModel.PAY_PER_TOKEN:
        cost = token_cost * mult
    elif config.model == PricingModel.PAY_PER_TASK:
        cost = config.base_rate_per_task
    elif config.model == PricingModel.PAY_PER_SUCCESS:
        cost = config.base_rate_per_task * mult
    else:  # pragma: no cover — exhaustive match
        cost = token_cost

    return max(cost, 0.0)


def generate_invoice(
    config: OutcomePricingConfig,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate task costs into an invoice summary.

    Each task dict must contain at minimum:
      - ``token_cost`` (float): raw token cost
      - ``succeeded`` (bool): whether the task passed

    Optional keys: ``task_id`` (str).

    Args:
        config: Active pricing configuration.
        tasks: List of task dicts to invoice.

    Returns:
        Invoice dict with line items and totals.
    """
    line_items: list[dict[str, Any]] = []
    total: float = 0.0
    success_count = 0
    failure_count = 0

    for task in tasks:
        token_cost = float(task.get("token_cost", 0.0))
        succeeded = bool(task.get("succeeded", False))
        charge = calculate_task_cost(config, token_cost, succeeded)

        item: dict[str, Any] = {
            "task_id": task.get("task_id", "unknown"),
            "token_cost": round(token_cost, 6),
            "succeeded": succeeded,
            "charge": round(charge, 6),
        }
        line_items.append(item)
        total += charge

        if succeeded:
            success_count += 1
        else:
            failure_count += 1

    return {
        "pricing_model": config.model.value,
        "total_tasks": len(tasks),
        "success_count": success_count,
        "failure_count": failure_count,
        "line_items": line_items,
        "total_charge": round(total, 6),
    }
