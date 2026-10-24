"""Regression tests: persisted role-adapter policy is enforced at runtime (#6648).

Before the fix, AgentSpawner._get_adapter_by_name called enforce() without
passing a policy argument, so it used the process-global default (empty /
unrestricted). A restriction written to .sdd/security/role_adapter_policy.json
by the CLI was never loaded at spawn time.

These tests verify that the workdir-specific policy file is consulted for
every adapter spawn, both for cached and uncached adapters, and that two
independent workdirs with different policies do not bleed into each other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.core.security.role_adapter_policy import (
    DEFAULT_POLICY_PATH,
    PolicyFileCorrupt,
    RoleAdapterDenied,
    load_policy_file,
    reset_policy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPAWNER = "bernstein.core.agents.spawner_core"


def _make_spawner(workdir: Path) -> Any:
    """Construct a minimal AgentSpawner-like namespace for unit testing.

    We call _get_adapter_by_name directly rather than constructing the full
    AgentSpawner (which requires a live adapter, templates dir, etc.).
    """
    from bernstein.core.agents.spawner_core import AgentSpawner

    spawner = object.__new__(AgentSpawner)
    spawner._workdir = workdir
    spawner._adapter_cache: dict[str, Any] = {}
    spawner._enable_caching = False
    return spawner


def _write_policy(workdir: Path, policy: dict[str, list[str]]) -> None:
    """Write a role_adapter_policy.json for *workdir*."""
    path = workdir / DEFAULT_POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoleAdapterPolicyRuntime:
    """Persisted role-adapter policy is enforced at runtime (#6648)."""

    def setup_method(self) -> None:
        reset_policy()  # ensure global policy is empty/unrestricted before each test

    def teardown_method(self) -> None:
        reset_policy()

    def test_disallowed_adapter_raises_before_cache_lookup(self, tmp_path: Path) -> None:
        """A denied adapter raises RoleAdapterDenied even when it is already cached."""
        _write_policy(tmp_path, {"docs": ["claude"]})
        spawner = _make_spawner(tmp_path)
        marker = object()
        spawner._adapter_cache["codex"] = marker  # pre-warm the cache

        with pytest.raises(RoleAdapterDenied) as exc_info:
            spawner._get_adapter_by_name("codex", role="docs")

        assert exc_info.value.adapter == "codex"
        assert exc_info.value.role == "docs"

    def test_allowed_adapter_proceeds(self, tmp_path: Path) -> None:
        """An adapter on the allow-list is returned from the cache without error."""
        _write_policy(tmp_path, {"docs": ["claude"]})
        spawner = _make_spawner(tmp_path)
        marker = object()
        spawner._adapter_cache["claude"] = marker

        result = spawner._get_adapter_by_name("claude", role="docs")

        assert result is marker

    def test_no_policy_file_permits_all(self, tmp_path: Path) -> None:
        """When no policy file exists all adapters are permitted (back-compat)."""
        spawner = _make_spawner(tmp_path)
        marker = object()
        spawner._adapter_cache["anything"] = marker

        result = spawner._get_adapter_by_name("anything", role="backend")

        assert result is marker

    def test_no_role_skips_enforcement(self, tmp_path: Path) -> None:
        """When role=None the policy check is skipped entirely."""
        _write_policy(tmp_path, {"docs": ["claude"]})
        spawner = _make_spawner(tmp_path)
        marker = object()
        spawner._adapter_cache["codex"] = marker

        result = spawner._get_adapter_by_name("codex", role=None)

        assert result is marker

    def test_two_workdirs_do_not_share_policy(self, tmp_path: Path) -> None:
        """Two spawners with different workdirs enforce their own policies independently."""
        workdir_a = tmp_path / "a"
        workdir_a.mkdir()
        workdir_b = tmp_path / "b"
        workdir_b.mkdir()

        _write_policy(workdir_a, {"qa": ["mock"]})  # qa may only use mock in A
        # workdir_b has no policy — all adapters allowed

        marker_a = object()
        marker_b = object()

        spawner_a = _make_spawner(workdir_a)
        spawner_a._adapter_cache["other"] = marker_a

        spawner_b = _make_spawner(workdir_b)
        spawner_b._adapter_cache["other"] = marker_b

        # A blocks 'other' for qa
        with pytest.raises(RoleAdapterDenied):
            spawner_a._get_adapter_by_name("other", role="qa")

        # B permits 'other' for qa (no policy file)
        result = spawner_b._get_adapter_by_name("other", role="qa")
        assert result is marker_b

    def test_global_policy_not_mutated(self, tmp_path: Path) -> None:
        """Runtime enforcement does not alter the process-global policy."""
        from bernstein.core.security.role_adapter_policy import get_policy

        _write_policy(tmp_path, {"security": ["mock"]})
        spawner = _make_spawner(tmp_path)
        marker = object()
        spawner._adapter_cache["mock"] = marker

        before = get_policy()
        spawner._get_adapter_by_name("mock", role="security")
        after = get_policy()

        assert before is after, "global policy must not change after runtime enforcement"

    def test_deny_emits_audit_event(self, tmp_path: Path) -> None:
        """A denied spawn attempts to write a role.adapter.denied audit event."""
        _write_policy(tmp_path, {"frontend": ["claude"]})
        spawner = _make_spawner(tmp_path)
        spawner._adapter_cache["aider"] = object()

        with (
            patch(f"{_SPAWNER}.AgentSpawner._get_adapter_by_name.__module__"),
            pytest.raises(RoleAdapterDenied),
        ):
            spawner._get_adapter_by_name("aider", role="frontend")

    def test_corrupt_policy_file_raises_policy_file_corrupt(self, tmp_path: Path) -> None:
        """load_policy_file raises PolicyFileCorrupt when file exists but is malformed."""
        path = tmp_path / DEFAULT_POLICY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a valid file then truncate it (simulate torn write)
        path.write_bytes(b'{"docs": ["claude"')  # unterminated JSON

        with pytest.raises(PolicyFileCorrupt):
            load_policy_file(path)

    def test_corrupt_policy_file_denies_spawn(self, tmp_path: Path) -> None:
        """A corrupt policy file causes spawner to deny the adapter (fail-closed)."""
        path = tmp_path / DEFAULT_POLICY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"docs": ["claude"')  # unterminated JSON

        spawner = _make_spawner(tmp_path)
        spawner._adapter_cache["codex"] = object()

        # A corrupt policy file must not silently permit all adapters.
        with pytest.raises(RoleAdapterDenied):
            spawner._get_adapter_by_name("codex", role="docs")
