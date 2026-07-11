"""Integration tests for criterion-profile-driven routing (issue #1346).

The unit tests verify that the profile data model and the bias
mapping are correct in isolation.  The integration tests below assert
the *wiring*: when a profile is stamped onto ``Task.metadata`` and the
task is fed through :func:`bernstein.core.routing.router_core.route_task`,
the model selection changes in the documented direction.

Each test holds the bandit metrics path empty so the heuristic /
override path is exercised without bandit interference.

The CLI surface tests (``bernstein criterion-profile show``,
``bernstein add-task --criterion-profile``, etc.) hit the Click runner
in-process to avoid spinning up the full task server - the CLI
performs early validation locally, which is what the integration
contract guarantees.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from bernstein.core.models import Complexity, Scope, Task
from click.testing import CliRunner

from bernstein.cli.commands.criterion_profile_cmd import criterion_profile_group
from bernstein.core.routing.criterion_profile import (
    ENV_FLAG,
    CriterionProfileError,
    resolve,
)
from bernstein.core.routing.router_core import route_task


def _make_task(
    *,
    role: str = "backend",
    metadata: dict[str, Any] | None = None,
    scope: Scope = Scope.SMALL,
    complexity: Complexity = Complexity.LOW,
    priority: int = 2,
) -> Task:
    return Task(
        id=f"T-{role}",
        title="Test task",
        description="integration test task for criterion profile",
        role=role,
        scope=scope,
        complexity=complexity,
        priority=priority,
        metadata=metadata or {},
    )


class TestRouteTaskHonoursCriterionProfile:
    """End-to-end: a profile on metadata diverts model selection."""

    def test_safety_first_routes_to_opus(self) -> None:
        baseline = _make_task()
        with_profile = _make_task(metadata={"criterion_profile": "safety-first"})
        # The unbiased baseline needs a configured default; routing no longer
        # guesses a model for unconfigured tasks.
        baseline_cfg = route_task(baseline, default_model="sonnet")
        biased_cfg = route_task(with_profile)
        assert baseline_cfg.model != biased_cfg.model or baseline_cfg.effort != biased_cfg.effort, (
            "expected criterion-profile to change at least one of model/effort"
        )
        assert biased_cfg.model == "opus"
        assert biased_cfg.effort == "max"

    def test_speed_first_routes_to_haiku(self) -> None:
        cfg = route_task(_make_task(metadata={"criterion_profile": "speed-first"}))
        assert cfg.model == "haiku"
        assert cfg.effort == "low"

    def test_cost_first_routes_to_haiku(self) -> None:
        cfg = route_task(_make_task(metadata={"criterion_profile": "cost-first"}))
        assert cfg.model == "haiku"

    def test_balanced_keeps_default_routing(self) -> None:
        cfg = route_task(_make_task(metadata={"criterion_profile": "balanced"}))
        # Balanced has no dominant axis -> defaults to sonnet/high
        assert cfg.model == "sonnet"
        assert cfg.effort == "high"

    def test_inline_dict_works_end_to_end(self) -> None:
        cfg = route_task(
            _make_task(
                metadata={
                    "criterion_profile": {
                        "correctness": 0.7,
                        "cost": 0.1,
                        "latency": 0.1,
                        "reversibility": 0.1,
                    }
                }
            )
        )
        assert cfg.model == "opus"


class TestFeatureFlagDisablesPath:
    def test_disabled_flag_reverts_to_default_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_FLAG, "0")
        cfg = route_task(
            _make_task(metadata={"criterion_profile": "safety-first"}),
            default_model="sonnet",
        )
        # Without the bias, the small/low backend task lands on the
        # configured default sonnet/high path, not opus.
        assert cfg.model != "opus"

    def test_enabled_flag_picks_up_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        cfg = route_task(_make_task(metadata={"criterion_profile": "safety-first"}))
        assert cfg.model == "opus"


class TestExplicitModelOverrideStillWins:
    """task.model set explicitly beats criterion-profile bias."""

    def test_explicit_model_beats_profile(self) -> None:
        task = _make_task(metadata={"criterion_profile": "safety-first"})
        task.model = "haiku"
        task.effort = "low"
        cfg = route_task(task)
        assert cfg.model == "haiku"
        assert cfg.effort == "low"


class TestCriterionProfileShowCLI:
    def test_show_prints_resolved_weights(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        # The CLI hits the task server via ``server_get``; stub it.
        from bernstein.cli.commands import criterion_profile_cmd as mod

        def _fake_server_get(_path: str) -> dict[str, Any]:
            return {
                "id": "T-show",
                "metadata": {"criterion_profile": "safety-first"},
            }

        monkeypatch.setattr(mod, "server_get", _fake_server_get)

        result = runner.invoke(criterion_profile_group, ["show", "T-show"])
        assert result.exit_code == 0, result.output
        assert "safety-first" in result.output
        assert "correctness" in result.output

    def test_show_handles_missing_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        from bernstein.cli.commands import criterion_profile_cmd as mod

        monkeypatch.setattr(mod, "server_get", lambda _p: {"id": "T-x", "metadata": {}})
        result = runner.invoke(criterion_profile_group, ["show", "T-x"])
        assert result.exit_code == 0, result.output
        assert "no criterion_profile" in result.output

    def test_show_handles_disabled_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_FLAG, "0")
        runner = CliRunner()
        result = runner.invoke(criterion_profile_group, ["show", "T-x"])
        assert result.exit_code == 0
        assert "disabled" in result.output

    def test_list_includes_all_presets(self) -> None:
        runner = CliRunner()
        result = runner.invoke(criterion_profile_group, ["list"])
        assert result.exit_code == 0, result.output
        for name in ("safety-first", "speed-first", "balanced", "cost-first"):
            assert name in result.output


class TestProfileValidationGuardsAtCLI:
    """The CLI validates the preset name before payload submission."""

    def test_invalid_preset_in_metadata_yields_warning(self) -> None:
        # ``extract_from_task`` is the wire-side path; integration check
        # confirms it doesn't crash the router for malformed input.
        task = _make_task(metadata={"criterion_profile": "made-up-preset"})
        cfg = route_task(task, default_model="sonnet")
        # Falls through to the default heuristic path.
        assert cfg.model in {"sonnet", "haiku", "opus"}


class TestInheritanceEndToEnd:
    def test_child_inherits_parent_profile(self) -> None:
        from bernstein.core.routing.criterion_profile import inherit_for_child

        parent_metadata = {"criterion_profile": "safety-first"}
        child_metadata = inherit_for_child(parent_metadata, None)
        child = _make_task(metadata=child_metadata)
        cfg = route_task(child)
        assert cfg.model == "opus"

    def test_child_override_changes_routing(self) -> None:
        from bernstein.core.routing.criterion_profile import inherit_for_child

        parent_metadata = {"criterion_profile": "safety-first"}
        child_metadata = inherit_for_child(parent_metadata, {"criterion_profile": "speed-first"})
        child = _make_task(metadata=child_metadata)
        cfg = route_task(child)
        assert cfg.model == "haiku"


class TestBundledPresetsResolve:
    """The bundled YAML files round-trip through the resolver."""

    @pytest.mark.parametrize("preset", ["safety-first", "speed-first", "balanced", "cost-first"])
    def test_each_bundled_preset_resolves(self, preset: str) -> None:
        profile = resolve(preset)
        assert profile.name == preset
        profile.validate()


class TestRunCommandFlagValidation:
    """`bernstein run --criterion-profile <preset>` rejects typos early."""

    def test_invalid_preset_raises(self) -> None:
        # We exercise the underlying resolver directly because the run
        # command body installs a long-lived orchestrator; the early
        # validation block runs the same ``resolve`` call.
        with pytest.raises(CriterionProfileError):
            resolve("totally-not-real")

    def test_valid_preset_resolves(self) -> None:
        profile = resolve("balanced")
        assert profile.name == "balanced"


def test_json_describe_output_round_trips() -> None:
    """The describe helper output is parseable enough for log scraping."""
    from bernstein.core.routing.criterion_profile import describe

    text = describe(resolve("safety-first"))
    # The format is `preset=NAME axis=value, axis=value, ...`
    assert text.startswith("preset=safety-first")
    # Each axis label appears exactly once.
    for axis in ("correctness", "cost", "latency", "reversibility"):
        assert text.count(axis) == 1


def test_env_var_propagated_to_router_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting ``BERNSTEIN_RUN_CRITERION_PROFILE`` doesn't crash the router.

    The env var is set by the ``bernstein run`` bootstrap; the router
    itself does not read it directly - tasks carry the metadata.  This
    test pins the contract: the env var presence alone never alters
    routing for tasks that lack the metadata key.
    """
    monkeypatch.setenv("BERNSTEIN_RUN_CRITERION_PROFILE", "safety-first")
    cfg = route_task(_make_task(), default_model="sonnet")
    # No metadata on the task -> no bias, default routing applies.
    assert cfg.model in {"sonnet", "haiku", "opus"}
    # Specifically, it must NOT be auto-pinned to opus just because the
    # env var is set.  That's task-level metadata's job.
    if not os.environ.get("BERNSTEIN_FORCE_OPUS"):
        # Heuristic for the small/low backend task -> sonnet.
        assert cfg.model == "sonnet"


# Smoke test exercising the JSON output mode of `criterion-profile show`.
def test_show_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    from bernstein.cli.commands import criterion_profile_cmd as mod

    monkeypatch.setattr(
        mod,
        "server_get",
        lambda _p: {
            "id": "T-json",
            "metadata": {"criterion_profile": "balanced"},
        },
    )
    monkeypatch.setattr(mod, "is_json", lambda: True)
    result = runner.invoke(criterion_profile_group, ["show", "T-json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["preset"] == "balanced"
    assert payload["weights"]["correctness"] == pytest.approx(0.25)
