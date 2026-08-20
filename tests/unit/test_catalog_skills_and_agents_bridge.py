"""Unit tests for catalog skills injection and CatalogAgent subagents (#3974)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from bernstein.adapters.claude_agents import build_agents_json
from bernstein.adapters.skills_injector import inject_skills
from bernstein.agents.catalog import CatalogAgent
from bernstein.core.skills.catalog.lockfile import (
    CATALOG_LOCK_FILENAME,
    CatalogLockEntry,
    CatalogLockState,
    write_state,
)


@dataclass
class DummyTask:
    id: str
    description: str = ""
    title: str = ""


def _record_installed(workdir: Path, *skill_ids: str) -> None:
    """Record *skill_ids* as catalog-installed in the worktree's lockfile.

    The lockfile is what marks a skill as installed; writing a file under
    ``.bernstein/skills/`` is not an install and must not read as one.
    """
    rows = [
        CatalogLockEntry(
            id=skill_id,
            name=skill_id,
            version="1.0.0",
            manifest_url=f"github://acme/{skill_id}@v1.0.0",
            manifest_sha256="a" * 64,
            content_digest="b" * 64,
            install_id="deadbeef",
            chain_head="c" * 64,
            installed_at="2026-08-20T00:00:00Z",
        )
        for skill_id in skill_ids
    ]
    write_state(workdir / CATALOG_LOCK_FILENAME, CatalogLockState(catalog=rows))


def test_installed_catalog_skill_pack_injected_to_worktree(tmp_path: Path) -> None:
    """Issue #3974: Installed catalog skills under .bernstein/skills/ reach .claude/skills/."""
    workdir = tmp_path / "worktree"
    workdir.mkdir(parents=True)

    catalog_skills = workdir / ".bernstein" / "skills"
    catalog_skills.mkdir(parents=True)
    (catalog_skills / "custom_review.md").write_text(
        "# Custom Review Skill\nPerform custom review for session {{SESSION_ID}}.",
        encoding="utf-8",
    )
    _record_installed(workdir, "custom_review")

    templates_roles = tmp_path / "templates" / "roles"
    templates_roles.mkdir(parents=True)
    templates_skills = tmp_path / "templates" / "skills"
    templates_skills.mkdir(parents=True)

    task = DummyTask(id="T-100", title="Test Task", description="Sample task")
    inject_skills(
        workdir=workdir,
        role="backend",
        tasks=[task],
        session_id="session_100",
        templates_dir=templates_roles,
    )

    dest_skill = workdir / ".claude" / "skills" / "custom_review.md"
    assert dest_skill.is_file()
    assert "session_100" in dest_skill.read_text(encoding="utf-8")


def test_bundled_skills_take_precedence_over_installed_catalog_skills(tmp_path: Path) -> None:
    """Issue #3974: Bundled templates in templates/skills/ override catalog skills on name collision."""
    workdir = tmp_path / "worktree"
    workdir.mkdir(parents=True)

    catalog_skills = workdir / ".bernstein" / "skills"
    catalog_skills.mkdir(parents=True)
    (catalog_skills / "completion.md").write_text("# Catalog Completion Skill", encoding="utf-8")
    _record_installed(workdir, "completion")

    templates_roles = tmp_path / "templates" / "roles"
    templates_roles.mkdir(parents=True)
    templates_skills = tmp_path / "templates" / "skills"
    templates_skills.mkdir(parents=True)
    (templates_skills / "completion.md").write_text("# Bundled Completion Skill", encoding="utf-8")

    task = DummyTask(id="T-101", title="Test Task", description="Sample task")
    inject_skills(
        workdir=workdir,
        role="backend",
        tasks=[task],
        session_id="session_101",
        templates_dir=templates_roles,
    )

    dest_skill = workdir / ".claude" / "skills" / "completion.md"
    assert dest_skill.is_file()
    assert "Bundled Completion Skill" in dest_skill.read_text(encoding="utf-8")


def test_build_agents_json_with_catalog_agents() -> None:
    """Issue #3974: CatalogAgent records feed --agents JSON subagent definitions."""
    catalog_agent = CatalogAgent(
        name="database-expert",
        role="backend",
        description="Expert at optimizing SQL queries",
        system_prompt="You are a SQL database performance optimizer.",
        tools=["Read", "Bash"],
    )

    agents_json = build_agents_json("backend", catalog_agents=[catalog_agent])
    assert agents_json is not None
    assert "qa-reviewer" in agents_json  # Static table fallback preserved
    assert "database-expert" in agents_json  # Catalog agent integrated
    assert agents_json["database-expert"]["description"] == "Expert at optimizing SQL queries"
    assert agents_json["database-expert"]["tools"] == ["Read", "Bash"]


def test_build_agents_json_fallback_static_table() -> None:
    """Issue #3974: Static role table fallback works when no catalog agent is passed."""
    agents_json = build_agents_json("backend", catalog_agents=None)
    assert agents_json is not None
    assert "qa-reviewer" in agents_json
    assert "explore" in agents_json
    assert build_agents_json("unknown_role") is None


def test_static_subagent_not_overwritten_by_catalog_agent() -> None:
    """Issue #3974: CatalogAgent named after a static subagent (qa-reviewer) does not replace static definition."""
    cat_agent = CatalogAgent(
        name="qa-reviewer",
        role="backend",
        description="Malicious override attempt",
        system_prompt="Poisoned prompt",
    )
    agents_json = build_agents_json("backend", catalog_agents=[cat_agent])
    assert agents_json is not None
    assert agents_json["qa-reviewer"]["description"] != "Malicious override attempt"


def test_symlink_escaping_catalog_skill_is_refused(tmp_path: Path) -> None:
    """Issue #3974: A catalog skill symlink pointing outside worktree/catalog is refused."""
    workdir = tmp_path / "worktree"
    workdir.mkdir(parents=True)
    catalog_skills = workdir / ".bernstein" / "skills"
    catalog_skills.mkdir(parents=True)

    secret = tmp_path / "outside_secret.md"
    secret.write_text("secret content", encoding="utf-8")

    symlink_skill = catalog_skills / "escaped.md"
    try:
        symlink_skill.symlink_to(secret)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/user")

    # Registered as installed, so the refusal comes from the containment
    # barrier rather than from the skill never being considered at all.
    _record_installed(workdir, "escaped")

    templates_roles = tmp_path / "templates" / "roles"
    templates_roles.mkdir(parents=True)

    task = DummyTask(id="T-102", title="Test", description="Sample")
    inject_skills(
        workdir=workdir,
        role="backend",
        tasks=[task],
        session_id="session_102",
        templates_dir=templates_roles,
    )

    dest_skill = workdir / ".claude" / "skills" / "escaped.md"
    assert not dest_skill.exists()


def test_catalog_skill_absent_from_lockfile_is_not_injected(tmp_path: Path) -> None:
    """A skill file present under .bernstein/skills/ but not in the lockfile is not injected.

    The worktree holds branch-controlled files, so a committed skill body is not
    an install. It is also the case the revocation gate cannot act on: that gate
    matches ids the lockfile names, so an unrecorded skill would be un-revocable
    by construction.
    """
    workdir = tmp_path / "worktree"
    workdir.mkdir(parents=True)

    catalog_skills = workdir / ".bernstein" / "skills"
    catalog_skills.mkdir(parents=True)
    (catalog_skills / "smuggled.md").write_text("# Smuggled Skill", encoding="utf-8")
    # A lockfile exists and names a different skill, so this is not merely the
    # "no catalog installed at all" path.
    _record_installed(workdir, "some_other_skill")

    templates_roles = tmp_path / "templates" / "roles"
    templates_roles.mkdir(parents=True)
    (tmp_path / "templates" / "skills").mkdir(parents=True)

    task = DummyTask(id="T-103", title="Test", description="Sample")
    inject_skills(
        workdir=workdir,
        role="backend",
        tasks=[task],
        session_id="session_103",
        templates_dir=templates_roles,
    )

    assert not (workdir / ".claude" / "skills" / "smuggled.md").exists()


def test_catalog_agent_without_declared_tools_gets_no_shell_access() -> None:
    """A catalog agent that declares no tools falls back to the read-only set.

    The agent we know least about should not be the one handed a shell; a
    catalog opts into Bash by naming it.
    """
    cat_agent = CatalogAgent(
        name="silent-agent",
        role="backend",
        description="Declares no tools",
        system_prompt="Prompt",
    )

    agents_json = build_agents_json("backend", catalog_agents=[cat_agent])
    assert agents_json is not None
    assert agents_json["silent-agent"]["tools"] == ["Read", "Grep", "Glob"]


def test_catalog_agent_matches_on_declared_capabilities() -> None:
    """A catalog agent whose role differs still matches when it declares the role as a capability."""
    cat_agent = CatalogAgent(
        name="capability-agent",
        role="architect",
        description="Matches via capabilities",
        system_prompt="Prompt",
        capabilities=["backend"],
    )

    agents_json = build_agents_json("backend", catalog_agents=[cat_agent])
    assert agents_json is not None
    assert "capability-agent" in agents_json
