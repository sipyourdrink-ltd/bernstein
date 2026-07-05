"""Tests for the A/B runner primitive.

These tests use synthetic deterministic fixtures only - no LLM calls,
no httpx, no live tasks. The contract under test is:

  * ``run_ab`` produces a stable :class:`Comparison` for fixed inputs.
  * ``Comparison.to_json`` is byte-stable (sort_keys=True).
  * Built-in scorers / executors compose correctly.
  * Winner logic respects the tolerance band.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.eval.ab_runner import (
    VARIANT_ADDENDUM_KEY,
    VARIANT_PROFILE_KEY,
    ArmCost,
    Comparison,
    RunResult,
    Task,
    Variant,
    VariantStats,
    echo_executor,
    exact_match_scorer,
    load_tasks_yaml,
    load_variant_yaml,
    run_ab,
    spawn_executor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tasks() -> list[Task]:
    return [
        Task(task_id="t1", input="hello", expected="A::hello"),
        Task(task_id="t2", input="world", expected="A::world"),
        Task(task_id="t3", input="foo", expected="B::foo"),
    ]


@pytest.fixture
def variant_a() -> Variant:
    return Variant(name="A", prompt="A")


@pytest.fixture
def variant_b() -> Variant:
    return Variant(name="B", prompt="B")


# ---------------------------------------------------------------------------
# Echo executor + scorer composition
# ---------------------------------------------------------------------------


def test_echo_executor_is_deterministic() -> None:
    """Echo executor must produce stable output for a fixed (variant, task)."""
    v = Variant(name="A", prompt="hello")
    t = Task(task_id="t1", input="world")

    r1 = echo_executor(v, t)
    r2 = echo_executor(v, t)

    assert r1 == r2
    assert r1.output == "hello::world"
    assert r1.variant == "A"
    assert r1.passed is False  # baseline executor leaves scoring to the scorer


def test_exact_match_scorer_marks_passed() -> None:
    """The exact-match scorer flips ``passed`` based on string equality."""
    v = Variant(name="A", prompt="A")
    t = Task(task_id="t1", input="x", expected="A::x")

    score, passed = exact_match_scorer(v, t, "A::x")
    assert (score, passed) == (1.0, True)

    score, passed = exact_match_scorer(v, t, "different")
    assert (score, passed) == (0.0, False)


# ---------------------------------------------------------------------------
# run_ab - deterministic comparison
# ---------------------------------------------------------------------------


def test_run_ab_deterministic_with_echo_and_exact_match(
    variant_a: Variant,
    variant_b: Variant,
) -> None:
    """A vs B over fixed tasks must produce the same Comparison every run.

    With echo_executor + exact_match_scorer:
      * A passes t1, t2 (expected starts with ``A::``) → 2/3
      * B passes t3 (expected ``B::foo``) → 1/3
      * Winner: A (pass-rate gap 33.3% > tolerance 5%).
    """
    cmp1 = run_ab(variant_a, variant_b, _tasks(), scorer=exact_match_scorer)
    cmp2 = run_ab(variant_a, variant_b, _tasks(), scorer=exact_match_scorer)

    assert cmp1 == cmp2  # frozen dataclasses → structural equality

    assert cmp1.variant_a == VariantStats(
        name="A", n=3, pass_count=2, pass_rate=2 / 3, mean_score=2 / 3, mean_duration_ms=0.0
    )
    assert cmp1.variant_b == VariantStats(
        name="B", n=3, pass_count=1, pass_rate=1 / 3, mean_score=1 / 3, mean_duration_ms=0.0
    )

    assert cmp1.winner == "a"
    assert "pass_rate" in cmp1.reason


def test_run_ab_per_task_deltas_in_input_order(
    variant_a: Variant,
    variant_b: Variant,
) -> None:
    """Per-task deltas preserve task input order and compute B - A."""
    cmp = run_ab(variant_a, variant_b, _tasks(), scorer=exact_match_scorer)

    ids = [d.task_id for d in cmp.per_task]
    assert ids == ["t1", "t2", "t3"]

    # t1: A=1.0, B=0.0 → delta = -1.0
    assert cmp.per_task[0].score_a == 1.0
    assert cmp.per_task[0].score_b == 0.0
    assert cmp.per_task[0].delta == -1.0

    # t3: A=0.0, B=1.0 → delta = +1.0
    assert cmp.per_task[2].delta == 1.0


def test_run_ab_tie_within_tolerance() -> None:
    """If both variants score identically, the winner must be 'tie'."""
    a = Variant(name="A", prompt="A")
    b = Variant(name="B", prompt="A")  # B uses the same prompt body
    tasks = [Task(task_id="t1", input="x", expected="A::x")]

    cmp = run_ab(a, b, tasks, scorer=exact_match_scorer)

    assert cmp.winner == "tie"
    assert cmp.variant_a.pass_rate == cmp.variant_b.pass_rate == 1.0


def test_run_ab_rejects_duplicate_variant_names() -> None:
    """Same name on both sides would make deltas ambiguous; raise."""
    v = Variant(name="same", prompt="x")
    with pytest.raises(ValueError, match="variant names must differ"):
        run_ab(v, v, [Task(task_id="t1", input="x")])


def test_run_ab_handles_empty_task_list(variant_a: Variant, variant_b: Variant) -> None:
    """Empty task set yields zero stats and a tie verdict."""
    cmp = run_ab(variant_a, variant_b, [])

    assert cmp.variant_a.n == 0
    assert cmp.variant_b.n == 0
    assert cmp.winner == "tie"
    assert cmp.per_task == ()


# ---------------------------------------------------------------------------
# Custom executor path
# ---------------------------------------------------------------------------


def test_run_ab_with_custom_executor(variant_a: Variant, variant_b: Variant) -> None:
    """A custom executor that pre-scores must flow through unchanged."""

    def biased_executor(variant: Variant, task: Task) -> RunResult:
        return RunResult(
            variant=variant.name,
            task_id=task.task_id,
            output=variant.name,
            score=1.0 if variant.name == "B" else 0.0,
            duration_ms=10.0,
            passed=variant.name == "B",
        )

    tasks = [Task(task_id=f"t{i}", input=i) for i in range(4)]
    cmp = run_ab(variant_a, variant_b, tasks, executor=biased_executor)

    assert cmp.variant_a.pass_rate == 0.0
    assert cmp.variant_b.pass_rate == 1.0
    assert cmp.winner == "b"
    assert cmp.variant_a.mean_duration_ms == 10.0


# ---------------------------------------------------------------------------
# JSON serialisation / round-trip stability
# ---------------------------------------------------------------------------


def test_comparison_to_json_is_byte_stable(variant_a: Variant, variant_b: Variant) -> None:
    """Two identical runs must yield byte-identical JSON output."""
    cmp1 = run_ab(variant_a, variant_b, _tasks(), scorer=exact_match_scorer)
    cmp2 = run_ab(variant_a, variant_b, _tasks(), scorer=exact_match_scorer)

    json1 = cmp1.to_json()
    json2 = cmp2.to_json()
    assert json1 == json2

    # And the JSON must be parseable back to a dict matching to_dict()
    parsed = json.loads(json1)
    assert parsed == cmp1.to_dict()
    # Sort_keys → top-level keys are alphabetical
    top_level_keys = list(parsed.keys())
    assert top_level_keys == sorted(top_level_keys)


def test_comparison_dict_shape() -> None:
    """``to_dict`` exposes a stable schema for downstream consumers."""
    cmp = Comparison(
        variant_a=VariantStats(name="A", n=1, pass_count=1, pass_rate=1.0, mean_score=1.0, mean_duration_ms=0.0),
        variant_b=VariantStats(name="B", n=1, pass_count=0, pass_rate=0.0, mean_score=0.0, mean_duration_ms=0.0),
        per_task=(),
        winner="a",
        reason="A pass_rate 100.00% beat B 0.00%",
    )
    expected_keys = {"variant_a", "variant_b", "per_task", "winner", "reason"}
    assert set(cmp.to_dict().keys()) == expected_keys


def test_comparison_with_costs_serialises_ledger_refs() -> None:
    """Cost fields appear in the dict only when attached, with refs intact."""
    cost_a = ArmCost(arm="A", entries=2, input_tokens=100, output_tokens=50, cost_usd=0.12, ledger_refs=("x1", "x2"))
    cost_b = ArmCost(arm="B", entries=1, input_tokens=40, output_tokens=20, cost_usd=0.05, ledger_refs=("y1",))
    cmp = Comparison(
        variant_a=VariantStats(name="A", n=1, pass_count=1, pass_rate=1.0, mean_score=1.0, mean_duration_ms=0.0),
        variant_b=VariantStats(name="B", n=1, pass_count=0, pass_rate=0.0, mean_score=0.0, mean_duration_ms=0.0),
        per_task=(),
        winner="a",
        reason="quality",
        cost_a=cost_a,
        cost_b=cost_b,
    )
    payload = cmp.to_dict()
    assert payload["cost_a"]["ledger_refs"] == ["x1", "x2"]
    assert payload["cost_b"]["cost_usd"] == 0.05
    # JSON round-trip stays byte-stable.
    assert cmp.to_json() == cmp.to_json()


# ---------------------------------------------------------------------------
# spawn_executor - real path through the task server, mocked transport
# ---------------------------------------------------------------------------


def _mock_server(created: list[dict], status: str = "done", quality_passed: bool = True):
    """Return an httpx.MockTransport simulating the task server."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            payload = json.loads(request.content.decode("utf-8"))
            created.append(payload)
            return httpx.Response(200, json={"id": f"srv-{len(created)}"})
        if request.method == "GET" and request.url.path.startswith("/tasks/"):
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "status": status,
                    "metadata": {"quality_passed": quality_passed},
                },
            )
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def test_spawn_executor_dispatches_through_task_server() -> None:
    """The real executor creates one server task per run and joins the
    ledger via the server-assigned task id."""
    created: list[dict] = []
    executor = spawn_executor(
        "http://server",
        role="qa",
        scope="small",
        timeout_seconds=5,
        poll_interval_seconds=0.0,
        transport=_mock_server(created),
    )
    variant = Variant(name="candidate", prompt="", metadata={VARIANT_PROFILE_KEY: "terse"})
    result = executor(variant, Task(task_id="t1", input="fix the bug"))

    assert result.passed is True
    assert result.score == 1.0
    assert result.ledger_task_id == "srv-1"
    payload = created[0]
    assert payload["role"] == "qa"
    assert payload["metadata"]["mode"] == "terse"
    assert payload["metadata"]["eval_arm"] == "candidate"
    assert payload["description"] == "fix the bug"


def test_spawn_executor_appends_control_addendum_to_description() -> None:
    """The control arm's addendum rides the description, not the profile."""
    created: list[dict] = []
    executor = spawn_executor(
        "http://server",
        timeout_seconds=5,
        poll_interval_seconds=0.0,
        transport=_mock_server(created),
    )
    variant = Variant(name="control", prompt="", metadata={VARIANT_ADDENDUM_KEY: "Keep it brief."})
    executor(variant, Task(task_id="t1", input="fix the bug"))

    payload = created[0]
    assert payload["description"] == "fix the bug\n\nKeep it brief."
    assert "mode" not in payload["metadata"]


def test_spawn_executor_failed_task_scores_zero() -> None:
    created: list[dict] = []
    executor = spawn_executor(
        "http://server",
        timeout_seconds=5,
        poll_interval_seconds=0.0,
        transport=_mock_server(created, status="failed", quality_passed=False),
    )
    result = executor(Variant(name="baseline", prompt=""), Task(task_id="t1", input="x"))
    assert result.passed is False
    assert result.score == 0.0
    assert result.ledger_task_id == "srv-1"


# ---------------------------------------------------------------------------
# YAML loaders
# ---------------------------------------------------------------------------


def test_load_variant_yaml_round_trip(tmp_path: Path) -> None:
    """Round-trip a Variant via YAML."""
    p = tmp_path / "variant.yaml"
    p.write_text(
        "name: my-variant\nprompt: |\n  hello world\nmodel: haiku\nmetadata:\n  source: test\n",
        encoding="utf-8",
    )
    v = load_variant_yaml(p)
    assert v.name == "my-variant"
    assert v.prompt.strip() == "hello world"
    assert v.model == "haiku"
    assert v.metadata == {"source": "test"}


def test_load_variant_yaml_missing_keys_raises(tmp_path: Path) -> None:
    """Missing required keys → ValueError, not KeyError."""
    p = tmp_path / "bad.yaml"
    p.write_text("name: only-name\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required keys"):
        load_variant_yaml(p)


def test_load_tasks_yaml_round_trip(tmp_path: Path) -> None:
    """Round-trip a task list via YAML, preserving order."""
    p = tmp_path / "tasks.yaml"
    p.write_text(
        "tasks:\n"
        "  - id: t1\n    input: hello\n    expected: A::hello\n"
        "  - id: t2\n    input: world\n    expected: A::world\n",
        encoding="utf-8",
    )
    tasks = load_tasks_yaml(p)
    assert [t.task_id for t in tasks] == ["t1", "t2"]
    assert tasks[0].expected == "A::hello"


def test_load_tasks_yaml_rejects_non_list(tmp_path: Path) -> None:
    """Top-level must be a 'tasks' list."""
    p = tmp_path / "bad.yaml"
    p.write_text("not_tasks: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level 'tasks"):
        load_tasks_yaml(p)
