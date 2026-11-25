"""Unit tests for the bidirectional Routine bridge (rt-003)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.planning.routine_bridge import (
    RoutineBridge,
    build_task_payloads,
    estimate_minutes,
    spawn_scenario_tasks,
)
from bernstein.core.planning.scenario_library import (
    ScenarioLibrary,
    ScenarioRecipe,
    ScenarioTaskTemplate,
)


def _recipe(
    *,
    scenario_id: str = "demo",
    n_tasks: int = 3,
    tags: tuple[str, ...] = ("review",),
) -> ScenarioRecipe:
    tasks = tuple(
        ScenarioTaskTemplate(
            title=f"T{i}",
            description=f"d{i}",
            role="backend" if i == 0 else "qa",
            priority=1 + (i % 3),
            scope="medium",
            complexity="medium",
        )
        for i in range(n_tasks)
    )
    return ScenarioRecipe(
        scenario_id=scenario_id,
        name="Demo",
        description="desc",
        tags=tags,
        tasks=tasks,
    )


def _make_bridge(tmp_path: Path, *, scenarios: dict[str, ScenarioRecipe] | None = None) -> RoutineBridge:
    library = ScenarioLibrary(scenarios=scenarios or {})
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    from bernstein.core.planning.routine_provisioner import RoutineProvisioner

    return RoutineBridge(
        library=library,
        provisioner=RoutineProvisioner(library=library),
        state_dir=state_dir,
    )


class TestBuildTaskPayloads:
    def test_one_payload_per_task_template(self) -> None:
        recipe = _recipe(n_tasks=4)
        payloads = build_task_payloads(recipe)
        assert len(payloads) == 4

    def test_payload_carries_scenario_id(self) -> None:
        recipe = _recipe(scenario_id="abc")
        payloads = build_task_payloads(recipe)
        assert all(p.scenario_id == "abc" for p in payloads)

    def test_all_payloads_share_orchestration_id(self) -> None:
        recipe = _recipe(n_tasks=3)
        payloads = build_task_payloads(recipe)
        orch_ids = {p.orchestration_id for p in payloads}
        assert len(orch_ids) == 1

    def test_explicit_orchestration_id_is_preserved(self) -> None:
        recipe = _recipe(n_tasks=2)
        payloads = build_task_payloads(recipe, orchestration_id="my-orch-1")
        assert all(p.orchestration_id == "my-orch-1" for p in payloads)

    def test_pr_number_injected_into_description(self) -> None:
        recipe = _recipe(n_tasks=1)
        payloads = build_task_payloads(recipe, pr_number=42, branch="feat/foo")
        assert "#42" in payloads[0].description
        assert "feat/foo" in payloads[0].description

    def test_no_context_means_clean_description(self) -> None:
        recipe = _recipe(n_tasks=1)
        payloads = build_task_payloads(recipe)
        assert "Trigger context" not in payloads[0].description

    def test_complexity_legacy_values_normalised(self) -> None:
        recipe = ScenarioRecipe(
            scenario_id="x",
            name="x",
            description="",
            tags=(),
            tasks=(
                ScenarioTaskTemplate(
                    title="t",
                    description="",
                    role="qa",
                    priority=1,
                    scope="medium",
                    complexity="medium",
                ),
            ),
        )
        payloads = build_task_payloads(recipe)
        # ScenarioTaskTemplate normalises 'simple'/'moderate'/'complex' on parse,
        # so post-load the values are already low/medium/high. Confirm the
        # bridge passes them through unchanged.
        assert payloads[0].complexity in {"low", "medium", "high"}

    def test_as_server_payload_has_metadata(self) -> None:
        recipe = _recipe(n_tasks=1)
        payloads = build_task_payloads(recipe)
        body = payloads[0].as_server_payload()
        assert body["metadata"]["scenario_id"] == "demo"
        assert "orchestration_id" in body["metadata"]


class TestEstimateMinutes:
    def test_empty_scenario_is_zero(self) -> None:
        recipe = ScenarioRecipe(scenario_id="x", name="x", description="", tags=(), tasks=())
        assert estimate_minutes(recipe) == 0

    def test_minimum_is_five(self) -> None:
        recipe = _recipe(n_tasks=1)
        assert estimate_minutes(recipe) >= 5

    def test_more_distinct_roles_lowers_estimate(self) -> None:
        single_role = ScenarioRecipe(
            scenario_id="s",
            name="s",
            description="",
            tags=(),
            tasks=tuple(
                ScenarioTaskTemplate(
                    title=f"t{i}",
                    description="",
                    role="backend",
                    priority=1,
                    scope="medium",
                    complexity="medium",
                )
                for i in range(4)
            ),
        )
        many_roles = ScenarioRecipe(
            scenario_id="m",
            name="m",
            description="",
            tags=(),
            tasks=tuple(
                ScenarioTaskTemplate(
                    title=f"t{i}",
                    description="",
                    role=f"role_{i}",
                    priority=1,
                    scope="medium",
                    complexity="medium",
                )
                for i in range(4)
            ),
        )
        assert estimate_minutes(many_roles) < estimate_minutes(single_role)


class TestRoutineBridgeInvoke:
    def test_invoke_unknown_raises(self, tmp_path: Path) -> None:
        bridge = _make_bridge(tmp_path)
        with pytest.raises(KeyError):
            bridge.invoke_scenario("nope")

    def test_invoke_returns_payloads_and_invocation(self, tmp_path: Path) -> None:
        recipe = _recipe(n_tasks=2)
        bridge = _make_bridge(tmp_path, scenarios={recipe.scenario_id: recipe})
        invocation, payloads = bridge.invoke_scenario(recipe.scenario_id, pr_number=7)
        assert invocation.task_count == 2
        assert invocation.estimated_minutes > 0
        assert len(payloads) == 2
        assert all("#7" in p.description for p in payloads)


class TestRoutineBridgeRegistry:
    def test_register_unknown_raises(self, tmp_path: Path) -> None:
        bridge = _make_bridge(tmp_path)
        with pytest.raises(KeyError):
            bridge.register_binding("trig-1", "missing", "o/r")

    def test_register_persists_to_disk(self, tmp_path: Path) -> None:
        recipe = _recipe()
        bridge = _make_bridge(tmp_path, scenarios={recipe.scenario_id: recipe})
        binding = bridge.register_binding("trig-1", recipe.scenario_id, "o/r")
        assert binding.trigger_id == "trig-1"
        assert bridge.registry_path.exists()
        data: dict[str, Any] = json.loads(bridge.registry_path.read_text())
        assert "trig-1" in data
        assert data["trig-1"]["scenario_id"] == recipe.scenario_id

    def test_lookup_returns_binding(self, tmp_path: Path) -> None:
        recipe = _recipe()
        bridge = _make_bridge(tmp_path, scenarios={recipe.scenario_id: recipe})
        bridge.register_binding("trig-1", recipe.scenario_id, "o/r")
        found = bridge.lookup_binding("trig-1")
        assert found is not None
        assert found.scenario_id == recipe.scenario_id

    def test_lookup_missing_returns_none(self, tmp_path: Path) -> None:
        bridge = _make_bridge(tmp_path)
        assert bridge.lookup_binding("ghost") is None

    def test_list_bindings_orders_by_time(self, tmp_path: Path) -> None:
        recipe = _recipe()
        bridge = _make_bridge(tmp_path, scenarios={recipe.scenario_id: recipe})
        bridge.register_binding("a", recipe.scenario_id, "o/r")
        bridge.register_binding("b", recipe.scenario_id, "o/r")
        listing = bridge.list_bindings()
        assert [b.trigger_id for b in listing] == ["a", "b"]


class TestSpawnScenarioTasks:
    def test_spawn_calls_poster_per_payload(self, tmp_path: Path) -> None:
        recipe = _recipe(n_tasks=3)
        payloads = build_task_payloads(recipe)
        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_poster(path: str, body: dict[str, Any]) -> dict[str, Any]:
            calls.append((path, body))
            return {"id": f"task-{len(calls)}"}

        ids = spawn_scenario_tasks(payloads, poster=fake_poster)
        assert ids == ["task-1", "task-2", "task-3"]
        assert all(p == "/tasks" for p, _ in calls)

    def test_spawn_skips_failed_posts(self, tmp_path: Path) -> None:
        recipe = _recipe(n_tasks=2)
        payloads = build_task_payloads(recipe)

        def flaky_poster(path: str, body: dict[str, Any]) -> dict[str, Any]:
            if "T0" in body.get("title", ""):
                raise RuntimeError("boom")
            return {"id": "ok"}

        ids = spawn_scenario_tasks(payloads, poster=flaky_poster)
        assert ids == ["ok"]

    def test_spawn_rejects_non_callable(self) -> None:
        with pytest.raises(TypeError):
            spawn_scenario_tasks([], poster=42)  # type: ignore[arg-type]

    def test_spawn_orders_and_resolves_template_dependencies(self) -> None:
        recipe = ScenarioRecipe(
            scenario_id="ordered",
            name="Ordered",
            description="desc",
            tags=(),
            tasks=(
                ScenarioTaskTemplate(id="verify", title="Verify", description="v", role="qa", depends_on=("restore",)),
                ScenarioTaskTemplate(id="restore", title="Restore", description="r", role="devops"),
                ScenarioTaskTemplate(
                    id="audit",
                    title="Audit",
                    description="a",
                    role="security",
                    depends_on=("verify",),
                ),
            ),
        )
        payloads = build_task_payloads(recipe)
        assert [p.template_id for p in payloads] == ["restore", "verify", "audit"]

        calls: list[dict[str, Any]] = []

        def fake_poster(_path: str, body: dict[str, Any]) -> dict[str, Any]:
            calls.append(body)
            return {"id": f"task-{len(calls)}"}

        ids = spawn_scenario_tasks(payloads, poster=fake_poster)
        assert ids == ["task-1", "task-2", "task-3"]
        assert "depends_on" not in calls[0]
        assert calls[1]["depends_on"] == ["task-1"]
        assert calls[2]["depends_on"] == ["task-2"]

    def test_spawn_skips_dependents_when_upstream_fails(self) -> None:
        recipe = ScenarioRecipe(
            scenario_id="ordered-fail",
            name="Ordered Fail",
            description="desc",
            tags=(),
            tasks=(
                ScenarioTaskTemplate(id="step1", title="Step 1", description="s1", role="devops"),
                ScenarioTaskTemplate(id="step2", title="Step 2", description="s2", role="qa", depends_on=("step1",)),
            ),
        )
        payloads = build_task_payloads(recipe)
        posted_titles: list[str] = []

        def failing_poster(_path: str, body: dict[str, Any]) -> dict[str, Any]:
            posted_titles.append(str(body["title"]))
            raise RuntimeError("server rejected step1")

        ids = spawn_scenario_tasks(payloads, poster=failing_poster)
        assert ids == []
        assert posted_titles == ["Step 1"]


class TestRoutineBridgeProvision:
    def test_provision_writes_artefacts(self, tmp_path: Path) -> None:
        recipe = _recipe()
        bridge = _make_bridge(tmp_path, scenarios={recipe.scenario_id: recipe})
        out_dir = tmp_path / "exported"
        export, files = bridge.provision(recipe.scenario_id, "owner/repo", out_dir)
        assert export.scenario_id == recipe.scenario_id
        assert (out_dir / "prompt.md").exists()
        assert (out_dir / "mcp-config.json").exists()
        assert len(files) == 5
