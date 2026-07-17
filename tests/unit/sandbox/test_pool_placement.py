"""Tests for pool placement selection and sealed placement receipts (#2547).

Covers the determinism criterion (two hosts agree on the placement tuple) and
the verifiability criterion (a forged backend or widened effective manifest
breaks receipt verification).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.sandbox.pool import PoolManifest, PoolWorkspaceTemplate, merge_pool_overrides
from bernstein.core.sandbox.pool_placement import (
    seal_placement,
    select_pool_backend,
    verify_placement_receipt,
    write_placement_receipt,
)

_BASE_CAPS = frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC})


@dataclass(frozen=True)
class StubBackend:
    name: str
    capabilities: frozenset[SandboxCapability] = _BASE_CAPS

    async def create(self, manifest: Any, options: Any = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def resume(self, snapshot_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def destroy(self, session: Any) -> None:  # pragma: no cover
        raise NotImplementedError


def _pool(**overrides) -> PoolManifest:
    kwargs = dict(
        name="ci-linux",
        backend_allowlist=("worktree", "docker"),
        template=PoolWorkspaceTemplate(env={"FOO": "bar"}),
        exposed_fields=("env", "backend"),
        capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC}),
    )
    kwargs.update(overrides)
    return PoolManifest(**kwargs)


class TestSelection:
    def test_selects_first_allowlisted_backend(self):
        pool = _pool()
        merge = merge_pool_overrides(pool, {})
        backends = [StubBackend("docker"), StubBackend("worktree"), StubBackend("e2b")]
        chosen, inputs = select_pool_backend(backends, merge, pool=pool)
        assert chosen.name == "worktree"  # allowlist order wins
        assert "e2b" not in inputs["candidates"]  # filtered out (not allowlisted)

    def test_backend_override_wins(self):
        pool = _pool()
        merge = merge_pool_overrides(pool, {"backend": "docker"})
        backends = [StubBackend("worktree"), StubBackend("docker")]
        chosen, _ = select_pool_backend(backends, merge, pool=pool)
        assert chosen.name == "docker"

    def test_two_hosts_agree_on_backend(self):
        pool = _pool()
        merge = merge_pool_overrides(pool, {"env": {"X": "1"}})
        backends_a = [StubBackend("docker"), StubBackend("worktree")]
        backends_b = [StubBackend("worktree"), StubBackend("docker")]
        chosen_a, _ = select_pool_backend(backends_a, merge, pool=pool)
        chosen_b, _ = select_pool_backend(backends_b, merge, pool=pool)
        assert chosen_a.name == chosen_b.name


class TestPlacementReceipt:
    def test_seal_is_deterministic(self):
        pool = _pool()
        merge = merge_pool_overrides(pool, {"env": {"X": "1"}})
        inputs = {"pool_hash": merge.pool_hash, "candidates": ["worktree"]}
        a = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs=inputs, timestamp=1700000000)
        b = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs=inputs, timestamp=1700000000)
        assert a.placement_hash == b.placement_hash
        # AC: placement receipts agree on (pool_hash, effective_manifest_hash, chosen_backend)
        assert (a.pool_hash, a.effective_manifest_hash, a.chosen_backend) == (
            b.pool_hash,
            b.effective_manifest_hash,
            b.chosen_backend,
        )

    def test_self_hash_verifies(self):
        pool = _pool()
        merge = merge_pool_overrides(pool, {})
        r = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs={}, timestamp=1)
        assert r.verify_self_hash()

    def test_forged_backend_breaks_self_hash(self):
        pool = _pool()
        merge = merge_pool_overrides(pool, {})
        r = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs={}, timestamp=1)
        forged = replace(r, chosen_backend="e2b")  # placement_hash unchanged -> mismatch
        assert not forged.verify_self_hash()

    def test_widened_effective_manifest_breaks_self_hash(self):
        pool = _pool()
        merge = merge_pool_overrides(pool, {})
        r = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs={}, timestamp=1)
        forged = replace(r, effective_manifest_hash="0" * 64)
        assert not forged.verify_self_hash()

    def test_on_disk_verify_roundtrip(self, tmp_path):
        pool = _pool()
        merge = merge_pool_overrides(pool, {})
        r = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs={}, timestamp=1)
        write_placement_receipt(tmp_path, r)
        result = verify_placement_receipt(tmp_path, r.placement_hash)
        assert result.ok

    def test_on_disk_tamper_detected(self, tmp_path):
        pool = _pool()
        merge = merge_pool_overrides(pool, {})
        r = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs={}, timestamp=1)
        path = write_placement_receipt(tmp_path, r)
        text = path.read_text().replace('"chosen_backend":"worktree"', '"chosen_backend":"e2b"')
        path.write_text(text)
        result = verify_placement_receipt(tmp_path, r.placement_hash)
        assert not result.ok
        assert "tampered" in result.reason

    def test_missing_receipt_reports_absent(self, tmp_path):
        result = verify_placement_receipt(tmp_path, "a" * 64)
        assert not result.ok


class TestRegression:
    def test_selector_untouched_without_pool(self):
        """With no pool, select_sandbox behaves exactly as today (import parity)."""
        from bernstein.core.sandbox.selector import SandboxPolicy, select_sandbox

        backends = [StubBackend("worktree"), StubBackend("docker")]
        chosen = select_sandbox(backends, policy=SandboxPolicy())
        assert chosen.name == "worktree"
