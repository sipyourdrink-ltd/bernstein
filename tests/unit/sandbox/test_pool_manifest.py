"""Tests for the frozen, chain-projected PoolManifest (#2547)."""

from __future__ import annotations

import pytest

from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.sandbox.pool import (
    BASE_CAPABILITIES,
    PoolManifest,
    PoolManifestError,
    PoolWorkspaceTemplate,
)


def _pool(**overrides) -> PoolManifest:
    kwargs = dict(
        name="ci-linux",
        backend_allowlist=("worktree", "docker"),
        template=PoolWorkspaceTemplate(root="/workspace", env={"FOO": "bar"}, timeout_seconds=900),
        exposed_fields=("env", "timeout_seconds"),
        capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC, SandboxCapability.NETWORK}),
        network_egress_class="restricted",
        credential_env_allowlist=frozenset({"AWS_ACCESS_KEY_ID"}),
    )
    kwargs.update(overrides)
    return PoolManifest(**kwargs)


class TestPoolHash:
    def test_pool_hash_is_64_hex(self):
        pool = _pool()
        assert len(pool.pool_hash) == 64
        assert all(c in "0123456789abcdef" for c in pool.pool_hash)

    def test_hash_is_stable_across_constructions(self):
        assert _pool().pool_hash == _pool().pool_hash

    def test_env_key_order_does_not_change_hash(self):
        a = _pool(template=PoolWorkspaceTemplate(env={"A": "1", "B": "2"}))
        b = _pool(template=PoolWorkspaceTemplate(env={"B": "2", "A": "1"}))
        assert a.pool_hash == b.pool_hash

    def test_different_name_changes_hash(self):
        assert _pool(name="ci-linux").pool_hash != _pool(name="ci-mac").pool_hash

    def test_capability_set_order_does_not_change_hash(self):
        a = _pool(capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC}))
        b = _pool(capability_ceiling=frozenset({SandboxCapability.EXEC, SandboxCapability.FILE_RW}))
        assert a.pool_hash == b.pool_hash


class TestValidation:
    def test_empty_name_rejected(self):
        with pytest.raises(PoolManifestError):
            _pool(name="")

    def test_unknown_exposed_field_rejected(self):
        with pytest.raises(PoolManifestError):
            _pool(exposed_fields=("env", "not_a_field"))

    def test_bad_egress_class_rejected(self):
        with pytest.raises(PoolManifestError):
            _pool(network_egress_class="firehose")

    def test_ceiling_missing_base_capability_rejected(self):
        with pytest.raises(PoolManifestError):
            _pool(capability_ceiling=frozenset({SandboxCapability.FILE_RW}))

    def test_wrong_pool_hash_rejected(self):
        with pytest.raises(PoolManifestError):
            PoolManifest(name="ci", pool_hash="0" * 64)

    def test_base_capabilities_are_file_rw_and_exec(self):
        expected = frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC})
        assert expected == BASE_CAPABILITIES


class TestRoundTrip:
    def test_to_dict_from_dict_preserves_hash(self):
        pool = _pool()
        restored = PoolManifest.from_dict(pool.to_dict())
        assert restored.pool_hash == pool.pool_hash
        assert restored.canonical_json() == pool.canonical_json()

    def test_from_dict_recomputes_absent_hash(self):
        pool = _pool()
        spec = pool.to_dict()
        spec.pop("pool_hash")
        restored = PoolManifest.from_dict(spec)
        assert restored.pool_hash == pool.pool_hash
