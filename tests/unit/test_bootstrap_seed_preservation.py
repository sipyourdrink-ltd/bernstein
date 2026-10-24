"""Regression tests: goal bootstrap preserves the full seed policy (#6642).

_bootstrap_from_goal_impl used to construct a minimal SeedConfig that carried
only goal/cli/model plus quality_gates and formal_verification; all other
policy fields (evolution_enabled, gate_repair_enabled, max_agents, etc.) were
silently reverted to their dataclass defaults.

These tests verify that when bernstein.yaml exists and is valid, its policy
fields survive into the SeedConfig the bootstrap builds.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.seed import SeedConfig

_BOOTSTRAP = "bernstein.core.orchestration.bootstrap"


def _write_seed(path: Path, **extra_yaml: str) -> None:
    """Write a minimal bernstein.yaml with a goal and optional extra policy fields."""
    lines = ["goal: 'existing goal'\n"]
    for key, val in extra_yaml.items():
        lines.append(f"{key}: {val}\n")
    path.write_text("".join(lines), encoding="utf-8")


def _run_seed_construction(
    workdir: Path,
    goal: str,
    cli: str = "auto",
    model: str | None = None,
) -> SeedConfig:
    """Run _bootstrap_from_goal_impl up to seed use and capture the seed.

    Patches enough of the startup pipeline so the function can proceed past
    seed construction and reach ``_goal_sync_and_plan``, where we intercept
    the seed and abort.
    """
    from bernstein.core.orchestration.bootstrap import _bootstrap_from_goal_impl

    captured: list[SeedConfig] = []

    def _capture(*, seed: object, **_: object) -> tuple[int, str | None, object]:
        captured.append(seed)  # type: ignore[arg-type]
        raise SystemExit(0)

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_BOOTSTRAP}._acquire_pid_lock"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._run_git_hygiene"))
        stack.enter_context(patch(f"{_BOOTSTRAP}.preflight_checks"))
        stack.enter_context(patch(f"{_BOOTSTRAP}.ensure_sdd", return_value=False))
        stack.enter_context(patch(f"{_BOOTSTRAP}._reconcile_dead_owner_before_runtime_cleanup"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._clean_stale_runtime"))
        stack.enter_context(patch(f"{_BOOTSTRAP}.check_adapter_configured"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._discover_catalog"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._build_codebase_index"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._register_ci_parsers"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._resolve_bind_host", return_value="127.0.0.1"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._resolve_auth_token", return_value=None))
        stack.enter_context(patch(f"{_BOOTSTRAP}._resolve_server_url", return_value="http://server"))
        stack.enter_context(patch(f"{_BOOTSTRAP}.supervised_server", return_value=111))
        stack.enter_context(patch(f"{_BOOTSTRAP}._wait_for_server", return_value=True))
        stack.enter_context(patch(f"{_BOOTSTRAP}._register_mcp_discovery"))
        stack.enter_context(patch(f"{_BOOTSTRAP}._goal_sync_and_plan", side_effect=_capture))
        stack.enter_context(patch(f"{_BOOTSTRAP}.console", MagicMock()))
        # Stub invariants which may not be importable in unit context
        fake_inv = MagicMock()
        fake_inv.verify_invariants.return_value = (True, [])
        fake_inv.write_lockfile = MagicMock()
        fake_mod = {"bernstein.evolution.invariants": fake_inv}
        import sys

        stack.enter_context(patch.dict(sys.modules, fake_mod))

        with pytest.raises(SystemExit):
            _bootstrap_from_goal_impl(goal=goal, workdir=workdir, cli=cli, model=model)

    assert captured, "_goal_sync_and_plan was never called — seed construction did not reach it"
    return captured[0]


class TestGoalBootstrapSeedPreservation:
    """Goal bootstrap preserves complete seed policy from bernstein.yaml (#6642)."""

    def test_evolution_enabled_false_preserved(self, tmp_path: Path) -> None:
        """evolution_enabled: false in bernstein.yaml survives into the inline-goal seed."""
        _write_seed(tmp_path / "bernstein.yaml", evolution_enabled="false")

        seed = _run_seed_construction(tmp_path, goal="add tests")

        assert seed.evolution_enabled is False, (
            f"evolution_enabled reverted to True (default) instead of being preserved; seed={seed}"
        )

    def test_gate_repair_enabled_false_preserved(self, tmp_path: Path) -> None:
        """gate_repair_enabled: false in bernstein.yaml survives into the inline-goal seed."""
        _write_seed(tmp_path / "bernstein.yaml", gate_repair_enabled="false")

        seed = _run_seed_construction(tmp_path, goal="fix the lint")

        assert seed.gate_repair_enabled is False, f"gate_repair_enabled reverted to True (default); seed={seed}"

    def test_max_agents_preserved(self, tmp_path: Path) -> None:
        """max_agents in bernstein.yaml survives into the inline-goal seed."""
        _write_seed(tmp_path / "bernstein.yaml", max_agents="2")

        seed = _run_seed_construction(tmp_path, goal="run tasks")

        assert seed.max_agents == 2, f"max_agents reverted to default 6; seed={seed}"

    def test_goal_always_overrides(self, tmp_path: Path) -> None:
        """The inline goal always replaces the goal from bernstein.yaml."""
        _write_seed(tmp_path / "bernstein.yaml")

        seed = _run_seed_construction(tmp_path, goal="new inline goal")

        assert seed.goal == "new inline goal", f"inline goal not applied; seed={seed}"

    def test_explicit_cli_override_wins(self, tmp_path: Path) -> None:
        """An explicit --cli flag overrides the cli from bernstein.yaml."""
        _write_seed(tmp_path / "bernstein.yaml")

        seed = _run_seed_construction(tmp_path, goal="tasks", cli="codex")

        assert seed.cli == "codex", f"explicit cli not applied; seed={seed}"

    def test_explicit_model_override_wins(self, tmp_path: Path) -> None:
        """An explicit --model flag overrides the model from bernstein.yaml."""
        _write_seed(tmp_path / "bernstein.yaml")

        seed = _run_seed_construction(tmp_path, goal="tasks", model="opus")

        assert seed.model == "opus", f"explicit model not applied; seed={seed}"

    def test_no_seed_file_uses_minimal_config(self, tmp_path: Path) -> None:
        """When no bernstein.yaml exists, a minimal SeedConfig is built from CLI args."""
        # No bernstein.yaml in tmp_path.
        seed = _run_seed_construction(tmp_path, goal="fresh start")

        assert seed.goal == "fresh start"
        assert seed.evolution_enabled is True  # dataclass default
        assert seed.gate_repair_enabled is True  # dataclass default
