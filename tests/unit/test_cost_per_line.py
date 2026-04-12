from bernstein.core.cost_per_line import CostEfficiency, compute_efficiency

def test_basic_efficiency():
    tasks = [
        {"lines_changed": 100, "cost_usd": 0.50},
        {"lines_changed": 200, "cost_usd": 0.30},
    ]
    result = compute_efficiency(tasks, total_cost_usd=0.80)
    assert result.run_avg_cost_per_line == round(0.80 / 300, 6)
    assert result.current_cost_per_line == round(0.30 / 200, 6)

def test_empty_tasks():
    result = compute_efficiency([], total_cost_usd=0.0)
    assert result.total_lines_changed == 0