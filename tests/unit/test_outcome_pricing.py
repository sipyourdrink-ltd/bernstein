"""Tests for bernstein.core.outcome_pricing — outcome-based pricing models."""

from __future__ import annotations

import pytest
from bernstein.core.outcome_pricing import (
    OutcomePricingConfig,
    PricingModel,
    calculate_task_cost,
    generate_invoice,
)

# ---------------------------------------------------------------------------
# calculate_task_cost
# ---------------------------------------------------------------------------


class TestCalculateTaskCost:
    def test_pay_per_token_success(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_TOKEN,
            success_multiplier=1.0,
        )
        cost = calculate_task_cost(cfg, token_cost=0.10, task_succeeded=True)
        assert cost == pytest.approx(0.10)

    def test_pay_per_token_failure_rebate(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_TOKEN,
            failure_rebate=0.5,
        )
        cost = calculate_task_cost(cfg, token_cost=0.10, task_succeeded=False)
        assert cost == pytest.approx(0.05)

    def test_pay_per_token_custom_multiplier(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_TOKEN,
            success_multiplier=2.0,
        )
        cost = calculate_task_cost(cfg, token_cost=0.10, task_succeeded=True)
        assert cost == pytest.approx(0.20)

    def test_pay_per_task(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_TASK,
            base_rate_per_task=0.50,
        )
        cost_ok = calculate_task_cost(cfg, token_cost=0.10, task_succeeded=True)
        cost_fail = calculate_task_cost(cfg, token_cost=0.10, task_succeeded=False)
        assert cost_ok == pytest.approx(0.50)
        assert cost_fail == pytest.approx(0.50)

    def test_pay_per_success_succeeded(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_SUCCESS,
            base_rate_per_task=1.00,
            success_multiplier=1.0,
        )
        cost = calculate_task_cost(cfg, token_cost=0.10, task_succeeded=True)
        assert cost == pytest.approx(1.00)

    def test_pay_per_success_failed(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_SUCCESS,
            base_rate_per_task=1.00,
            failure_rebate=0.25,
        )
        cost = calculate_task_cost(cfg, token_cost=0.10, task_succeeded=False)
        assert cost == pytest.approx(0.25)

    def test_cost_never_negative(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_TOKEN,
            failure_rebate=0.0,
        )
        cost = calculate_task_cost(cfg, token_cost=-5.0, task_succeeded=False)
        assert cost >= 0.0


# ---------------------------------------------------------------------------
# generate_invoice
# ---------------------------------------------------------------------------


class TestGenerateInvoice:
    def test_empty_tasks(self) -> None:
        cfg = OutcomePricingConfig()
        invoice = generate_invoice(cfg, [])
        assert invoice["total_tasks"] == 0
        assert invoice["total_charge"] == pytest.approx(0.0)
        assert invoice["line_items"] == []

    def test_mixed_success_failure(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_TOKEN,
            success_multiplier=1.0,
            failure_rebate=0.5,
        )
        tasks = [
            {"task_id": "t1", "token_cost": 0.10, "succeeded": True},
            {"task_id": "t2", "token_cost": 0.20, "succeeded": False},
            {"task_id": "t3", "token_cost": 0.30, "succeeded": True},
        ]
        invoice = generate_invoice(cfg, tasks)
        assert invoice["total_tasks"] == 3
        assert invoice["success_count"] == 2
        assert invoice["failure_count"] == 1
        # t1: 0.10, t2: 0.10, t3: 0.30
        assert invoice["total_charge"] == pytest.approx(0.50)
        assert invoice["pricing_model"] == "pay_per_token"

    def test_pay_per_task_invoice(self) -> None:
        cfg = OutcomePricingConfig(
            model=PricingModel.PAY_PER_TASK,
            base_rate_per_task=0.25,
        )
        tasks = [
            {"task_id": "a", "token_cost": 0.50, "succeeded": True},
            {"task_id": "b", "token_cost": 1.00, "succeeded": False},
        ]
        invoice = generate_invoice(cfg, tasks)
        assert invoice["total_charge"] == pytest.approx(0.50)

    def test_line_items_have_task_ids(self) -> None:
        cfg = OutcomePricingConfig()
        tasks = [{"task_id": "task-42", "token_cost": 0.01, "succeeded": True}]
        invoice = generate_invoice(cfg, tasks)
        assert invoice["line_items"][0]["task_id"] == "task-42"

    def test_missing_task_id_defaults_to_unknown(self) -> None:
        cfg = OutcomePricingConfig()
        tasks = [{"token_cost": 0.01, "succeeded": True}]
        invoice = generate_invoice(cfg, tasks)
        assert invoice["line_items"][0]["task_id"] == "unknown"
