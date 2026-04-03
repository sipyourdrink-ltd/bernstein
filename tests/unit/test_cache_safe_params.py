"""Tests for CacheSafeParams and prompt caching integration in spawn_prompt."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from bernstein.core.spawn_prompt import (
    _CACHEABLE_PROTOCOL_PREFIX,
    CacheSafeParams,
    _render_git_safety_protocol,
    build_cache_safe_params,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# TestCacheSafeParamsDataclass
# ---------------------------------------------------------------------------


class TestCacheSafeParamsDataclass:
    """Tests for the CacheSafeParams dataclass."""

    def test_frozen_dataclass(self) -> None:
        params = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        # Frozen dataclass should raise TypeError on mutation
        try:
            params.role = "qa"  # type: ignore[misc]
            raise AssertionError("Should have raised TypeError")
        except (TypeError, AttributeError):
            pass  # Expected

    def test_default_values(self) -> None:
        params = CacheSafeParams(
            role="backend",
            templates_hash="abc",
            project_context_hash="def",
            git_safety_protocol="safety",
        )
        assert params.agent_protocol_prefix == ""
        assert params.task_descriptions == ""
        assert params.specialist_descriptions == ""
        assert params.fork_messages == []
        assert params.session_id == ""

    def test_compute_cache_key_is_deterministic(self) -> None:
        params1 = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        params2 = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        assert params1.compute_cache_key() == params2.compute_cache_key()

    def test_compute_cache_key_differs_on_stable_field(self) -> None:
        params1 = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        params2 = CacheSafeParams(
            role="qa",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        assert params1.compute_cache_key() != params2.compute_cache_key()

    def test_compute_cache_key_matches_sha256(self) -> None:
        params = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        stable = (
            f"{params.role}\n"
            f"{params.templates_hash}\n"
            f"{params.project_context_hash}\n"
            f"{params.git_safety_protocol}\n"
            f"{params.agent_protocol_prefix}\n"
        )
        expected = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        assert params.compute_cache_key() == expected

    def test_validate_against_identical_parent(self) -> None:
        parent = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
            agent_protocol_prefix="protocol",
        )
        child = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
            agent_protocol_prefix="protocol",
        )
        assert child.validate_against(parent) == []

    def test_validate_against_detects_role_change(self) -> None:
        parent = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        child = CacheSafeParams(
            role="qa",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        breaks = child.validate_against(parent)
        assert "role" in breaks

    def test_validate_against_detects_template_change(self) -> None:
        parent = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        child = CacheSafeParams(
            role="backend",
            templates_hash="xyz789",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        breaks = child.validate_against(parent)
        assert "templates_hash" in breaks

    def test_validate_ignores_variable_fields(self) -> None:
        parent = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
        )
        child = CacheSafeParams(
            role="backend",
            templates_hash="abc123",
            project_context_hash="def456",
            git_safety_protocol="safety rules",
            task_descriptions="Different task block",
            specialist_descriptions="Different specialists",
            session_id="different-session",
            fork_messages=["extra message"],
        )
        assert child.validate_against(parent) == []

    def test_validate_detects_multiple_breaks(self) -> None:
        parent = CacheSafeParams(
            role="backend",
            templates_hash="abc",
            project_context_hash="def",
            git_safety_protocol="safety",
            agent_protocol_prefix="proto",
        )
        child = CacheSafeParams(
            role="qa",
            templates_hash="xyz",
            project_context_hash="ghi",
            git_safety_protocol="different safety",
            agent_protocol_prefix="other proto",
        )
        breaks = child.validate_against(parent)
        assert set(breaks) == {
            "role",
            "templates_hash",
            "project_context_hash",
            "git_safety_protocol",
            "agent_protocol_prefix",
        }


# ---------------------------------------------------------------------------
# TestBuildCacheSafeParams
# ---------------------------------------------------------------------------


class TestBuildCacheSafeParams:
    """Tests for the build_cache_safe_params function."""

    def test_build_with_empty_templates_dir(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )

        assert params.role == "backend"
        assert params.templates_hash == hashlib.sha256(b"").hexdigest()
        assert params.project_context_hash == hashlib.sha256(b"").hexdigest()
        assert params.session_id == ""

    def test_build_with_templates(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / "templates" / "roles" / "backend"
        templates_dir.mkdir(parents=True)
        (templates_dir.parent / "system_prompt.md").write_text("You are a backend specialist.", encoding="utf-8")
        workdir = tmp_path

        params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir.parent,
            workdir=workdir,
        )
        assert params.role == "backend"
        # Non-empty hash since a template file exists
        assert len(params.templates_hash) == 64
        assert params.templates_hash != hashlib.sha256(b"").hexdigest()

    def test_build_with_project_context(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path
        project_md = workdir / ".sdd" / "project.md"
        project_md.parent.mkdir(parents=True)
        project_md.write_text("This is my project.", encoding="utf-8")

        params = build_cache_safe_params(
            role="qa",
            templates_dir=templates_dir,
            workdir=workdir,
        )

        expected_hash = hashlib.sha256(b"This is my project.").hexdigest()
        assert params.project_context_hash == expected_hash

    def test_build_includes_task_and_specialist_content(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
            task_block="### Task 1: Fix auth bug",
            specialist_block="- **SecurityAgent**: Handles auth.",
            session_id="sess-001",
        )

        assert params.task_descriptions == "### Task 1: Fix auth bug"
        assert params.specialist_descriptions == "- **SecurityAgent**: Handles auth."
        assert params.session_id == "sess-001"

    def test_stable_params_produce_same_cache_key(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        params1 = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        params2 = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        assert params1.compute_cache_key() == params2.compute_cache_key()

    def test_different_roles_produce_different_cache_keys(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        params1 = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        params2 = build_cache_safe_params(
            role="qa",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        assert params1.compute_cache_key() != params2.compute_cache_key()

    def test_fork_validation_succeeds_with_same_params(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        parent = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        child = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
            task_block="### Task 2: New task",
        )
        breaks = child.validate_against(parent)
        assert breaks == []

    def test_git_safety_protocol_in_cache_params(self, tmp_path: Path) -> None:
        """build_cache_safe_params should include git safety rules."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        assert "git safety protocol" in params.git_safety_protocol.lower()
        assert "git safety protocol" in _render_git_safety_protocol().lower()
        assert params.git_safety_protocol == _render_git_safety_protocol()

    def test_protocol_prefix_constant(self) -> None:
        """_CACHEABLE_PROTOCOL_PREFIX should be a non-empty string."""
        assert "agent/" in _CACHEABLE_PROTOCOL_PREFIX
        assert "signal" in _CACHEABLE_PROTOCOL_PREFIX


# ---------------------------------------------------------------------------
# TestCacheSafeParamsPromptCachingIntegration
# ---------------------------------------------------------------------------


class TestCacheSafeParamsPromptCachingIntegration:
    """Tests proving cache key stability across fork scenarios."""

    def test_parent_and_child_fork_share_cache_key(self, tmp_path: Path) -> None:
        """When only task descriptions differ, fork cache key is stable."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        parent_params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        child_params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
            task_block="### Task 3: Refactored task",
            session_id="forked-session",
        )
        assert parent_params.compute_cache_key() == child_params.compute_cache_key()
        assert child_params.validate_against(parent_params) == []

    def test_template_change_breaks_fork_cache(self, tmp_path: Path) -> None:
        """Adding a template file changes the templates_hash, breaking cache."""
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        workdir = tmp_path

        parent_params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        # Add a new template file
        (templates_dir / "new_template.md").write_text("New prompt.", encoding="utf-8")
        child_params = build_cache_safe_params(
            role="backend",
            templates_dir=templates_dir,
            workdir=workdir,
        )
        breaks = child_params.validate_against(parent_params)
        assert "templates_hash" in breaks
        assert parent_params.compute_cache_key() != child_params.compute_cache_key()
