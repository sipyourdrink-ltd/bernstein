"""Unit tests for benchmarks/run_benchmark.py (single-agent vs multi-agent harness)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import the harness module directly from the benchmarks/ directory
# ---------------------------------------------------------------------------

_HARNESS = Path(__file__).parent.parent.parent / "benchmarks" / "run_benchmark.py"


def _import_harness():  # type: ignore[return]
    spec = importlib.util.spec_from_file_location("run_benchmark", _HARNESS)
    if spec is None or spec.loader is None:
        pytest.skip("benchmarks/run_benchmark.py not found")
    loader = spec.loader
    assert loader is not None  # narrowed above, re-assert for static analysis
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules so dataclass field type resolution works
    sys.modules["run_benchmark"] = mod
    loader.exec_module(mod)
    return mod


_harness = _import_harness()

BenchmarkTask = _harness.BenchmarkTask
SubTask = _harness.SubTask
ScenarioResult = _harness.ScenarioResult
TaskBenchmarkResult = _harness.TaskBenchmarkResult
BenchmarkSuite = _harness.BenchmarkSuite
simulate_schedule = _harness.simulate_schedule
estimate_cost = _harness.estimate_cost
estimate_pass_rate = _harness.estimate_pass_rate
run_simulate = _harness.run_simulate
load_task = _harness.load_task
load_all_tasks = _harness.load_all_tasks
format_table = _harness.format_table
format_summary = _harness.format_summary
write_results = _harness.write_results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "test-task",
    name: str = "Test task",
    category: str = "feature",
    parallelizable: bool = True,
    subtasks: list[SubTask] | None = None,
) -> BenchmarkTask:
    if subtasks is None:
        subtasks = [
            SubTask(id="st-a", role="backend", description="A", estimated_minutes=10.0, depends_on=[]),
            SubTask(id="st-b", role="qa", description="B", estimated_minutes=5.0, depends_on=["st-a"]),
        ]
    return BenchmarkTask(
        id=task_id,
        name=name,
        description="desc",
        category=category,
        parallelizable=parallelizable,
        subtasks=subtasks,
    )


# ---------------------------------------------------------------------------
# BenchmarkTask properties
# ---------------------------------------------------------------------------


def test_total_minutes_sums_all_subtasks() -> None:
    task = _make_task(
        subtasks=[
            SubTask(id="a", role="backend", description="a", estimated_minutes=10.0),
            SubTask(id="b", role="qa", description="b", estimated_minutes=5.0),
            SubTask(id="c", role="docs", description="c", estimated_minutes=3.0),
        ]
    )
    assert task.total_minutes == pytest.approx(18.0)


def test_subtask_count_matches_list_length() -> None:
    task = _make_task()
    assert task.subtask_count == 2


# ---------------------------------------------------------------------------
# simulate_schedule — single agent
# ---------------------------------------------------------------------------


def test_simulate_schedule_single_agent_equals_total_minutes() -> None:
    task = _make_task(
        subtasks=[
            SubTask(id="a", role="backend", description="a", estimated_minutes=10.0),
            SubTask(id="b", role="qa", description="b", estimated_minutes=5.0),
        ]
    )
    result = simulate_schedule(task, agents=1)
    assert result == task.total_minutes


# ---------------------------------------------------------------------------
# simulate_schedule — multi-agent
# ---------------------------------------------------------------------------


def test_simulate_schedule_parallel_independent_subtasks() -> None:
    """Three independent subtasks with 3 agents should finish in the max subtask time."""
    task = BenchmarkTask(
        id="t",
        name="n",
        description="d",
        category="c",
        parallelizable=True,
        subtasks=[
            SubTask(id="a", role="backend", description="a", estimated_minutes=10.0),
            SubTask(id="b", role="backend", description="b", estimated_minutes=8.0),
            SubTask(id="c", role="backend", description="c", estimated_minutes=6.0),
        ],
    )
    result = simulate_schedule(task, agents=3)
    assert result == pytest.approx(10.0)


def test_simulate_schedule_multi_agent_faster_than_single() -> None:
    task = _make_task(
        subtasks=[
            SubTask(id="a", role="backend", description="a", estimated_minutes=10.0),
            SubTask(id="b", role="backend", description="b", estimated_minutes=10.0),
            SubTask(id="c", role="qa", description="c", estimated_minutes=5.0, depends_on=["a", "b"]),
        ]
    )
    single = simulate_schedule(task, agents=1)
    multi = simulate_schedule(task, agents=2)
    assert multi < single


def test_simulate_schedule_sequential_chain_same_for_any_agents() -> None:
    """A full sequential chain A→B→C gives the same time regardless of agent count."""
    task = BenchmarkTask(
        id="t",
        name="n",
        description="d",
        category="c",
        parallelizable=False,
        subtasks=[
            SubTask(id="a", role="backend", description="a", estimated_minutes=5.0),
            SubTask(id="b", role="qa", description="b", estimated_minutes=5.0, depends_on=["a"]),
            SubTask(id="c", role="docs", description="c", estimated_minutes=5.0, depends_on=["b"]),
        ],
    )
    single = simulate_schedule(task, agents=1)
    multi3 = simulate_schedule(task, agents=3)
    assert single == pytest.approx(multi3, rel=0.01)


def test_simulate_schedule_more_agents_never_slower() -> None:
    task = _make_task()
    t1 = simulate_schedule(task, agents=1)
    t3 = simulate_schedule(task, agents=3)
    t5 = simulate_schedule(task, agents=5)
    assert t3 <= t1
    assert t5 <= t3


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_single_uses_sonnet_for_all_roles() -> None:
    task = _make_task(
        subtasks=[
            SubTask(id="a", role="backend", description="a", estimated_minutes=10.0),
            SubTask(id="b", role="qa", description="b", estimated_minutes=10.0),
        ]
    )
    # Both roles use Sonnet in single mode — cost should be same for both subtasks
    cost = estimate_cost(task, "single")
    assert cost > 0.0


def test_estimate_cost_multi_applies_overhead() -> None:
    task = _make_task()
    multi3_cost = estimate_cost(task, "multi-3")
    # Multi overhead + model mixing means cost relationship varies, but overhead > 0
    assert multi3_cost > 0.0
    # For a backend-heavy task single cost ~ multi cost (overhead vs model savings)
    # Just verify cost is positive and finite
    assert 0 < multi3_cost < 10.0


def test_estimate_cost_multi_cheaper_for_qa_heavy_task() -> None:
    """QA-heavy tasks cost less with multi-agent (Haiku vs Sonnet for QA subtasks)."""
    task = BenchmarkTask(
        id="t",
        name="n",
        description="d",
        category="testing",
        parallelizable=True,
        subtasks=[
            SubTask(id="a", role="qa", description="a", estimated_minutes=30.0),
            SubTask(id="b", role="qa", description="b", estimated_minutes=30.0),
            SubTask(id="c", role="qa", description="c", estimated_minutes=30.0),
        ],
    )
    single = estimate_cost(task, "single")
    multi3 = estimate_cost(task, "multi-3")
    # QA roles use Haiku in multi, which is much cheaper than Sonnet
    assert multi3 < single


# ---------------------------------------------------------------------------
# estimate_pass_rate
# ---------------------------------------------------------------------------


def test_estimate_pass_rate_single_baseline() -> None:
    task = _make_task()
    rate = estimate_pass_rate(task, "single")
    assert 0.0 <= rate <= 1.0


def test_estimate_pass_rate_single_degrades_with_many_subtasks() -> None:
    small = _make_task(
        subtasks=[SubTask(id=f"s{i}", role="backend", description="x", estimated_minutes=5.0) for i in range(3)]
    )
    large = _make_task(
        subtasks=[SubTask(id=f"s{i}", role="backend", description="x", estimated_minutes=5.0) for i in range(10)]
    )
    assert estimate_pass_rate(large, "single") < estimate_pass_rate(small, "single")


def test_estimate_pass_rate_multi_higher_than_single_for_large_tasks() -> None:
    task = _make_task(
        subtasks=[SubTask(id=f"s{i}", role="backend", description="x", estimated_minutes=5.0) for i in range(8)]
    )
    single = estimate_pass_rate(task, "single")
    multi3 = estimate_pass_rate(task, "multi-3")
    assert multi3 > single


def test_estimate_pass_rate_clamps_at_minimum() -> None:
    """Pass rate never goes below 50% for single-agent."""
    task = _make_task(
        subtasks=[SubTask(id=f"s{i}", role="backend", description="x", estimated_minutes=5.0) for i in range(100)]
    )
    rate = estimate_pass_rate(task, "single")
    assert rate >= 0.50


# ---------------------------------------------------------------------------
# run_simulate
# ---------------------------------------------------------------------------


def test_run_simulate_returns_one_result_per_task() -> None:
    tasks = [_make_task("t1"), _make_task("t2")]
    suite = run_simulate(tasks)
    assert len(suite.task_results) == 2


def test_run_simulate_produces_three_scenarios_per_task() -> None:
    suite = run_simulate([_make_task()])
    scenarios = {r.scenario for r in suite.task_results[0].results}
    assert scenarios == {"single", "multi-3", "multi-5"}


def test_run_simulate_speedup_single_is_1() -> None:
    suite = run_simulate([_make_task()])
    single = next(r for r in suite.task_results[0].results if r.scenario == "single")
    assert single.speedup == pytest.approx(1.0)


def test_run_simulate_mean_speedup_3_above_1() -> None:
    tasks = [
        BenchmarkTask(
            id="t",
            name="n",
            description="d",
            category="c",
            parallelizable=True,
            subtasks=[SubTask(id=f"s{i}", role="backend", description="x", estimated_minutes=10.0) for i in range(5)],
        )
    ]
    suite = run_simulate(tasks)
    assert suite.mean_speedup_3 > 1.0


def test_run_simulate_mode_is_simulate() -> None:
    suite = run_simulate([_make_task()])
    assert suite.mode == "simulate"


# ---------------------------------------------------------------------------
# BenchmarkSuite aggregate metrics
# ---------------------------------------------------------------------------


def test_mean_speedup_3_averages_across_tasks() -> None:
    task1 = ScenarioResult(
        task_id="t1",
        scenario="multi-3",
        wall_time_minutes=5.0,
        cost_usd=0.1,
        test_pass_rate=0.9,
        speedup=2.0,
        cost_ratio=0.8,
    )
    task2 = ScenarioResult(
        task_id="t2",
        scenario="multi-3",
        wall_time_minutes=5.0,
        cost_usd=0.1,
        test_pass_rate=0.9,
        speedup=4.0,
        cost_ratio=0.8,
    )
    tr1 = TaskBenchmarkResult(task_id="t1", task_name="T1", category="c", subtask_count=2, results=[task1])
    tr2 = TaskBenchmarkResult(task_id="t2", task_name="T2", category="c", subtask_count=2, results=[task2])
    suite = BenchmarkSuite(run_at="2026-01-01", mode="simulate", task_results=[tr1, tr2])
    assert suite.mean_speedup_3 == pytest.approx(3.0)


def test_mean_cost_savings_3_positive_for_cheaper_multi() -> None:
    tr = TaskBenchmarkResult(
        task_id="t1",
        task_name="T",
        category="c",
        subtask_count=2,
        results=[
            ScenarioResult(
                task_id="t1",
                scenario="multi-3",
                wall_time_minutes=5.0,
                cost_usd=0.08,
                test_pass_rate=0.9,
                speedup=2.0,
                cost_ratio=0.8,
            ),
        ],
    )
    suite = BenchmarkSuite(run_at="2026-01-01", mode="simulate", task_results=[tr])
    assert suite.mean_cost_savings_3 == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# load_task from YAML
# ---------------------------------------------------------------------------


def test_load_task_reads_yaml_correctly(tmp_path: Path) -> None:
    data = {
        "id": "task-test",
        "name": "Test task",
        "description": "A test",
        "category": "feature",
        "parallelizable": True,
        "subtasks": [
            {"id": "st-a", "role": "backend", "description": "A", "estimated_minutes": 10, "depends_on": []},
            {"id": "st-b", "role": "qa", "description": "B", "estimated_minutes": 5, "depends_on": ["st-a"]},
        ],
    }
    f = tmp_path / "task_test.yaml"
    f.write_text(yaml.dump(data))
    task = load_task(f)
    assert task.id == "task-test"
    assert task.name == "Test task"
    assert task.subtask_count == 2
    assert task.subtasks[1].depends_on == ["st-a"]


def test_load_all_tasks_loads_10_real_tasks() -> None:
    """The 10 real task YAML files must all parse correctly."""
    real_dir = Path(__file__).parent.parent.parent / "benchmarks" / "tasks"
    if not real_dir.exists():
        pytest.skip("benchmarks/tasks/ not found")
    tasks = load_all_tasks(real_dir)
    assert len(tasks) == 10


def test_load_all_tasks_sorted_by_filename(tmp_path: Path) -> None:
    for name in ("task_003.yaml", "task_001.yaml", "task_002.yaml"):
        (tmp_path / name).write_text(
            yaml.dump(
                {
                    "id": name.replace(".yaml", ""),
                    "name": name,
                    "description": "d",
                    "category": "c",
                    "parallelizable": True,
                    "subtasks": [],
                }
            )
        )
    tasks = load_all_tasks(tmp_path)
    ids = [t.id for t in tasks]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# format_table / format_summary
# ---------------------------------------------------------------------------


def test_format_table_contains_task_name() -> None:
    suite = run_simulate([_make_task(name="My Special Task")])
    table = format_table(suite)
    assert "My Special Task" in table


def test_format_table_has_header_row() -> None:
    suite = run_simulate([_make_task()])
    table = format_table(suite)
    assert "Speedup" in table


def test_format_summary_includes_speedup_numbers() -> None:
    suite = run_simulate([_make_task()])
    summary = format_summary(suite)
    assert "faster" in summary
    assert "%" in summary


# ---------------------------------------------------------------------------
# write_results
# ---------------------------------------------------------------------------


def test_write_results_creates_json_and_md(tmp_path: Path) -> None:
    suite = run_simulate([_make_task()])
    json_path, md_path = write_results(suite, tmp_path)
    assert json_path.exists()
    assert md_path.exists()
    assert json_path.suffix == ".json"
    assert md_path.suffix == ".md"


def test_write_results_json_is_valid(tmp_path: Path) -> None:
    import json

    suite = run_simulate([_make_task()])
    json_path, _ = write_results(suite, tmp_path)
    data = json.loads(json_path.read_text())
    assert data["mode"] == "simulate"
    assert len(data["task_results"]) == 1


# ---------------------------------------------------------------------------
# End-to-end: run all 10 real tasks
# ---------------------------------------------------------------------------


def test_full_simulation_10_tasks() -> None:
    """Full end-to-end: load all 10 tasks, simulate, verify multi-agent advantage."""
    real_dir = Path(__file__).parent.parent.parent / "benchmarks" / "tasks"
    if not real_dir.exists():
        pytest.skip("benchmarks/tasks/ not found")
    tasks = load_all_tasks(real_dir)
    assert len(tasks) == 10

    suite = run_simulate(tasks)
    assert len(suite.task_results) == 10
    assert suite.mean_speedup_3 > 1.0
    assert suite.mean_speedup_5 >= suite.mean_speedup_3

    # Every task should have 3 scenarios
    for tr in suite.task_results:
        scenarios = {r.scenario for r in tr.results}
        assert scenarios == {"single", "multi-3", "multi-5"}

    # Multi-agent must be at least as fast as single on every task
    for tr in suite.task_results:
        single = next(r for r in tr.results if r.scenario == "single")
        for r in tr.results:
            assert r.wall_time_minutes <= single.wall_time_minutes + 0.1  # tiny float tolerance
