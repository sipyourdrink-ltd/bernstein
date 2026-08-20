"""Admission subsystem: named pools, leases, postures, quarantine (#2544).

One test per acceptance criterion, plus the determinism property test that
drives randomized claim/release/renew/crash interleavings through the pure
projection and asserts byte-identical state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.core.admission import (
    AdmissionEngine,
    AdmissionState,
    Posture,
    verify_admission_ledger,
)
from bernstein.core.admission.interop import spawn_throttles_as_named_limits
from bernstein.core.admission.ledger import (
    admission_ledger_dir,
    open_admission_reader,
)
from bernstein.core.admission.rate_limit import effective_rate_limit
from bernstein.core.admission.receipts import verify_receipt as verify_admission_receipt


def _fixed_keypair() -> tuple[str, str]:
    from bernstein.core.skills.catalog.signature import generate_signer_keypair

    return generate_signer_keypair()


def _engine(tmp_path: Path, **kw) -> AdmissionEngine:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    priv, pub = _fixed_keypair()
    return AdmissionEngine(sdd_dir=sdd, private_key_pem=priv, public_key_pem=pub, **kw)


# ---------------------------------------------------------------------------
# AC1: determinism -- two processes derive byte-identical grant order
# ---------------------------------------------------------------------------


def test_named_pool_admits_exactly_the_slot_count(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("staging-env", 1)
    first = eng.request_grant(pool="staging-env", task_id="T1", worker_id="w1", now=1)
    second = eng.request_grant(pool="staging-env", task_id="T2", worker_id="w2", now=2)
    assert first.admitted is True
    assert second.admitted is False
    assert "capacity" in second.reason
    assert eng.state().pool_occupancy("staging-env") == 1


def test_projection_is_a_pure_function_of_the_chain(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 2)
    g1 = eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1, ttl_s=10).grant_id
    eng.request_grant(pool="p", task_id="b", worker_id="w2", now=2, ttl_s=10)
    eng.renew(g1, now=5)
    eng.release(g1, now=6)

    reader = open_admission_reader(eng.sdd_dir)
    one = AdmissionState.from_rows(reader.entries()).to_canonical_json()
    two = AdmissionState.from_rows(reader.entries()).to_canonical_json()
    assert one == two


def test_two_processes_derive_byte_identical_grant_order(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 3)
    for i in range(6):
        eng.request_grant(pool="p", task_id=f"t{i}", worker_id=f"w{i}", now=i, ttl_s=100)
        if i % 2 == 0:
            eng.release(eng.state().grant_order[-1], now=i + 100)

    ledger_dir = admission_ledger_dir(eng.sdd_dir)
    program = (
        "import json;"
        "from pathlib import Path;"
        "from bernstein.core.persistence.work_ledger import LedgerReader;"
        "from bernstein.core.admission import AdmissionState;"
        f"r=LedgerReader(Path({str(ledger_dir)!r}));"
        "print(json.dumps(AdmissionState.from_rows(r.entries()).grant_order))"
    )
    src_root = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": src_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc_a = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, env=env, check=True)
    proc_b = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, env=env, check=True)
    order_a = json.loads(proc_a.stdout)
    order_b = json.loads(proc_b.stdout)
    assert order_a == order_b
    assert order_a == eng.state().grant_order


_OP = st.sampled_from(["grant", "release", "renew", "sweep"])


@settings(derandomize=True, max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=st.lists(st.tuples(_OP, st.integers(min_value=0, max_value=6)), min_size=1, max_size=40))
def test_randomized_interleavings_project_identically(tmp_path_factory: pytest.TempPathFactory, ops: list) -> None:
    base = tmp_path_factory.mktemp("adm")
    eng = _engine(base)
    eng.set_pool("p", 2)
    counter = 0
    for clock, (op, arg) in enumerate(ops, start=1):
        state = eng.state()
        if op == "grant":
            counter += 1
            eng.request_grant(pool="p", task_id=f"t{counter}", worker_id=f"w{arg}", now=clock, ttl_s=5)
        elif op == "release" and state.active_grants:
            eng.release(sorted(state.active_grants)[arg % len(state.active_grants)], now=clock)
        elif op == "renew" and state.active_grants:
            gid = sorted(state.active_grants)[arg % len(state.active_grants)]
            # A late heartbeat can no longer revive an expired lease; only renew
            # a still-live grant (the engine rejects the rest).
            if not state.active_grants[gid].is_expired(clock):
                eng.renew(gid, now=clock)
        elif op == "sweep":
            eng.sweep_expired(now=clock + 10)

    reader = open_admission_reader(eng.sdd_dir)
    one = AdmissionState.from_rows(reader.entries()).to_canonical_json()
    two = AdmissionState.from_rows(reader.entries()).to_canonical_json()
    assert one == two
    # Occupancy never exceeds capacity under enforce.
    assert AdmissionState.from_rows(open_admission_reader(eng.sdd_dir).entries()).pool_occupancy("p") <= 2


# ---------------------------------------------------------------------------
# AC2: verifiability -- recompute from genesis; tamper is named; offline query
# ---------------------------------------------------------------------------


def test_verify_passes_on_a_sound_ledger(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1)
    result = eng.verify()
    assert result.ok is True
    assert result.entries == 2


def test_mutating_a_grant_row_payload_fails_verification_at_its_position(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1)

    bucket = admission_ledger_dir(eng.sdd_dir) / "000000.jsonl"
    lines = bucket.read_text().splitlines()
    row = json.loads(lines[1])
    row["payload"]["worker_id"] = "attacker"  # mutate without recomputing the hash
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    bucket.write_text("\n".join(lines) + "\n")

    result = verify_admission_ledger(open_admission_reader(eng.sdd_dir))
    assert result.ok is False
    assert any("entry 1" in e for e in result.errors)


def test_swapping_two_grants_fails_verification(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 2)
    eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1)
    eng.request_grant(pool="p", task_id="b", worker_id="w2", now=2)

    bucket = admission_ledger_dir(eng.sdd_dir) / "000000.jsonl"
    lines = bucket.read_text().splitlines()
    lines[1], lines[2] = lines[2], lines[1]  # swap the two grant rows
    bucket.write_text("\n".join(lines) + "\n")

    result = verify_admission_ledger(open_admission_reader(eng.sdd_dir))
    assert result.ok is False
    assert result.errors  # the exact position is named by the chain walker


def test_who_held_pool_is_answerable_offline(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("staging-env", 1)
    eng.request_grant(pool="staging-env", task_id="a", worker_id="alice", now=100, ttl_s=1000)
    eng.release(eng.state().grant_order[-1], now=150)
    eng.request_grant(pool="staging-env", task_id="b", worker_id="bob", now=160, ttl_s=1000)

    state = AdmissionState.from_rows(open_admission_reader(eng.sdd_dir).entries())
    assert [iv.worker_id for iv in state.who_held("staging-env", 120)] == ["alice"]
    assert [iv.worker_id for iv in state.who_held("staging-env", 155)] == []
    assert [iv.worker_id for iv in state.who_held("staging-env", 170)] == ["bob"]


# ---------------------------------------------------------------------------
# AC3: SIGKILL'd holder loses lease -> signed escalation + resume + reference
# ---------------------------------------------------------------------------


def test_lease_expiry_emits_signed_receipt_resume_and_references_expiry(tmp_path: Path) -> None:
    from bernstein.core.orchestration.supervisor_receipt import verify_receipt as verify_escalation
    from bernstein.core.tasks.checkpoint_retry import record_task_checkpoint

    eng = _engine(tmp_path)
    eng.set_pool("staging-env", 1)
    # A fork-capable adapter with a checkpoint so the resume is warm, not cold.
    record_task_checkpoint(
        sdd_dir=eng.sdd_dir,
        task_id="T1",
        adapter="claude",
        session_id="sess-1",
        workspace_hash="deadbeef",
        worktree_path=str(tmp_path),
    )
    eng.request_grant(pool="staging-env", task_id="T1", worker_id="w1", now=100, ttl_s=50)

    # No renewal (the holder was SIGKILL'd); sweep past the TTL.
    outcomes = eng.sweep_expired(now=200)
    assert len(outcomes) == 1
    outcome = outcomes[0]

    # Signed escalation receipt in the supervisor-receipt shape.
    assert outcome.receipt.is_signed
    from cryptography.hazmat.primitives import serialization

    pub = serialization.load_pem_public_key(eng.public_key_pem.encode("ascii"))
    assert verify_escalation(outcome.receipt, pub).ok is True

    # Resume via checkpointed retry (warm, not a silent cold wipe).
    assert str(outcome.retry_decision.effective_mode) == "warm"

    # The next grant for the freed slot references the expiry row.
    nxt = eng.request_grant(pool="staging-env", task_id="T2", worker_id="w2", now=201, ttl_s=50)
    assert nxt.admitted is True
    freed_by = eng.state().active_grants[nxt.grant_id].slot_freed_by
    assert freed_by == outcome.expire_hash


def test_a_renewed_lease_does_not_expire(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    grant = eng.request_grant(pool="p", task_id="T1", worker_id="w1", now=100, ttl_s=50)  # expiry 150
    eng.renew(grant.grant_id, now=140)  # heartbeat renewal before expiry -> new expiry 190
    assert eng.sweep_expired(now=180) == []  # 140 + 50 = 190 > 180
    assert eng.sweep_expired(now=200) != []  # now past the renewed expiry


# ---------------------------------------------------------------------------
# AC4: advise posture never blocks; waivers == over-limit passes
# ---------------------------------------------------------------------------


def test_advise_posture_never_blocks_and_waivers_equal_over_limit_passes(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("soft", 1, posture=Posture.ADVISE)
    over_limit_passes = 0
    for i in range(8):
        dec = eng.request_grant(pool="soft", task_id=f"t{i}", worker_id=f"w{i}", now=i)
        assert dec.admitted is True  # advise never blocks
        if dec.over_limit:
            over_limit_passes += 1
            assert dec.waiver is not None
            ok, reason = verify_admission_receipt(dec.waiver, public_key_pem=eng.public_key_pem)
            assert ok, reason

    assert over_limit_passes == 7  # capacity 1 => 7 of 8 are over-limit
    assert eng.state().waivers == over_limit_passes


# ---------------------------------------------------------------------------
# AC5: quarantine limit=0 -> freeze + one chain entry with affected manifest
# ---------------------------------------------------------------------------


def test_quarantine_freezes_and_emits_one_manifest_reproducible_on_replay(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_tag_limit("migration-lock", 3)
    eng.request_grant(pool="", task_id="a", worker_id="w1", now=1, tags=("migration-lock",))
    eng.request_grant(pool="", task_id="b", worker_id="w2", now=2, tags=("migration-lock",))

    result = eng.quarantine(target_kind="tag", target="migration-lock", now=10, queued_tasks=("c", "d"))
    manifest = result.manifest
    assert {w["task_id"] for w in manifest["checkpointed"]} == {"a", "b"}
    assert manifest["queued_tasks"] == ["c", "d"]

    # New admission for the class is now frozen (limit 0).
    blocked = eng.request_grant(pool="", task_id="e", worker_id="w3", now=11, tags=("migration-lock",))
    assert blocked.admitted is False

    # Replaying the chain reproduces the identical manifest.
    state = AdmissionState.from_rows(open_admission_reader(eng.sdd_dir).entries())
    assert len(state.quarantines) == 1
    replayed = state.quarantines[0]
    assert replayed["checkpointed"] == manifest["checkpointed"]
    assert replayed["queued_tasks"] == manifest["queued_tasks"]


# ---------------------------------------------------------------------------
# AC6: tag-conformance -- docs-only that touched src/ gets a signed violation
# ---------------------------------------------------------------------------


def test_docs_only_touching_src_gets_signed_violation_receipt(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    seal = eng.seal_tag_conformance(
        task_id="T1",
        worker_id="w1",
        declared_tags=("docs-only",),
        changed_paths=("src/foo.py", "docs/guide.md"),
    )
    assert seal.conformant is False
    assert seal.violations[0].tag == "docs-only"
    assert "src/foo.py" in seal.violations[0].offending_paths
    ok, reason = verify_admission_receipt(seal, public_key_pem=eng.public_key_pem)
    assert ok, reason


def test_conformant_docs_only_seals_clean(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    seal = eng.seal_tag_conformance(
        task_id="T2",
        worker_id="w2",
        declared_tags=("docs-only",),
        changed_paths=("docs/guide.md", "README.md"),
    )
    assert seal.conformant is True
    assert seal.violations == ()


# ---------------------------------------------------------------------------
# AC7: provider spawn throttles as named limits -- no behavior regression
# ---------------------------------------------------------------------------


def test_spawn_throttles_expressed_as_named_limits_no_regression() -> None:
    from bernstein.core.agents.spawn_rate_limiter import SpawnRateLimitConfig

    config = SpawnRateLimitConfig(max_spawns=2, per_provider_overrides={"anthropic": 5})
    specs = spawn_throttles_as_named_limits(config, providers=("openai",))
    by_name = {s.name: s for s in specs}

    # The named limit's ceiling equals the throttle's per-provider max_spawns.
    assert by_name["spawn-anthropic"].base_limit == 5
    assert by_name["spawn-default"].base_limit == 2
    assert by_name["spawn-openai"].base_limit == 2
    # With zero recorded 429s the effective limit equals the base (no change).
    assert effective_rate_limit(by_name["spawn-anthropic"].base_limit, [], now=0) == 5


def test_rate_limit_adaptive_decay_is_deterministic() -> None:
    # Each recent 429 halves the limit down to the floor; observations age out.
    assert effective_rate_limit(8, [100], now=200, floor=1) == 4
    assert effective_rate_limit(8, [100, 150], now=200, floor=1) == 2
    # Observation older than the recovery window (300s) no longer bites.
    assert effective_rate_limit(8, [100], now=500, floor=1) == 8
