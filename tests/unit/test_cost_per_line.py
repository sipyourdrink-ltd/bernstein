import pytest

from bernstein.core.cost_per_line import CostEfficiency, compute_efficiency


def test_basic_efficiency() -> None:
    tasks = [
        {"lines_changed": 100, "cost_usd": 0.50},
        {"lines_changed": 200, "cost_usd": 0.30},
    ]
    result = compute_efficiency(tasks, total_cost_usd=0.80)
    assert isinstance(result, CostEfficiency)
    assert result.run_avg_cost_per_line == pytest.approx(round(0.80 / 300, 6))
    assert result.current_cost_per_line == pytest.approx(round(0.30 / 200, 6))
    assert result.total_lines_changed == 300
    assert result.total_cost_usd == pytest.approx(0.80)


def test_empty_tasks() -> None:
    result = compute_efficiency([], total_cost_usd=0.0)
    assert result.total_lines_changed == 0
    assert result.total_cost_usd == pytest.approx(0.0)
    assert result.current_cost_per_line == pytest.approx(0.0)


def test_historical_avg() -> None:
    tasks = [{"lines_changed": 50, "cost_usd": 0.10}]
    result = compute_efficiency(tasks, total_cost_usd=0.10, historical_avg=0.005)
    assert result.historical_avg_cost_per_line == pytest.approx(0.005)
