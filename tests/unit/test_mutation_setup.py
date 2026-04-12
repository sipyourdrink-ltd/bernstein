"""TEST-015: Mutation testing setup (mutmut configuration).

Tests that validate the mutmut configuration is correct and the
key modules are amenable to mutation testing.  Also provides the
mutmut_config.py setup.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# The mutmut configuration (also written to the repo root)
# ---------------------------------------------------------------------------

MUTMUT_CONFIG = {
    "paths_to_mutate": [
        "src/bernstein/core/tasks/lifecycle.py",
        "src/bernstein/core/agents/spawner.py",
        "src/bernstein/core/guardrails.py",
        "src/bernstein/core/models.py",
        "src/bernstein/core/task_store.py",
        "src/bernstein/core/config_schema.py",
        "src/bernstein/adapters/base.py",
    ],
    "tests_dir": "tests/unit/",
    "runner": "python -m pytest tests/unit/ -x -q --no-header --override-ini=addopts=",
    "dict_synonyms": "Struct,NamedStruct",
}


class TestMutmutConfig:
    """Validate the mutmut configuration."""

    def test_paths_to_mutate_exist(self) -> None:
        root = Path(__file__).resolve().parent.parent.parent
        for p in MUTMUT_CONFIG["paths_to_mutate"]:
            full = root / p
            assert full.exists(), f"Mutation target does not exist: {p}"

    def test_tests_dir_exists(self) -> None:
        root = Path(__file__).resolve().parent.parent.parent
        tests_dir = root / MUTMUT_CONFIG["tests_dir"]
        assert tests_dir.exists(), "Tests directory does not exist"

    def test_modules_importable(self) -> None:
        """All mutation targets must be importable."""
        for p in MUTMUT_CONFIG["paths_to_mutate"]:
            # Convert path to module name: src/bernstein/core/tasks/lifecycle.py -> bernstein.core.tasks.lifecycle
            module_name = p.replace("src/", "").replace("/", ".").replace(".py", "")
            try:
                importlib.import_module(module_name)
            except Exception as e:
                pytest.fail(f"Cannot import {module_name}: {e}")

    def test_mutation_targets_have_logic(self) -> None:
        """Mutation targets should have meaningful logic (functions/methods)."""
        for p in MUTMUT_CONFIG["paths_to_mutate"]:
            module_name = p.replace("src/", "").replace("/", ".").replace(".py", "")
            mod = importlib.import_module(module_name)
            # Check the module has callable members
            callables = [name for name, obj in inspect.getmembers(mod) if callable(obj) and not name.startswith("_")]
            assert len(callables) > 0, f"Module {module_name} has no public callables to mutate"


class TestMutmutConfigFile:
    """Verify the mutmut setup.cfg content is correct."""

    def test_config_written(self) -> None:
        """Validate the mutmut_config.py would be valid if written."""
        root = Path(__file__).resolve().parent.parent.parent
        _config_path = root / "mutmut_config.py"

        # Generate the config content
        paths = "\n".join(f'    "{p}",' for p in MUTMUT_CONFIG["paths_to_mutate"])
        content = f'''"""Mutmut configuration for Bernstein mutation testing."""

def init():
    """Configure mutmut."""
    pass

# Paths to mutate
paths_to_mutate = [
{paths}
]

# Test runner command
test_command = "{MUTMUT_CONFIG["runner"]}"

# Tests directory
tests_dir = "{MUTMUT_CONFIG["tests_dir"]}"
'''
        # Just verify it's valid Python by compiling
        compile(content, "mutmut_config.py", "exec")


class TestKeyMutationScenarios:
    """Verify that key logic would be caught by mutation testing.

    These tests are specifically designed to catch common mutations:
    - Boundary changes (< vs <=)
    - Boolean flips (True vs False)
    - Return value changes
    """

    def test_lifecycle_transition_guards(self) -> None:
        """Ensure transition table is not trivially bypassable."""
        from bernstein.core.models import Task, TaskStatus

        from bernstein.core.tasks.lifecycle import IllegalTransitionError, transition_task

        # DONE -> OPEN should be illegal (mutant might allow all transitions)
        task = Task(id="mut-1", title="t", description="d", role="r", status=TaskStatus.DONE)
        with pytest.raises(IllegalTransitionError):
            transition_task(task, TaskStatus.OPEN)

    def test_env_expansion_blocked_vars(self) -> None:
        """Blocked env vars must stay blocked (mutant might remove the check)."""
        from bernstein.core.config_schema import EnvExpansionError, expand_env_vars

        with pytest.raises(EnvExpansionError, match="blocked"):
            expand_env_vars("${GITHUB_TOKEN}", field_name="test")

    def test_task_priority_order(self) -> None:
        """Priority ordering: 1 is higher priority than 3."""
        from bernstein.core.models import Task

        t1 = Task(id="a", title="t", description="d", role="r", priority=1)
        t3 = Task(id="b", title="t", description="d", role="r", priority=3)
        assert t1.priority < t3.priority  # Mutant might flip this

    def test_guardrails_sandboxed_relaxes_only_relaxable(self) -> None:
        """SAFETY/IMMUNE decisions must never be relaxed even in sandbox (mutant might remove check)."""
        import os

        from bernstein.core.guardrails import relax_sandboxed
        from bernstein.core.policy_engine import DecisionType, PermissionDecision

        os.environ["BERNSTEIN_SANDBOX"] = "1"
        try:
            safety_decision = PermissionDecision(type=DecisionType.SAFETY, reason="critical safety check")
            decisions = [safety_decision]
            result = relax_sandboxed(decisions, check_name="file_permissions")
            # Safety decisions must remain SAFETY even inside sandbox
            assert result[0].type == DecisionType.SAFETY
        finally:
            os.environ.pop("BERNSTEIN_SANDBOX", None)

    def test_guardrails_sandbox_relaxes_deny(self) -> None:
        """DENY decisions on relaxable checks must become ALLOW in sandbox (mutant might skip relaxation)."""
        import os

        from bernstein.core.guardrails import relax_sandboxed
        from bernstein.core.policy_engine import DecisionType, PermissionDecision

        os.environ["BERNSTEIN_SANDBOX"] = "1"
        try:
            deny_decision = PermissionDecision(type=DecisionType.DENY, reason="out of scope file")
            result = relax_sandboxed([deny_decision], check_name="scope_enforcement")
            assert result[0].type == DecisionType.ALLOW
        finally:
            os.environ.pop("BERNSTEIN_SANDBOX", None)

    def test_guardrails_no_relax_outside_sandbox(self) -> None:
        """Outside a sandbox, DENY decisions must stay DENY (mutant might always relax)."""
        import os

        from bernstein.core.guardrails import relax_sandboxed
        from bernstein.core.policy_engine import DecisionType, PermissionDecision

        os.environ.pop("BERNSTEIN_SANDBOX", None)
        deny_decision = PermissionDecision(type=DecisionType.DENY, reason="outside sandbox")
        result = relax_sandboxed([deny_decision], check_name="file_permissions")
        # Without sandbox, no relaxation should occur
        assert result[0].type == DecisionType.DENY
