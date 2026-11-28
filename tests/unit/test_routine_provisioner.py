"""Unit tests for the Direction A Routine provisioner (rt-003)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.planning.routine_provisioner import (
    RoutineProvisioner,
    build_env_vars,
    build_mcp_config,
    build_routine_prompt,
    recommend_triggers,
)
from bernstein.core.planning.scenario_library import (
    ScenarioLibrary,
    ScenarioRecipe,
    ScenarioTaskTemplate,
    load_scenario_library,
)

# Use the actual templates/scenarios directory shipped with the repo.
_SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "scenarios"


def _sample_recipe(
    *,
    scenario_id: str = "demo",
    name: str = "Demo",
    tags: tuple[str, ...] = ("review", "ci"),
    n_tasks: int = 2,
) -> ScenarioRecipe:
    tasks = tuple(
        ScenarioTaskTemplate(
            title=f"Task {i}",
            description=f"Do thing {i}",
            role="backend" if i % 2 == 0 else "qa",
            priority=1,
            scope="medium",
            complexity="medium",
        )
        for i in range(n_tasks)
    )
    return ScenarioRecipe(
        scenario_id=scenario_id,
        name=name,
        description="A test scenario",
        tags=tags,
        tasks=tasks,
    )


class TestRecommendTriggers:
    def test_review_tag_yields_github_trigger(self) -> None:
        recs = recommend_triggers(_sample_recipe(tags=("review",)))
        assert any(r.type == "github" and r.event == "pull_request.opened" for r in recs)

    def test_maintenance_tag_yields_schedule(self) -> None:
        recs = recommend_triggers(_sample_recipe(tags=("maintenance",)))
        assert any(r.type == "schedule" and r.cadence == "daily" for r in recs)

    def test_deploy_tag_yields_api(self) -> None:
        recs = recommend_triggers(_sample_recipe(tags=("deploy",)))
        assert any(r.type == "api" for r in recs)

    def test_no_known_tags_falls_back_to_api(self) -> None:
        recs = recommend_triggers(_sample_recipe(tags=("misc",)))
        assert len(recs) == 1
        assert recs[0].type == "api"

    def test_multiple_categories_emit_multiple_recs(self) -> None:
        recs = recommend_triggers(_sample_recipe(tags=("review", "deploy", "maintenance")))
        types = {r.type for r in recs}
        assert {"github", "schedule", "api"}.issubset(types)


class TestBuildRoutinePrompt:
    def test_prompt_contains_scenario_id(self) -> None:
        recipe = _sample_recipe(scenario_id="pr-review")
        prompt = build_routine_prompt(recipe, "http://x:8052")
        assert "pr-review" in prompt
        assert "bernstein_scenario" in prompt

    def test_prompt_contains_url(self) -> None:
        recipe = _sample_recipe()
        prompt = build_routine_prompt(recipe, "http://example.test:9000")
        assert "http://example.test:9000" in prompt

    def test_prompt_lists_tasks(self) -> None:
        recipe = _sample_recipe(n_tasks=3)
        prompt = build_routine_prompt(recipe, "http://x:8052")
        for i in range(3):
            assert f"Task {i}" in prompt

    def test_prompt_marks_dependent_scenarios_as_sequenced(self) -> None:
        recipe = ScenarioRecipe(
            scenario_id="seq",
            name="Sequenced",
            description="Ordered steps",
            tags=("ops",),
            tasks=(
                ScenarioTaskTemplate(id="first", title="First", description="d1", role="devops"),
                ScenarioTaskTemplate(id="second", title="Second", description="d2", role="qa", depends_on=("first",)),
            ),
        )
        prompt = build_routine_prompt(recipe, "http://x:8052")
        assert "respecting declared task dependencies" in prompt
        assert "parallel tasks" not in prompt
        assert "depends_on: `first`" in prompt


class TestBuildMcpConfig:
    def test_mcp_config_has_required_fields(self) -> None:
        cfg = build_mcp_config("http://example:8052")
        assert cfg["name"] == "bernstein"
        assert cfg["transport"] == "stdio"
        assert cfg["command"] == "bernstein"
        env = cfg["env"]
        assert isinstance(env, dict)
        assert env["BERNSTEIN_SERVER_URL"] == "http://example:8052"

    def test_mcp_config_serialises(self) -> None:
        cfg = build_mcp_config("http://x:8052")
        # Round-trip via JSON to confirm it is JSON-serialisable.
        round_tripped = json.loads(json.dumps(cfg))
        assert round_tripped == cfg


class TestBuildEnvVars:
    def test_default_keys_always_present(self) -> None:
        env = build_env_vars(_sample_recipe(tags=("misc",)))
        assert "BERNSTEIN_AUTH_TOKEN" in env
        assert "BERNSTEIN_SERVER_URL" in env

    def test_review_scenarios_request_github_token(self) -> None:
        env = build_env_vars(_sample_recipe(tags=("review",)))
        assert "GITHUB_TOKEN" in env

    def test_non_github_scenarios_skip_github_token(self) -> None:
        env = build_env_vars(_sample_recipe(tags=("misc",)))
        assert "GITHUB_TOKEN" not in env


class TestRoutineProvisioner:
    def _provisioner(self) -> RoutineProvisioner:
        recipe = _sample_recipe(scenario_id="t-1")
        library = ScenarioLibrary(scenarios={recipe.scenario_id: recipe})
        return RoutineProvisioner(library=library)

    def test_export_unknown_scenario_raises(self) -> None:
        prov = self._provisioner()
        try:
            prov.export_scenario_as_routine("nope", repo="o/r")
        except KeyError as exc:
            assert "nope" in str(exc)
        else:
            msg = "expected KeyError"
            raise AssertionError(msg)

    def test_export_returns_complete_bundle(self) -> None:
        prov = self._provisioner()
        export = prov.export_scenario_as_routine("t-1", repo="owner/repo")
        assert export.scenario_id == "t-1"
        assert export.name.startswith("Bernstein:")
        assert "owner/repo" in export.setup_instructions
        assert export.recommended_triggers
        assert export.mcp_config["name"] == "bernstein"

    def test_export_url_override(self) -> None:
        prov = self._provisioner()
        export = prov.export_scenario_as_routine(
            "t-1",
            repo="o/r",
            bernstein_url="http://other:1111",
        )
        assert "http://other:1111" in export.prompt
        env = export.mcp_config["env"]
        assert isinstance(env, dict)
        assert env["BERNSTEIN_SERVER_URL"] == "http://other:1111"

    def test_write_export_creates_files(self, tmp_path: Path) -> None:
        prov = self._provisioner()
        export = prov.export_scenario_as_routine("t-1", repo="o/r")
        files = prov.write_export(export, tmp_path / "out")
        names = sorted(p.name for p in files)
        assert names == [
            "env.json",
            "mcp-config.json",
            "prompt.md",
            "setup-guide.md",
            "triggers.md",
        ]
        for path in files:
            assert path.exists()
            assert path.stat().st_size > 0

    def test_list_scenarios_uses_library(self) -> None:
        prov = self._provisioner()
        listed = prov.list_scenarios()
        assert len(listed) == 1
        assert listed[0].scenario_id == "t-1"


class TestBundledTemplatesParse:
    """All scenario templates shipped under templates/scenarios/ must parse."""

    def test_at_least_eight_templates(self) -> None:
        library = load_scenario_library(_SCENARIOS_DIR)
        assert len(library.scenarios) >= 8

    def test_each_template_has_tasks(self) -> None:
        library = load_scenario_library(_SCENARIOS_DIR)
        for sid, recipe in library.scenarios.items():
            assert recipe.tasks, f"scenario {sid} has no tasks"

    def test_each_template_exports_cleanly(self) -> None:
        library = load_scenario_library(_SCENARIOS_DIR)
        prov = RoutineProvisioner(library=library)
        for sid in library.scenarios:
            export = prov.export_scenario_as_routine(sid, repo="o/r")
            assert export.prompt
            assert export.setup_instructions
            assert export.recommended_triggers
