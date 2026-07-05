"""Tests for the shared completion-contract template include (#2244).

The completion instruction block is defined once in
``templates/roles/_includes/completion_contract.md`` and pulled into
every role's ``task_prompt.md`` via the renderer's ``{{INCLUDE name}}``
directive, so the schema-emitting instructions stay identical across
roles. The runtime worker prompt built by ``spawn_prompt`` renders the
same include, keeping prompt and template in lockstep.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein import _BUNDLED_TEMPLATES_DIR
from bernstein.core.agents.spawn_prompt import _render_completion_instructions
from bernstein.core.tasks.contracts import WORKER_CONTRACT_VERSION, RefusalKind
from bernstein.core.tasks.models import Task
from bernstein.templates.renderer import TemplateError, render_template

_ROLES_DIR = Path(__file__).resolve().parents[2] / "templates" / "roles"
_INCLUDE_PATH = _ROLES_DIR / "_includes" / "completion_contract.md"


def _task(task_id: str = "T-1") -> Task:
    return Task(id=task_id, title="t", description="d", role="backend")


# ---------------------------------------------------------------------------
# The include file itself
# ---------------------------------------------------------------------------


class TestIncludeFile:
    def test_include_file_exists(self) -> None:
        assert _INCLUDE_PATH.is_file()

    def test_include_emits_contract_version(self) -> None:
        body = _INCLUDE_PATH.read_text(encoding="utf-8")
        assert WORKER_CONTRACT_VERSION in body

    def test_include_documents_every_refusal_kind(self) -> None:
        body = _INCLUDE_PATH.read_text(encoding="utf-8")
        for kind in RefusalKind:
            assert kind.value in body, f"refusal kind {kind.value} missing from include"

    def test_include_targets_complete_endpoint(self) -> None:
        body = _INCLUDE_PATH.read_text(encoding="utf-8")
        assert "/tasks/{{TASK_ID}}/complete" in body


# ---------------------------------------------------------------------------
# Renderer {{INCLUDE}} directive
# ---------------------------------------------------------------------------


class TestRendererInclude:
    def test_include_directive_expands(self, tmp_path: Path) -> None:
        roles = tmp_path / "roles"
        (roles / "_includes").mkdir(parents=True)
        (roles / "backend").mkdir()
        (roles / "_includes" / "snippet.md").write_text("shared block for {{TASK_ID}}", encoding="utf-8")
        template = roles / "backend" / "task_prompt.md"
        template.write_text("# Task\n\n{{INCLUDE snippet}}\n", encoding="utf-8")
        rendered = render_template(template, {"TASK_ID": "T-9"})
        assert "shared block for T-9" in rendered
        assert "{{INCLUDE" not in rendered

    def test_missing_include_raises_template_error(self, tmp_path: Path) -> None:
        roles = tmp_path / "roles"
        (roles / "backend").mkdir(parents=True)
        template = roles / "backend" / "task_prompt.md"
        template.write_text("{{INCLUDE nonexistent}}", encoding="utf-8")
        with pytest.raises(TemplateError):
            render_template(template, {})

    def test_role_task_prompts_render_with_real_include(self) -> None:
        template = _ROLES_DIR / "backend" / "task_prompt.md"
        rendered = render_template(
            template,
            {"TASK_TITLE": "Sample", "TASK_DESCRIPTION": "Sample body", "TASK_ID": "T-7"},
        )
        assert "{{INCLUDE" not in rendered
        assert "/tasks/T-7/complete" in rendered
        assert WORKER_CONTRACT_VERSION in rendered


# ---------------------------------------------------------------------------
# All role templates use the single include
# ---------------------------------------------------------------------------


class TestRoleTemplatesUseInclude:
    def test_every_role_task_prompt_has_the_include_once(self) -> None:
        role_dirs = sorted(d for d in _ROLES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_"))
        assert role_dirs, "no role template directories found"
        for role_dir in role_dirs:
            task_prompt = role_dir / "task_prompt.md"
            if not task_prompt.is_file():
                continue
            body = task_prompt.read_text(encoding="utf-8")
            assert body.count("{{INCLUDE completion_contract}}") == 1, (
                f"{role_dir.name}/task_prompt.md must pull the completion contract via the shared include"
            )
            assert '"result_summary": "{{TASK_TITLE}}' not in body, (
                f"{role_dir.name}/task_prompt.md still carries a legacy inline done-signal block"
            )


# ---------------------------------------------------------------------------
# Runtime worker prompt uses the same include
# ---------------------------------------------------------------------------


class TestSpawnPromptInstructions:
    def test_single_task_instructions_carry_contract(self) -> None:
        text = _render_completion_instructions([_task("T-42")])
        assert "/tasks/T-42/complete" in text
        assert WORKER_CONTRACT_VERSION in text
        for kind in RefusalKind:
            assert kind.value in text

    def test_multi_task_instructions_list_all_ids(self) -> None:
        text = _render_completion_instructions([_task("T-1"), _task("T-2")])
        assert "T-1" in text
        assert "T-2" in text
        assert WORKER_CONTRACT_VERSION in text

    def test_instructions_keep_retry_guidance(self) -> None:
        text = _render_completion_instructions([_task("T-1")])
        assert "409" in text

    def test_bundled_include_is_packaged(self) -> None:
        bundled = _BUNDLED_TEMPLATES_DIR / "roles" / "_includes" / "completion_contract.md"
        assert bundled.is_file()
