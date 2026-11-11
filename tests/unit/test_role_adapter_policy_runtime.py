"""Regression tests: persisted role-adapter policy is enforced at runtime (#6648).

Before the fix, AgentSpawner._get_adapter_by_name called enforce() without
passing a policy argument, so it used the process-global default (empty /
unrestricted). A restriction written to .sdd/security/role_adapter_policy.json
by the CLI was never loaded at spawn time.

These tests exercise initial dispatch, resume, failover, backend boundaries,
persisted input validation and real audit records. Two independent project
roots and nested task worktrees must not bleed into each other's policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.core.security.role_adapter_policy import (
    DEFAULT_POLICY_PATH,
    PolicyFileCorrupt,
    RoleAdapterDenied,
    load_policy_file,
    reset_policy,
)


@pytest.mark.parametrize("value", ["codex", None, {}, 42, ["codex", None], ["codex", {}]])
def test_policy_file_rejects_invalid_mapping_values_without_lifting_restrictions(tmp_path, value):
    path = tmp_path / DEFAULT_POLICY_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"docs": value}))
    with pytest.raises(PolicyFileCorrupt, match="docs"):
        load_policy_file(path)


@pytest.mark.parametrize("contents", [b'{"docs":', b"[]", b"\xff"])
def test_present_unusable_policy_file_denies_with_reason(tmp_path, contents):
    path = tmp_path / DEFAULT_POLICY_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(contents)
    with pytest.raises(PolicyFileCorrupt, match="policy file"):
        load_policy_file(path)


def test_policy_directory_and_unreadable_file_are_not_missing(tmp_path, monkeypatch):
    path = tmp_path / DEFAULT_POLICY_PATH
    path.mkdir(parents=True)
    with pytest.raises(PolicyFileCorrupt, match="policy file"):
        load_policy_file(path)
    path.rmdir()
    path.write_text("{}")
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_text", MagicMock(side_effect=PermissionError("unreadable fixture")))
        with pytest.raises(PolicyFileCorrupt, match="unreadable fixture"):
            load_policy_file(path)
    path.unlink()
    assert load_policy_file(path).is_allowed("docs", "qwen")


def test_explicit_empty_policy_preserves_default(tmp_path):
    _write_policy(tmp_path, {"docs": []})
    assert load_policy_file(tmp_path / DEFAULT_POLICY_PATH).is_allowed("docs", "qwen")


@pytest.fixture
def dispatch_spawner(tmp_path, mock_adapter_factory, monkeypatch):
    from bernstein.core.agents.spawner_core import AgentSpawner

    def build(root=tmp_path, name="qwen", **kwargs):
        root.mkdir(parents=True, exist_ok=True)
        adapter = mock_adapter_factory()
        adapter.name.return_value = name
        spawner = AgentSpawner(adapter, root, root, default_model="mock-model", use_worktrees=False, **kwargs)
        # Admission probes have their own tests; never invoke vendor binaries here.
        monkeypatch.setattr(spawner, "_preflight_adapter_security_floor", MagicMock())
        monkeypatch.setattr(spawner, "_preflight_adapter_admission", MagicMock())
        monkeypatch.setattr(spawner, "_record_adapter_capability_selection", MagicMock())
        return spawner, adapter

    return build


@pytest.mark.parametrize("cached", [False, True])
def test_persisted_role_policy_blocks_cached_and_uncached_initial_dispatch(
    tmp_path, dispatch_spawner, make_task, monkeypatch, cached
):
    spawner, adapter = dispatch_spawner()
    _write_policy(tmp_path, {"docs": ["codex"]})
    if not cached:
        spawner._adapter_cache.clear()
    constructor = MagicMock(side_effect=AssertionError("forbidden constructor"))
    monkeypatch.setattr(f"{_SPAWNER}.get_adapter", constructor)
    worktree = MagicMock()
    spawner._use_worktrees = True
    monkeypatch.setattr(spawner, "_worktree_manager_for_repo", worktree)
    with pytest.raises(RoleAdapterDenied):
        spawner.spawn_for_tasks([make_task(role="docs")])
    constructor.assert_not_called()
    adapter.spawn.assert_not_called()
    worktree.assert_not_called()


def test_resume_uses_current_saved_role_policy_before_adapter_spawn(tmp_path, dispatch_spawner, make_task, monkeypatch):
    spawner, adapter = dispatch_spawner()
    _write_policy(tmp_path, {"docs": ["codex"]})
    owner = MagicMock()
    spawner._worktree_managers = {tmp_path: owner}
    constructor = MagicMock(side_effect=AssertionError("forbidden constructor"))
    monkeypatch.setattr(f"{_SPAWNER}.get_adapter", constructor)
    with pytest.raises(RoleAdapterDenied):
        spawner.spawn_for_resume([make_task(role="docs")], worktree_path=tmp_path / "old", changed_files=[])
    adapter.spawn.assert_not_called()
    owner.adopt.assert_not_called()
    constructor.assert_not_called()


@pytest.mark.parametrize("resume", [False, True])
def test_allowed_role_adapter_runs_after_policy_reload_and_restart(tmp_path, dispatch_spawner, make_task, resume):
    _write_policy(tmp_path, {"docs": ["codex"]})
    old, _ = dispatch_spawner()
    with pytest.raises(RoleAdapterDenied):
        old._get_adapter_by_name("qwen", role="docs")
    _write_policy(tmp_path, {"docs": ["qwen"]})
    fresh, adapter = dispatch_spawner()
    task = make_task(role="docs")
    if resume:
        session = fresh.spawn_for_resume([task], worktree_path=tmp_path, changed_files=[])
    else:
        session = fresh.spawn_for_tasks([task])
    assert session.status == "working"
    adapter.spawn.assert_called_once()


@pytest.mark.parametrize("corrupt", [False, True])
def test_policy_denial_and_corrupt_policy_denial_emit_real_audit_events(
    tmp_path, dispatch_spawner, make_task, monkeypatch, corrupt
):
    from bernstein.core.security.audit import AuditLog

    spawner, adapter = dispatch_spawner()
    _write_policy(tmp_path, {"docs": ["codex"]})
    if corrupt:
        (tmp_path / DEFAULT_POLICY_PATH).write_text('{"docs":')
    log = AuditLog(tmp_path / ".sdd" / "audit", key=b"role-policy-test-key".ljust(32, b"!"))
    monkeypatch.setattr("bernstein.core.security.audit.AuditLog", lambda *a, **kw: log)
    with pytest.raises(RoleAdapterDenied) as exc:
        spawner.spawn_for_resume([make_task(role="docs")], worktree_path=tmp_path, changed_files=[])
    records = log.query(event_type="role.adapter.denied")
    assert len(records) == 1
    record = records[0]
    assert record.resource_type == "adapter"
    assert record.resource_id == "qwen"
    assert record.details["role"] == "docs"
    assert record.details["adapter"] == "qwen"
    expected = "policy file" if corrupt else "not in role allow-list"
    assert expected in record.details["reason"]
    if corrupt:
        assert "policy file" in str(exc.value)
    assert log.verify()[0]
    adapter.spawn.assert_not_called()


@pytest.mark.parametrize("failure", ["constructor", "write"])
def test_audit_failure_does_not_allow_forbidden_resume(tmp_path, dispatch_spawner, make_task, monkeypatch, failure):
    spawner, adapter = dispatch_spawner()
    _write_policy(tmp_path, {"docs": ["codex"]})
    log = MagicMock()
    log.log.side_effect = OSError("fixture audit unavailable")
    constructor = MagicMock(return_value=log)
    if failure == "constructor":
        constructor.side_effect = OSError("fixture audit unavailable")
    monkeypatch.setattr("bernstein.core.security.audit.AuditLog", constructor)
    with pytest.raises(RoleAdapterDenied):
        spawner.spawn_for_resume([make_task(role="docs")], worktree_path=tmp_path, changed_files=[])
    adapter.spawn.assert_not_called()


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

    def test_deny_emits_audit_event(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A denied lookup writes a real, verifiable audit event."""
        from bernstein.core.security.audit import AuditLog

        _write_policy(tmp_path, {"frontend": ["claude"]})
        spawner = _make_spawner(tmp_path)
        spawner._adapter_cache["aider"] = object()
        log = AuditLog(tmp_path / ".sdd" / "audit", key=b"fixture-key".ljust(32, b"!"))
        monkeypatch.setattr("bernstein.core.security.audit.AuditLog", lambda *args, **kwargs: log)

        with pytest.raises(RoleAdapterDenied):
            spawner._get_adapter_by_name("aider", role="frontend")
        records = log.query(event_type="role.adapter.denied")
        assert len(records) == 1
        assert records[0].details == {
            "role": "frontend",
            "adapter": "aider",
            "allowed_adapters": ["claude"],
            "reason": "not in role allow-list",
        }
        assert log.verify()[0]

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


def test_final_dispatch_reloads_policy_after_lookup(tmp_path, dispatch_spawner, make_task, monkeypatch):
    spawner, adapter = dispatch_spawner()
    _write_policy(tmp_path, {"docs": ["qwen"]})
    lookup = spawner._get_adapter_by_name

    def revoke_after_lookup(*args, **kwargs):
        result = lookup(*args, **kwargs)
        _write_policy(tmp_path, {"docs": ["codex"]})
        return result

    monkeypatch.setattr(spawner, "_get_adapter_by_name", revoke_after_lookup)
    with pytest.raises(RoleAdapterDenied):
        spawner.spawn_for_tasks([make_task(role="docs")])
    adapter.spawn.assert_not_called()


def test_workspace_policies_do_not_leak_between_initial_resume_and_failover(
    tmp_path, dispatch_spawner, make_task, mock_adapter_factory, monkeypatch
):
    from types import SimpleNamespace

    from bernstein.core.models import ModelConfig

    from bernstein.adapters.base import RateLimitError
    from bernstein.core.security.role_adapter_policy import RolePolicy, get_policy, set_policy

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    spawner_a, adapter_a = dispatch_spawner(root_a)
    spawner_b, adapter_b = dispatch_spawner(root_b)
    _write_policy(root_a, {"docs": ["qwen"]})
    _write_policy(root_b, {"docs": ["codex"]})
    nested = root_b / ".sdd" / "worktrees" / "old"
    nested.mkdir(parents=True)
    _write_policy(nested, {"docs": ["qwen"]})
    previous = set_policy(RolePolicy.from_dict({"docs": ["unrelated-global-adapter"]}))
    global_policy = get_policy()
    try:
        spawner_a.spawn_for_tasks([make_task(role="docs")])
        spawner_a.spawn_for_resume([make_task(role="docs")], worktree_path=nested, changed_files=[])
        for resume in (False, True):
            with pytest.raises(RoleAdapterDenied):
                if resume:
                    spawner_b.spawn_for_resume([make_task(role="docs")], worktree_path=nested, changed_files=[])
                else:
                    spawner_b.spawn_for_tasks([make_task(role="docs")])
        assert adapter_a.spawn.call_count == 2
        adapter_b.spawn.assert_not_called()

        # Exercise the real failover loop with an inert first adapter.
        config = ModelConfig(model="mock-model", effort="medium")
        router = MagicMock()
        router.state.providers = {
            "primary": SimpleNamespace(health=SimpleNamespace(status=None), available=True),
            "secondary": SimpleNamespace(health=SimpleNamespace(status=None), available=True),
        }
        router.select_provider_for_task.return_value = SimpleNamespace(provider="secondary", model_config=config)
        spawner_a._router = router
        monkeypatch.setattr(spawner_a, "_resolve_routing", lambda *args: (config, "primary", "fixture"))
        monkeypatch.setattr(
            spawner_a,
            "_infer_adapter_name_for_provider",
            lambda provider, model: "codex" if provider == "secondary" else "qwen",
        )
        forbidden = mock_adapter_factory()
        forbidden.name.return_value = "codex"
        spawner_a._adapter_cache["codex"] = forbidden
        adapter_a.spawn.reset_mock()
        adapter_a.spawn.side_effect = RateLimitError("fixture throttle")
        constructor = MagicMock(side_effect=AssertionError("forbidden constructor"))
        monkeypatch.setattr(f"{_SPAWNER}.get_adapter", constructor)
        with pytest.raises(RoleAdapterDenied):
            spawner_a.spawn_for_tasks([make_task(role="docs")])
        adapter_a.spawn.assert_called_once()
        forbidden.spawn.assert_not_called()
        constructor.assert_not_called()
        router.select_provider_for_task.assert_called_once()
        assert get_policy() is global_policy
    finally:
        set_policy(previous)


def test_fresh_process_reads_current_saved_policy(tmp_path):
    import subprocess
    import sys

    _write_policy(tmp_path, {"docs": ["qwen"]})
    script = (
        "from pathlib import Path; "
        "from bernstein.core.security.role_adapter_policy import load_policy_file; "
        "import sys; p = load_policy_file(Path(sys.argv[1])); "
        "assert p.is_allowed('docs', 'qwen'); assert not p.is_allowed('docs', 'codex')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / DEFAULT_POLICY_PATH)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("invalid", ["directory", "mapping", "utf8", "torn", "root", "unreadable"])
def test_unusable_policy_blocks_dispatch_before_effects(
    tmp_path, dispatch_spawner, make_task, monkeypatch, invalid, resume
):
    spawner, adapter = dispatch_spawner()
    path = tmp_path / DEFAULT_POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if invalid == "directory":
        path.mkdir()
    else:
        path.write_bytes(
            {"mapping": b'{"docs": "qwen"}', "utf8": b"\xff", "torn": b'{"docs":', "root": b"[]", "unreadable": b"{}"}[
                invalid
            ]
        )
    original_read = Path.read_text

    def read_policy(file, *args, **kwargs):
        if invalid == "unreadable" and file == path:
            raise PermissionError("fixture permission denied")
        return original_read(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_policy)
    constructor = MagicMock(side_effect=AssertionError("forbidden constructor"))
    monkeypatch.setattr(f"{_SPAWNER}.get_adapter", constructor)
    worktree = MagicMock()
    monkeypatch.setattr(spawner, "_worktree_manager_for_repo", worktree)
    with pytest.raises(RoleAdapterDenied, match="policy file"):
        if resume:
            spawner.spawn_for_resume([make_task(role="docs")], worktree_path=tmp_path, changed_files=[])
        else:
            spawner.spawn_for_tasks([make_task(role="docs")])
    adapter.spawn.assert_not_called()
    constructor.assert_not_called()
    worktree.assert_not_called()


@pytest.mark.parametrize("transport", ["_spawn_in_container", "_spawn_in_sandbox", "_spawn_via_sandbox_session"])
def test_transport_boundaries_refuse_before_backend_effects(tmp_path, dispatch_spawner, transport):
    from types import SimpleNamespace

    from bernstein.core.models import ModelConfig

    spawner, adapter = dispatch_spawner()
    _write_policy(tmp_path, {"docs": ["codex"]})
    backend = MagicMock()
    spawner._container_mgr = backend
    spawner._sandbox = backend
    spawner._sandbox_session = backend
    with pytest.raises(RoleAdapterDenied):
        getattr(spawner, transport)(
            session_id="fixture",
            prompt="fixture",
            spawn_cwd=tmp_path,
            model_config=ModelConfig(model="mock-model", effort="medium"),
            mcp_config=None,
            session=SimpleNamespace(role="docs"),
            adapter=adapter,
        )
    assert backend.mock_calls == []
    adapter.spawn.assert_not_called()


def test_forbidden_provider_chain_is_not_probed(tmp_path, dispatch_spawner, make_task):
    prober = MagicMock(side_effect=AssertionError("forbidden provider probe"))
    spawner, adapter = dispatch_spawner(
        provider_availability={"roles": {"docs": {"chain": [{"adapter": "qwen", "model": "fixture"}]}}},
        availability_prober=prober,
    )
    _write_policy(tmp_path, {"docs": ["codex"]})
    with pytest.raises(RoleAdapterDenied):
        spawner.spawn_for_tasks([make_task(role="docs")])
    prober.assert_not_called()
    adapter.spawn.assert_not_called()


def test_new_cached_adapter_keeps_registry_identity_at_dispatch(tmp_path, dispatch_spawner, monkeypatch):
    from bernstein.core.models import ModelConfig

    from bernstein.adapters.registry import get_adapter

    spawner, _ = dispatch_spawner(name="primary-fixture", enable_caching=True)
    _write_policy(tmp_path, {"docs": ["qwen"]})
    adapter = get_adapter("qwen")
    adapter_spawn = MagicMock()
    monkeypatch.setattr(adapter, "spawn", adapter_spawn)
    monkeypatch.setattr(f"{_SPAWNER}.get_adapter", lambda name: adapter)
    cached = spawner._get_adapter_by_name("qwen", role="docs")
    assert cached is spawner._adapter_cache["qwen"]
    assert cached is not adapter
    spawner._spawn_adapter(
        cached,
        "docs",
        prompt="fixture",
        workdir=tmp_path,
        model_config=ModelConfig(model="mock-model", effort="medium"),
        session_id="fixture",
    )
    adapter_spawn.assert_called_once()


def test_runtime_bridge_refuses_before_remote_effect(tmp_path, dispatch_spawner):
    from types import SimpleNamespace

    from bernstein.core.models import ModelConfig

    spawner, _ = dispatch_spawner()
    _write_policy(tmp_path, {"docs": ["codex"]})
    bridge = MagicMock()
    bridge.name.return_value = "qwen"
    spawner._runtime_bridge = bridge
    with pytest.raises(RoleAdapterDenied):
        spawner._spawn_via_runtime_bridge(
            session=SimpleNamespace(role="docs"),
            prompt="fixture",
            spawn_cwd=tmp_path,
            model_config=ModelConfig(model="mock-model", effort="medium"),
            preferred_log_path=tmp_path / "log",
        )
    bridge.spawn.assert_not_called()
