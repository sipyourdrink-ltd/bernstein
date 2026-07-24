"""Admission engine / ledger / tags / verifier hardening (#2650).

One regression test per acceptance criterion. These lock in the security and
integrity fixes over the pool/lease-based admission subsystem: the verifier
must replay the full ENFORCE predicate (not just pool capacity), the install
identity must not silently fail open, path/tag inputs must reject traversal,
and lease/renew/waiver semantics must be exact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.admission import (
    AdmissionEngine,
    AdmissionState,
    Posture,
    verify_admission_ledger,
)
from bernstein.core.admission.ledger import (
    KIND_GRANT,
    KIND_RELEASE,
    open_admission_ledger,
    open_admission_reader,
)


def _fixed_keypair() -> tuple[str, str]:
    from bernstein.core.skills.catalog.signature import generate_signer_keypair

    return generate_signer_keypair()


def _engine(tmp_path: Path, **kw) -> AdmissionEngine:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    priv, pub = _fixed_keypair()
    return AdmissionEngine(sdd_dir=sdd, private_key_pem=priv, public_key_pem=pub, **kw)


def _forge_grant(
    eng: AdmissionEngine,
    *,
    task_id: str,
    pool: str,
    worker_id: str,
    ts: int,
    tags: tuple[str, ...] = (),
    over_limit: bool = False,
) -> str:
    """Append a raw, hash-valid grant row the engine would never have issued."""
    ledger = open_admission_ledger(eng.sdd_dir)
    entry = ledger.append(
        kind=KIND_GRANT,
        task_id=task_id,
        payload={
            "over_limit": over_limit,
            "pool": pool,
            "slot_freed_by": "",
            "tags": list(tags),
            "task_id": task_id,
            "ttl_s": 0,
            "ts": ts,
            "worker_id": worker_id,
        },
    )
    ledger.close()
    return entry.entry_hash


# ---------------------------------------------------------------------------
# Item 1: install-identity keystore failure must not fail open
# ---------------------------------------------------------------------------


def test_install_keypair_propagates_keystore_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.admission import engine as engine_mod
    from bernstein.core.security import agent_card_keystore as ks

    def unsafe_perms(_self: object) -> tuple[bytes, bytes]:
        raise PermissionError("agent-card private key has unsafe permissions")

    monkeypatch.setattr(ks.AgentCardKeystore, "load_or_generate", unsafe_perms)
    # A real keystore failure is a security signal: it must propagate, not be
    # papered over with an unpinnable ephemeral identity.
    with pytest.raises(PermissionError):
        engine_mod._install_keypair()


def test_install_keypair_falls_back_only_when_keystore_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.admission import engine as engine_mod
    from bernstein.core.security import agent_card_keystore as ks

    def unavailable(_self: object) -> tuple[bytes, bytes]:
        raise ImportError("keystore dependency not installed")

    monkeypatch.setattr(ks.AgentCardKeystore, "load_or_generate", unavailable)
    priv, pub = engine_mod._install_keypair()
    assert "PRIVATE KEY" in priv
    assert "PUBLIC KEY" in pub


# ---------------------------------------------------------------------------
# Item 2: a partial keypair must be rejected (never mixed with a generated one)
# ---------------------------------------------------------------------------


def test_partial_keypair_is_rejected(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    priv, pub = _fixed_keypair()
    with pytest.raises(ValueError, match="both|partial|neither"):
        AdmissionEngine(sdd_dir=sdd, private_key_pem=priv, public_key_pem="")
    with pytest.raises(ValueError, match="both|partial|neither"):
        AdmissionEngine(sdd_dir=sdd, private_key_pem="", public_key_pem=pub)


def test_absent_keypair_generates_a_matched_pair(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    eng = AdmissionEngine(sdd_dir=sdd)
    assert eng.private_key_pem and eng.public_key_pem
    # The generated pair must actually match: a signed receipt verifies.
    from bernstein.core.admission.receipts import verify_receipt

    seal = eng.seal_tag_conformance(
        task_id="T1", worker_id="w1", declared_tags=("docs-only",), changed_paths=("docs/x.md",)
    )
    ok, reason = verify_receipt(seal, public_key_pem=eng.public_key_pem)
    assert ok, reason


# ---------------------------------------------------------------------------
# Item 3: verifier soundness is not bypassable
# ---------------------------------------------------------------------------


def test_forged_over_limit_flag_cannot_bypass_full_enforce_pool(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1)  # pool now full
    _forge_grant(eng, task_id="b", pool="p", worker_id="w2", ts=2, over_limit=True)
    result = verify_admission_ledger(open_admission_reader(eng.sdd_dir))
    assert result.ok is False
    assert any("capacity" in e for e in result.errors)


def test_grant_on_undeclared_pool_fails_verification(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    _forge_grant(eng, task_id="x", pool="ghost", worker_id="w", ts=1)
    result = verify_admission_ledger(open_admission_reader(eng.sdd_dir))
    assert result.ok is False
    assert any("ghost" in e for e in result.errors)


def test_repeated_terminal_row_cannot_undercount_occupancy(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    a = eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1).grant_id
    eng.release(a, now=2)
    eng.request_grant(pool="p", task_id="b", worker_id="w2", now=3)  # legit: reuses the freed slot
    # Forge a duplicate release of the already-released grant, then a grant that
    # would only pass if the extra release wrongly freed a second slot.
    ledger = open_admission_ledger(eng.sdd_dir)
    ledger.append(kind=KIND_RELEASE, payload={"grant": a, "ts": 4})
    ledger.close()
    _forge_grant(eng, task_id="c", pool="p", worker_id="w3", ts=5)
    result = verify_admission_ledger(open_admission_reader(eng.sdd_dir))
    assert result.ok is False
    assert any("capacity" in e for e in result.errors)


def test_enforce_pool_with_advise_tag_over_limit_still_verifies(tmp_path: Path) -> None:
    # Regression guard: a legitimate over_limit grant (ENFORCE pool under
    # capacity, ADVISE tag over its limit) must NOT be flagged as forged.
    eng = _engine(tmp_path)
    eng.set_pool("p", 5)
    eng.set_tag_limit("t", 1, posture=Posture.ADVISE)
    eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1, tags=("t",))
    dec = eng.request_grant(pool="p", task_id="b", worker_id="w2", now=2, tags=("t",))
    assert dec.over_limit is True
    result = eng.verify()
    assert result.ok is True, result.errors


# ---------------------------------------------------------------------------
# Item 4: verifier replays the ENFORCE tag gate, not only pool capacity
# ---------------------------------------------------------------------------


def test_verifier_replays_enforce_tag_gate(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_tag_limit("lock", 1)  # ENFORCE, limit 1
    eng.request_grant(pool="", task_id="a", worker_id="w1", now=1, tags=("lock",))
    _forge_grant(eng, task_id="b", pool="", worker_id="w2", ts=2, tags=("lock",))
    result = verify_admission_ledger(open_admission_reader(eng.sdd_dir))
    assert result.ok is False
    assert any("tag" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Item 5: `limits queue pause` on unknown / mis-cased name
# ---------------------------------------------------------------------------


def test_queue_pause_unknown_name_fails_and_creates_no_queue(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein.cli.commands.limits_cmd import limits_group

    runner = CliRunner()
    result = runner.invoke(limits_group, ["queue", "pause", "ghost", "--workdir", str(tmp_path)])
    assert result.exit_code == 1
    # No phantom queue was appended to the ledger.
    eng = AdmissionEngine.for_workdir(tmp_path.resolve())
    assert "ghost" not in eng.state().queues


def test_queue_pause_mis_cased_name_preserves_priority(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein.cli.commands.limits_cmd import limits_group

    runner = CliRunner()
    created = runner.invoke(limits_group, ["queue", "create", "build", "--priority", "5", "--workdir", str(tmp_path)])
    assert created.exit_code == 0
    paused = runner.invoke(limits_group, ["queue", "pause", "BUILD", "--workdir", str(tmp_path)])
    assert paused.exit_code == 0
    spec = AdmissionEngine.for_workdir(tmp_path.resolve()).state().queues["build"]
    assert spec.paused is True
    assert spec.priority == 5  # priority preserved, not reset to 0


# ---------------------------------------------------------------------------
# Item 6: tag-conformance path check rejects `..` traversal and absolute paths
# ---------------------------------------------------------------------------


def test_docs_only_dotdot_escape_is_a_violation() -> None:
    from bernstein.core.admission.tags import check_conformance

    assert check_conformance(("docs-only",), ("docs/../src/evil.py",))
    assert check_conformance(("docs-only",), ("/etc/passwd",))
    # A clean docs path is still conformant.
    assert check_conformance(("docs-only",), ("docs/guide.md",)) == ()


def test_no_src_dotdot_escape_is_a_violation() -> None:
    from bernstein.core.admission.tags import check_conformance

    # A path that dodges the ``src/`` prefix via traversal must still be flagged.
    assert check_conformance(("no-src",), ("build/../src/secret.py",))


# ---------------------------------------------------------------------------
# Item 7: ledger_id path traversal is rejected (already hardened via containment)
# ---------------------------------------------------------------------------


def test_ledger_id_traversal_is_rejected(tmp_path: Path) -> None:
    from bernstein.core.admission.ledger import admission_ledger_dir
    from bernstein.core.security.path_containment import PathContainmentError

    for evil in ("../evil", "..", "a/b", "/abs"):
        with pytest.raises((PathContainmentError, ValueError)):
            admission_ledger_dir(tmp_path, evil)


# ---------------------------------------------------------------------------
# Item 8: quarantine runs the full expiry receipt lifecycle per grant
# ---------------------------------------------------------------------------


def test_quarantine_runs_full_expiry_lifecycle_per_grant(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_tag_limit("lock", 3)
    eng.request_grant(pool="", task_id="a", worker_id="w1", now=1, tags=("lock",))
    eng.request_grant(pool="", task_id="b", worker_id="w2", now=2, tags=("lock",))
    result = eng.quarantine(target_kind="tag", target="lock", now=10, queued_tasks=("c",))

    for entry in result.manifest["checkpointed"]:
        assert entry["expire_hash"]  # a real expire row was appended
        assert entry["receipt_digest"]  # a signed escalation receipt was assembled
    # The grants are actually released, not merely relabelled.
    assert eng.state().tag_occupancy().get("lock", 0) == 0
    # Determinism preserved: replay reproduces the identical manifest.
    replayed = AdmissionState.from_rows(open_admission_reader(eng.sdd_dir).entries()).quarantines[0]
    assert replayed["checkpointed"] == result.manifest["checkpointed"]


# ---------------------------------------------------------------------------
# Item 9: waiver receipt names the exact violated gate
# ---------------------------------------------------------------------------


def test_waiver_names_the_tag_gate_not_the_pool(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 5)  # ENFORCE, roomy -> the pool is not the exceeded gate
    eng.set_tag_limit("lock", 1, posture=Posture.ADVISE)
    eng.request_grant(pool="p", task_id="a", worker_id="w1", now=1, tags=("lock",))
    dec = eng.request_grant(pool="p", task_id="b", worker_id="w2", now=2, tags=("lock",))
    assert dec.over_limit is True
    assert dec.waiver is not None
    assert dec.waiver.gate == "lock"
    assert dec.waiver.resource == "lock"


def test_waiver_names_the_pool_gate_for_pool_over_limit(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("soft", 1, posture=Posture.ADVISE)
    eng.request_grant(pool="soft", task_id="a", worker_id="w1", now=1)
    dec = eng.request_grant(pool="soft", task_id="b", worker_id="w2", now=2)
    assert dec.over_limit is True
    assert dec.waiver is not None
    assert dec.waiver.gate == "soft"
    assert dec.waiver.resource == "soft"


# ---------------------------------------------------------------------------
# Item 10: declared tags are de-duplicated (one grant = at most one unit)
# ---------------------------------------------------------------------------


def test_declared_tags_are_deduplicated(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_tag_limit("lock", 2)  # ENFORCE, limit 2
    eng.request_grant(pool="", task_id="a", worker_id="w1", now=1, tags=("lock", "lock"))
    assert eng.state().tag_occupancy().get("lock") == 1
    # A second grant is still admissible (the duplicate did not eat two units).
    dec = eng.request_grant(pool="", task_id="b", worker_id="w2", now=2, tags=("lock",))
    assert dec.admitted is True


# ---------------------------------------------------------------------------
# Item 11: a late heartbeat cannot revive an already-expired lease
# ---------------------------------------------------------------------------


def test_late_renew_is_rejected(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    g = eng.request_grant(pool="p", task_id="T1", worker_id="w1", now=100, ttl_s=50)  # expiry 150
    with pytest.raises(ValueError):
        eng.renew(g.grant_id, now=200)  # 200 >= 150
    # The lease is still expired; a sweep still expires it.
    assert eng.sweep_expired(now=201) != []


def test_forged_late_renew_row_does_not_revive_in_projection(tmp_path: Path) -> None:
    from bernstein.core.admission.ledger import KIND_RENEW

    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    g = eng.request_grant(pool="p", task_id="T1", worker_id="w1", now=100, ttl_s=50).grant_id  # expiry 150
    # Forge a renew row past the lease expiry directly into the chain.
    ledger = open_admission_ledger(eng.sdd_dir)
    ledger.append(kind=KIND_RENEW, payload={"grant": g, "ts": 200})
    ledger.close()
    # The projection must ignore the late renew: the lease stays expired at 150.
    state = eng.state()
    assert state.expired_grants(160) != []


# ---------------------------------------------------------------------------
# Item 12: an open interval is detected by end_kind, not end_ts == 0
# ---------------------------------------------------------------------------


def test_release_at_logical_time_zero_closes_the_interval(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    eng.set_pool("p", 1)
    g = eng.request_grant(pool="p", task_id="a", worker_id="w1", now=0, ttl_s=0).grant_id
    eng.release(g, now=0)  # released at logical time 0
    state = AdmissionState.from_rows(open_admission_reader(eng.sdd_dir).entries())
    # The interval is closed: nobody holds the pool at (or after) time 0.
    assert state.who_held("p", 0) == []
    interval = state.grant_history[0]
    assert interval.end_kind == "release"
    assert interval.covers(0) is False
