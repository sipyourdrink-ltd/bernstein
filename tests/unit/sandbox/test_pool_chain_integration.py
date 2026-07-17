"""Chain-level integrity tests for pool receipts (#2547).

These use a real :class:`AuditChainStore` with an isolated HMAC key to prove the
substrate coupling: a placement receipt mirrored into the chain makes flipping
one byte of the recorded effective manifest break ``chain.verify()`` -- exactly
the tamper test the acceptance criteria demand.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.sandbox.pool import PoolManifest, PoolWorkspaceTemplate, merge_pool_overrides
from bernstein.core.sandbox.pool_placement import record_placement, seal_placement
from bernstein.core.sandbox.pool_warm import WarmSlotKey, record_quarantine
from bernstein.core.security.audit_chain import (
    AuditChainStore,
    record_pool_override_refused,
    record_pool_registered,
    record_pool_worker_enrolled,
)

_ISOLATED_KEY = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
def chain(tmp_path):
    return AuditChainStore(tmp_path / "audit", key=_ISOLATED_KEY)


def _pool() -> PoolManifest:
    return PoolManifest(
        name="ci-linux",
        backend_allowlist=("worktree", "docker"),
        template=PoolWorkspaceTemplate(env={"FOO": "bar"}),
        exposed_fields=("env",),
        capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC}),
    )


class TestPlacementChainTamper:
    def test_placement_recorded_and_chain_verifies(self, chain):
        pool = _pool()
        merge = merge_pool_overrides(pool, {"env": {"X": "1"}})
        receipt = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs={"k": "v"}, timestamp=1)
        record_placement(chain=chain, receipt=receipt)
        ok, errors = chain.verify()
        assert ok, errors

    def test_flipping_effective_manifest_byte_breaks_chain(self, chain, tmp_path):
        pool = _pool()
        merge = merge_pool_overrides(pool, {"env": {"X": "1"}})
        receipt = seal_placement(merge=merge, chosen_backend="worktree", selector_inputs={"k": "v"}, timestamp=1)
        record_placement(chain=chain, receipt=receipt)

        # Tamper with the recorded effective manifest hash in the JSONL, as an
        # auditor investigating a widened-egress dispute would detect.
        audit_dir = tmp_path / "audit"
        jsonl = next(audit_dir.glob("*.jsonl"))
        lines = jsonl.read_text().splitlines()
        mutated = []
        for line in lines:
            entry = json.loads(line)
            details = entry.get("details", {})
            if "effective_manifest_hash" in details:
                details["effective_manifest_hash"] = "0" * 64
            mutated.append(json.dumps(entry))
        jsonl.write_text("\n".join(mutated) + "\n")

        reopened = AuditChainStore(audit_dir, key=_ISOLATED_KEY)
        ok, errors = reopened.verify()
        assert not ok
        assert errors


class TestOtherPoolEvents:
    def test_register_enrol_refuse_quarantine_chain_verifies(self, chain):
        pool = _pool()
        record_pool_registered(chain=chain, pool_name=pool.name, pool_hash=pool.pool_hash)
        record_pool_worker_enrolled(
            chain=chain,
            pool_hash=pool.pool_hash,
            worker_name="node-1",
            keyid="kid-abc",
            enrolment_hash="e" * 64,
            signature="sig",
        )
        record_pool_override_refused(
            chain=chain,
            pool_hash=pool.pool_hash,
            reason="egress_widened",
            refused_field="network_egress_class",
            overrides_hash="f" * 64,
            author="agent",
        )
        slot = WarmSlotKey(slot_id="s1", pool_hash=pool.pool_hash, effective_manifest_hash="d" * 64)
        record_quarantine(chain=chain, slot=slot, dispatch_effective_hash="e" * 64)
        ok, errors = chain.verify()
        assert ok, errors

    def test_enrolled_event_names_worker_keyid(self, chain):
        pool = _pool()
        record_pool_worker_enrolled(
            chain=chain,
            pool_hash=pool.pool_hash,
            worker_name="node-1",
            keyid="kid-xyz",
            enrolment_hash="e" * 64,
            signature="sig",
        )
        events = chain.query(event_type="pool.worker_enrolled")
        assert events
        assert events[-1].details["keyid"] == "kid-xyz"
        assert events[-1].details["pool_hash"] == pool.pool_hash
