import hashlib
from pathlib import Path

import pytest
from bernstein.core.models import Task

from bernstein.adapters import skills_injector
from bernstein.adapters.skills_injector import (
    inject_skills,
    render_skill_template,
    sanitize_skill_body,
)

SKILL_BODY = """---
version: 1.2.3
---
# Example skill

Session: {{SESSION_ID}}
Tasks: {{TASK_IDS}}
"""


@pytest.fixture(autouse=True)
def patch_skill_mappings(monkeypatch):
    monkeypatch.setattr(skills_injector, "ROLE_SKILL_MAP", {"backend": ["example.md"]})
    if hasattr(skills_injector, "_ALWAYS_INJECT"):
        monkeypatch.setattr(skills_injector, "_ALWAYS_INJECT", [])


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    roles_dir = tmp_path / "templates" / "roles"
    skills_dir = tmp_path / "templates" / "skills"
    roles_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)
    (skills_dir / "example.md").write_text(SKILL_BODY, encoding="utf-8")
    return roles_dir


def _expected_bytes(session_id: str, tasks: list) -> bytes:
    sanitized = sanitize_skill_body(
        SKILL_BODY,
        skill_name="example.md",
        origin="templates/skills/example.md",
        source_name="templates/skills",
    )
    rendered = render_skill_template(sanitized, session_id=session_id, tasks=tasks)
    return rendered.encode("utf-8")


def test_written_bytes_match_render_pipeline_independent_of_audit(tmp_path: Path, templates_dir: Path):
    workdir = tmp_path / "worktree"
    workdir.mkdir()

    tasks = [
        Task(id="task-1", title="Setup DB", description="Init database", role="backend"),
        Task(id="task-2", title="Write API", description="Create endpoints", role="backend"),
    ]

    inject_skills(
        workdir=workdir,
        role="backend",
        tasks=tasks,
        session_id="sess-123",
        templates_dir=templates_dir,
    )

    written_path = workdir / ".claude" / "skills" / "example.md"
    assert written_path.exists(), "skill file was not written to the worktree"

    actual_bytes = written_path.read_bytes()
    expected_bytes = _expected_bytes(session_id="sess-123", tasks=tasks)
    assert actual_bytes == expected_bytes


def test_writing_is_deterministic_across_repeated_calls(tmp_path: Path, templates_dir: Path):
    tasks = [Task(id="task-1", title="Determinism Check", description="Test", role="backend")]

    workdir_a = tmp_path / "worktree_a"
    workdir_b = tmp_path / "worktree_b"
    workdir_a.mkdir()
    workdir_b.mkdir()

    inject_skills(
        workdir=workdir_a,
        role="backend",
        tasks=tasks,
        session_id="sess-abc",
        templates_dir=templates_dir,
    )
    inject_skills(
        workdir=workdir_b,
        role="backend",
        tasks=tasks,
        session_id="sess-abc",
        templates_dir=templates_dir,
    )

    file_a = (workdir_a / ".claude" / "skills" / "example.md").read_bytes()
    file_b = (workdir_b / ".claude" / "skills" / "example.md").read_bytes()
    assert file_a == file_b


def test_audit_record_digests_match_written_bytes(tmp_path: Path, templates_dir: Path):
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    tasks = [Task(id="task-1", title="Digest test", description="Check hash", role="backend")]

    audit_records = inject_skills(
        workdir=workdir,
        role="backend",
        tasks=tasks,
        session_id="sess-xyz",
        templates_dir=templates_dir,
    )

    injected = [r for r in audit_records if r["template_name"] == "example.md"]
    assert len(injected) == 1
    record = injected[0]
    assert record["status"] == "injected"
    assert record["source"] == "injected"

    written_bytes = (workdir / ".claude" / "skills" / "example.md").read_bytes()
    actual_rendered_digest = hashlib.blake2b(written_bytes).hexdigest()
    assert record["rendered_digest"] == actual_rendered_digest


def test_empty_injection_returns_explicit_empty_list(tmp_path: Path):
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    missing_templates_dir = tmp_path / "does_not_exist" / "roles"

    audit_records = inject_skills(
        workdir=workdir,
        role="backend",
        tasks=[],
        session_id="sess-empty",
        templates_dir=missing_templates_dir,
    )

    assert audit_records == []


def test_refused_record_has_full_shape_with_empty_digests(tmp_path: Path, templates_dir: Path, monkeypatch):
    workdir = tmp_path / "worktree"
    workdir.mkdir()

    if hasattr(skills_injector, "_revoked_skill_ids"):
        monkeypatch.setattr(
            skills_injector,
            "_revoked_skill_ids",
            lambda wd: {"example.md", "example"},
        )

    tasks = [Task(id="task-1", title="Refused task", description="Will not run", role="backend")]

    audit_records = inject_skills(
        workdir=workdir,
        role="backend",
        tasks=tasks,
        session_id="sess-revoked",
        templates_dir=templates_dir,
    )

    refused = [r for r in audit_records if r["template_name"] == "example.md"]
    assert len(refused) == 1, "the skill must appear in the audit log with status refused"

    record = refused[0]
    assert record["status"] == "refused"
    assert record["source"] == "injected"
    assert record["version"] == ""
    assert record["pre_render_digest"] == ""
    assert record["rendered_digest"] == ""
    assert not (workdir / ".claude" / "skills" / "example.md").exists()
