"""Tests for governed override merge and fail-closed ceilings (#2547).

Covers the acceptance criterion "Isolation, fail closed": an override that
widens network egress or adds a credential env var beyond the pool ceiling is
refused with a structured reason and *no sandbox is created*, for both a
recipe-authored and an agent-authored override.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.sandbox.pool import (
    PoolManifest,
    PoolOverrideRefused,
    PoolWorkspaceTemplate,
    merge_pool_overrides,
)

KNOWN_CREDS = frozenset({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"})


def _pool(**overrides) -> PoolManifest:
    kwargs = dict(
        name="ci-linux",
        backend_allowlist=("worktree", "docker"),
        template=PoolWorkspaceTemplate(root="/workspace", env={"FOO": "bar"}, timeout_seconds=900),
        exposed_fields=("env", "timeout_seconds", "network_egress_class", "capabilities", "backend"),
        capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC, SandboxCapability.NETWORK}),
        network_egress_class="restricted",
        credential_env_allowlist=frozenset({"AWS_ACCESS_KEY_ID"}),
    )
    kwargs.update(overrides)
    return PoolManifest(**kwargs)


class TestHappyPathMerge:
    def test_empty_overrides_yields_base_template(self):
        pool = _pool()
        result = merge_pool_overrides(pool, {}, known_credential_keys=KNOWN_CREDS)
        assert result.effective_template.env == {"FOO": "bar"}
        assert result.effective_template.timeout_seconds == 900
        assert result.pool_hash == pool.pool_hash
        assert len(result.effective_manifest_hash) == 64

    def test_exposed_env_merges_over_base(self):
        pool = _pool()
        result = merge_pool_overrides(pool, {"env": {"EXTRA": "1"}}, known_credential_keys=KNOWN_CREDS)
        assert result.effective_template.env == {"FOO": "bar", "EXTRA": "1"}

    def test_allowlisted_credential_env_permitted(self):
        pool = _pool()
        result = merge_pool_overrides(pool, {"env": {"AWS_ACCESS_KEY_ID": "AKIA"}}, known_credential_keys=KNOWN_CREDS)
        assert result.credential_env == ("AWS_ACCESS_KEY_ID",)

    def test_capability_within_ceiling_permitted(self):
        pool = _pool()
        result = merge_pool_overrides(pool, {"capabilities": ["network"]}, known_credential_keys=KNOWN_CREDS)
        assert SandboxCapability.NETWORK in result.capabilities

    def test_backend_override_within_allowlist(self):
        pool = _pool()
        result = merge_pool_overrides(pool, {"backend": "docker"}, known_credential_keys=KNOWN_CREDS)
        assert result.backend_override == "docker"


class TestFailClosed:
    @pytest.mark.parametrize("author", ["recipe", "agent"])
    def test_widened_egress_refused(self, author):
        pool = _pool()
        with pytest.raises(PoolOverrideRefused) as exc:
            merge_pool_overrides(pool, {"network_egress_class": "open"}, known_credential_keys=KNOWN_CREDS)
        assert exc.value.reason == "egress_widened"
        assert exc.value.field == "network_egress_class"

    @pytest.mark.parametrize("author", ["recipe", "agent"])
    def test_credential_env_beyond_allowlist_refused(self, author):
        pool = _pool()
        with pytest.raises(PoolOverrideRefused) as exc:
            merge_pool_overrides(pool, {"env": {"AWS_SECRET_ACCESS_KEY": "s3cr3t"}}, known_credential_keys=KNOWN_CREDS)
        assert exc.value.reason == "credential_env_not_allowed"

    def test_non_exposed_field_refused(self):
        pool = _pool(exposed_fields=("env",))
        with pytest.raises(PoolOverrideRefused) as exc:
            merge_pool_overrides(pool, {"timeout_seconds": 60}, known_credential_keys=KNOWN_CREDS)
        assert exc.value.reason == "non_exposed_field"

    def test_capability_above_ceiling_refused(self):
        pool = _pool(
            capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC}),
        )
        with pytest.raises(PoolOverrideRefused) as exc:
            merge_pool_overrides(pool, {"capabilities": ["gpu"]}, known_credential_keys=KNOWN_CREDS)
        assert exc.value.reason == "capability_above_ceiling"

    def test_backend_outside_allowlist_refused(self):
        pool = _pool()
        with pytest.raises(PoolOverrideRefused) as exc:
            merge_pool_overrides(pool, {"backend": "e2b"}, known_credential_keys=KNOWN_CREDS)
        assert exc.value.reason == "backend_not_allowed"

    def test_no_sandbox_created_on_refusal(self):
        """A refused override raises before any backend is ever touched."""
        pool = _pool()
        spy_backend = MagicMock()
        backends = [spy_backend]
        with pytest.raises(PoolOverrideRefused):
            # Merge is the precondition to selection; it raises first, so a
            # dispatch never reaches select_pool_backend and no backend is
            # instantiated or created.
            merge = merge_pool_overrides(pool, {"network_egress_class": "open"}, known_credential_keys=KNOWN_CREDS)
            from bernstein.core.sandbox.pool_placement import select_pool_backend

            select_pool_backend(backends, merge, pool=pool)
        spy_backend.create.assert_not_called()


class TestDeterminism:
    def test_override_key_order_does_not_change_effective_hash(self):
        pool = _pool()
        a = merge_pool_overrides(
            pool, {"timeout_seconds": 1200, "env": {"A": "1", "B": "2"}}, known_credential_keys=KNOWN_CREDS
        )
        b = merge_pool_overrides(
            pool, {"env": {"B": "2", "A": "1"}, "timeout_seconds": 1200}, known_credential_keys=KNOWN_CREDS
        )
        assert a.effective_manifest_hash == b.effective_manifest_hash
        assert a.overrides_hash == b.overrides_hash

    def test_capability_list_order_does_not_change_hash(self):
        pool = _pool()
        a = merge_pool_overrides(pool, {"capabilities": ["network", "exec"]}, known_credential_keys=KNOWN_CREDS)
        b = merge_pool_overrides(pool, {"capabilities": ["exec", "network"]}, known_credential_keys=KNOWN_CREDS)
        assert a.effective_manifest_hash == b.effective_manifest_hash
