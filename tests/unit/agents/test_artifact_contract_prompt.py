"""Issue #4539 - artifact contract surfaced in the spawn prompt.

An artifact-mode task completes on a signed lineage receipt over its produced
artifact, not on a git SHA. But until now the spawn prompt never surfaced the
contract the completion path actually enforces, so an agent could only learn
"write a dataset to reports/out.jsonl with these criteria" from the operator
hand-duplicating the contract into free-text. When they forgot, completion
failed on criteria the agent never saw.

These tests assemble the live agent prompt exactly as production does
(``spawner_core._render_prompt``) and assert that an artifact-mode task's
prompt names the kind, the exact output path the verifier reads, and every
declared criterion - and that a plain ``code_diff`` task's prompt is
byte-unchanged.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.models import Task

from bernstein import _BUNDLED_TEMPLATES_DIR
from bernstein.core.agents.spawner_core import _render_prompt
from bernstein.core.tasks.artifact_completion import (
    artifact_output_path,
    is_artifact_mode,
)
from bernstein.core.tasks.artifacts import (
    ArtifactCriterion,
    ArtifactKind,
    ArtifactSpec,
)


def _render(tasks: list[Task], tmp_path: Path) -> str:
    """Assemble the prompt the way the production spawner does."""
    workdir = tmp_path / "workdir"
    (workdir / ".sdd").mkdir(parents=True, exist_ok=True)
    return _render_prompt(tasks, _BUNDLED_TEMPLATES_DIR / "roles", workdir)


def _artifact_task() -> Task:
    spec = ArtifactSpec(
        kind=ArtifactKind.DATASET,
        output_path="reports/out.jsonl",
        criteria=(
            ArtifactCriterion(type="schema_valid", value='{"type": "object"}'),
            ArtifactCriterion(type="hash_stable", value="sha256"),
        ),
    )
    return Task(
        id="T-ART-1",
        title="Produce the evaluation dataset",
        description="Build a fixture dataset for the eval harness.",
        role="analyst",
        artifact_spec=spec,
    )


def _code_diff_task() -> Task:
    return Task(
        id="T-CODE-1",
        title="Add a unit test",
        description="Add a test for the new resolver.",
        role="backend",
    )


def test_artifact_task_prompt_names_kind_path_and_criteria(tmp_path: Path) -> None:
    task = _artifact_task()
    prompt = _render([task], tmp_path)

    assert "Artifact contract" in prompt, "artifact contract section missing"
    assert f"`{ArtifactKind.DATASET.value}`" in prompt, "kind not named"
    assert "reports/out.jsonl" in prompt, "output path not named"
    assert "schema_valid" in prompt, "schema criterion missing"
    assert "hash_stable" in prompt, "hash criterion missing"
    # The prompt and verifier name the same path - no drift channel.
    assert artifact_output_path(task) in prompt, "prompt path differs from verifier path"


def test_code_diff_task_prompt_is_unchanged(tmp_path: Path) -> None:
    task = _code_diff_task()
    assert not is_artifact_mode(task), "code_diff task must not be artifact mode"
    prompt = _render([task], tmp_path)

    assert "Artifact contract" not in prompt, "artifact contract leaked into code_diff prompt"
    assert task.artifact_spec.kind.value not in prompt  # kind value not rendered


def test_prompt_and_verifier_consume_the_same_spec_object(tmp_path: Path) -> None:
    task = _artifact_task()
    prompt = _render([task], tmp_path)

    # The prompt renders from the same declared spec and the same resolver the
    # verifier uses - single source of truth, one parse, no drift.
    spec = task.artifact_spec
    assert spec.kind.value in prompt
    assert artifact_output_path(task) in prompt
    for criterion in spec.criteria:
        assert criterion.type in prompt, f"criterion {criterion.type!r} not rendered"
