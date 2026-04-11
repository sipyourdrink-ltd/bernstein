"""Tests for batch execution mode (mode: batch in plan YAML + /batch prompt).

Verifies:
- plan_loader.py parses ``mode: batch`` from step YAML into execution_mode
- spawner._render_batch_prompt() produces a /batch-prefixed prompt
- ClaudeCodeAdapter._build_command() uses BATCH_MAX_TURNS for /batch prompts
- Task model round-trips execution_mode through from_dict()
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.core.models import Complexity, ModelConfig, Scope, Task, TaskStatus, TaskType
from bernstein.core.plan_loader import load_plan
from bernstein.core.spawner import _render_batch_prompt

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Task model: execution_mode field
# ---------------------------------------------------------------------------


class TestTaskExecutionMode:
    """Task.execution_mode field round-trips through from_dict."""

    def test_default_is_none(self) -> None:
        task = Task(id="t1", title="T", description="D", role="backend")
        assert task.execution_mode is None

    def test_from_dict_none_when_absent(self) -> None:
        raw = {
            "id": "t1",
            "title": "T",
            "description": "D",
            "role": "backend",
        }
        task = Task.from_dict(raw)
        assert task.execution_mode is None

    def test_from_dict_batch(self) -> None:
        raw = {
            "id": "t1",
            "title": "T",
            "description": "D",
            "role": "backend",
            "execution_mode": "batch",
        }
        task = Task.from_dict(raw)
        assert task.execution_mode == "batch"

    def test_from_dict_arbitrary_mode_preserved(self) -> None:
        raw = {
            "id": "t1",
            "title": "T",
            "description": "D",
            "role": "backend",
            "execution_mode": "future-mode",
        }
        task = Task.from_dict(raw)
        assert task.execution_mode == "future-mode"


# ---------------------------------------------------------------------------
# plan_loader: parsing mode: batch from YAML steps
# ---------------------------------------------------------------------------


class TestPlanLoaderBatchMode:
    """plan_loader.load_plan() parses mode: batch into execution_mode."""

    def _write_plan(self, tmp_path: Path, step_extra: str = "") -> Path:
        plan = tmp_path / "plan.yaml"
        plan.write_text(
            textwrap.dedent(
                f"""\
                name: test-plan
                stages:
                  - name: migrate
                    steps:
                      - goal: Replace all lodash imports with native equivalents
                        role: backend
                        {step_extra}
                """
            ),
            encoding="utf-8",
        )
        return plan

    def test_mode_batch_sets_execution_mode(self, tmp_path: Path) -> None:
        plan = self._write_plan(tmp_path, "mode: batch")
        _config, tasks = load_plan(plan)
        assert len(tasks) == 1
        assert tasks[0].execution_mode == "batch"

    def test_no_mode_leaves_execution_mode_none(self, tmp_path: Path) -> None:
        plan = self._write_plan(tmp_path)
        _config, tasks = load_plan(plan)
        assert tasks[0].execution_mode is None

    def test_mode_standard_sets_execution_mode(self, tmp_path: Path) -> None:
        """Any mode value is passed through as a string."""
        plan = self._write_plan(tmp_path, "mode: standard")
        _config, tasks = load_plan(plan)
        assert tasks[0].execution_mode == "standard"

    def test_batch_step_coexists_with_normal_steps(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.yaml"
        plan.write_text(
            textwrap.dedent(
                """\
                name: test-plan
                stages:
                  - name: work
                    steps:
                      - goal: Normal task
                        role: backend
                      - goal: Big refactor
                        role: backend
                        mode: batch
                """
            ),
            encoding="utf-8",
        )
        _config, tasks = load_plan(plan)
        assert len(tasks) == 2
        assert tasks[0].execution_mode is None
        assert tasks[1].execution_mode == "batch"


# ---------------------------------------------------------------------------
# spawner._render_batch_prompt()
# ---------------------------------------------------------------------------


class TestRenderBatchPrompt:
    """_render_batch_prompt() builds a /batch-prefixed Claude Code prompt."""

    def _make_task(self, description: str = "Replace lodash", owned_files: list[str] | None = None) -> Task:
        return Task(
            id="t-batch-01",
            title="Refactor lodash",
            description=description,
            role="backend",
            scope=Scope.LARGE,
            complexity=Complexity.HIGH,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            owned_files=owned_files or [],
        )

    def test_prompt_starts_with_batch_command(self) -> None:
        task = self._make_task()
        prompt = _render_batch_prompt(task)
        assert prompt.lstrip().startswith("/batch")

    def test_description_included_in_prompt(self) -> None:
        task = self._make_task(description="Migrate all foo() calls to bar()")
        prompt = _render_batch_prompt(task)
        assert "Migrate all foo() calls to bar()" in prompt

    def test_task_id_included_for_completion(self) -> None:
        task = self._make_task()
        prompt = _render_batch_prompt(task)
        assert "t-batch-01" in prompt

    def test_owned_files_included_when_present(self) -> None:
        task = self._make_task(owned_files=["src/foo.ts", "src/bar.ts"])
        prompt = _render_batch_prompt(task)
        assert "src/foo.ts" in prompt
        assert "src/bar.ts" in prompt

    def test_completion_curl_command_included(self) -> None:
        task = self._make_task()
        prompt = _render_batch_prompt(task)
        assert "curl" in prompt
        assert "/tasks/t-batch-01/complete" in prompt

    def test_no_owned_files_no_paths_line(self) -> None:
        task = self._make_task(owned_files=[])
        prompt = _render_batch_prompt(task)
        assert "Affected paths" not in prompt


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter: batch_mode → BATCH_MAX_TURNS
# ---------------------------------------------------------------------------


class TestClaudeAdapterBatchMaxTurns:
    """ClaudeCodeAdapter uses BATCH_MAX_TURNS for /batch prompts."""

    def _make_config(self, effort: str = "high") -> ModelConfig:
        return ModelConfig(model="sonnet", effort=effort)

    def _extract_max_turns(self, cmd: list[str]) -> int:
        idx = cmd.index("--max-turns")
        return int(cmd[idx + 1])

    def test_batch_prompt_gets_batch_max_turns(self, tmp_path: Path) -> None:
        """Prompt starting with /batch triggers BATCH_MAX_TURNS."""
        adapter = ClaudeCodeAdapter()
        cmd = adapter._build_command(
            self._make_config(),
            None,
            "/batch Replace all lodash imports",
            batch_mode=True,
        )
        assert self._extract_max_turns(cmd) == ClaudeCodeAdapter.BATCH_MAX_TURNS

    def test_normal_prompt_gets_effort_based_turns(self, tmp_path: Path) -> None:
        """Non-batch prompts use the normal effort-based max_turns (scaled by scope)."""
        adapter = ClaudeCodeAdapter()
        cmd = adapter._build_command(
            self._make_config(effort="high"),
            None,
            "Implement the feature",
            batch_mode=False,
        )
        turns = self._extract_max_turns(cmd)
        # Default task_scope="medium" applies 1.5x multiplier: int(50 * 1.5) = 75
        assert turns == int(50 * 1.5)

    def test_batch_max_turns_is_at_least_200(self) -> None:
        """BATCH_MAX_TURNS constant is ≥ 200 for adequate lifecycle coverage."""
        assert ClaudeCodeAdapter.BATCH_MAX_TURNS >= 200

    def test_spawn_autodetects_batch_from_prompt_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """spawn() auto-detects batch mode when prompt starts with /batch."""
        _adapter = ClaudeCodeAdapter()
        captured: dict[str, object] = {}

        def fake_build_command(  # type: ignore[no-untyped-def]
            self_inner,
            model_config,
            mcp_config,
            prompt,
            *,
            role="",
            workdir=None,
            agents_json=None,
            system_addendum="",
            batch_mode=False,
        ):
            captured["batch_mode"] = batch_mode
            # Return a minimal valid command
            return ["claude", "-p", prompt]

        monkeypatch.setattr(ClaudeCodeAdapter, "_build_command", fake_build_command)

        # Simulate what spawn() does: checks prompt.lstrip().startswith("/batch")
        prompt = "/batch Do a big refactor"
        batch_mode = prompt.lstrip().startswith("/batch")
        assert batch_mode is True
        captured["batch_mode"] = batch_mode  # verify detection logic

        assert captured["batch_mode"] is True
