"""Unit tests for intent capsules with deterministic drift escalation (#2514).

An intent capsule is the approved goal compiled into a canonical, signed chain
entry: it lists the allowed action classes, file-scope globs, permitted
adapters, egress classes, a cost-envelope reference, and an expiry. The capsule
is written to the HMAC audit chain at approval time and its hash is bound into
the run journal, so every subsequent journal step is attributable to one
approved capsule.

A deterministic drift monitor (no LLM in the loop) maps observed journal events
to action classes and compares them against the capsule. The conformance verdict
is a pure function of ``(journal, capsule)``: two verifiers recompute the same
verdict offline. On divergence the monitor emits a signed escalation receipt
reusing the stall-escalation shape, binding the capsule hash and the divergent
events; the receipt passes ``bernstein escalation verify``.

Strip the audit chain and the journal and the feature collapses to a goal string
with a log: the capsule IS a signed chain entry and the drift escalation IS a
signed receipt referencing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.escalation import verify_escalation_receipt
from bernstein.core.security.audit_chain import (
    EVENT_INTENT_CAPSULE,
    EVENT_INTENT_DRIFT,
    AuditChainStore,
)
from bernstein.core.security.intent_capsule import (
    CAPSULE_BOUND_EVENT,
    DriftPolicy,
    IntentCapsule,
    approve_and_capsule,
    assemble_intent_drift_escalation,
    assert_no_llm_imports,
    bind_capsule_into_journal,
    canonicalise,
    capsule_hash,
    capsule_spawn_binding,
    classify_journal_event,
    compile_capsule,
    evaluate_conformance,
    read_capsule,
    read_capsule_binding,
    verify_intent_conformance,
    write_capsule,
)
from bernstein.core.tasks.models import TaskCostEstimate, TaskPlan

_HMAC_KEY = b"k" * 32
_RUN_ID = "run-intent-1"
_TASK_ID = "task-abc123"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _sdd(tmp_path: Path) -> Path:
    return tmp_path / ".sdd"


def _plan() -> TaskPlan:
    return TaskPlan(
        id="plan01",
        goal="Refactor the pricing module for clarity; no external calls.",
        task_estimates=[
            TaskCostEstimate(
                task_id=_TASK_ID,
                title="Refactor pricing",
                role="backend",
                model="sonnet",
                estimated_tokens=80_000,
                estimated_cost_usd=0.24,
                risk_level="low",
            )
        ],
        total_estimated_cost_usd=0.24,
        total_estimated_minutes=30,
    )


def _capsule() -> IntentCapsule:
    return compile_capsule(
        plan=_plan(),
        task_id=_TASK_ID,
        allowed_action_classes=["fs.read", "fs.write", "git.commit"],
        file_scope_globs=["src/pricing/**", "tests/**"],
        permitted_adapters=["claude", "codex"],
        egress_classes=[],
        expiry_ts=1_700_100_000,
    )


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(_sdd(tmp_path) / "audit", key=_HMAC_KEY)


def _journal_events(*, drift: bool):
    """Return an ordered list of journal events (dicts) for classification.

    When ``drift`` is False every event maps to an allowed action class. When
    True a ``web.fetch`` event (external comm, not in the capsule) is injected.
    """
    events = [
        {"event": "task.tick", "action_class": None, "seq": 0},
        {"event": "tool.call", "tool": "Read", "seq": 1},
        {"event": "tool.call", "tool": "Edit", "seq": 2},
        {"event": "tool.call", "tool": "Bash", "action_class": "git.commit", "seq": 3},
    ]
    if drift:
        events.append({"event": "tool.call", "tool": "WebFetch", "seq": 4})
    return events


def _build_run_journal(tmp_path: Path, *, capsule_h: str, drift: bool):
    """Write a real Merkle-chained run journal binding the capsule then events."""
    from bernstein.core.replay.journal import EventJournal

    journal = EventJournal(_RUN_ID, _sdd(tmp_path))
    bind_capsule_into_journal(journal, task_id=_TASK_ID, capsule_hash=capsule_h)
    journal.record("tool.call", tool="Read", seq=1)
    journal.record("tool.call", tool="Edit", seq=2)
    journal.record("tool.call", tool="Bash", action_class="git.commit", seq=3)
    if drift:
        journal.record("tool.call", tool="WebFetch", seq=4)
    return journal


# ---------------------------------------------------------------------------
# Capsule schema + canonicalisation
# ---------------------------------------------------------------------------


def test_capsule_canonical_bytes_are_stable_and_sorted() -> None:
    cap = _capsule()
    raw = canonicalise(cap)
    # Canonical: sorted keys, compact separators, deterministic.
    assert raw == canonicalise(IntentCapsule.from_dict(cap.to_dict()))
    assert capsule_hash(cap).startswith("sha256:")
    # Round-trips through dict without changing the hash.
    assert capsule_hash(IntentCapsule.from_dict(cap.to_dict())) == capsule_hash(cap)


def test_compile_capsule_binds_goal_and_cost_by_digest_not_text() -> None:
    cap = _capsule()
    # Goal is bound by digest, never stored as free text on the capsule.
    assert cap.goal_digest.startswith("sha256:")
    assert "Refactor" not in canonicalise(cap).decode("utf-8")
    assert cap.cost_envelope_ref.startswith("sha256:")
    assert cap.expiry_ts == 1_700_100_000


# ---------------------------------------------------------------------------
# AC1 - approving a plan writes a capsule to the audit chain; journal steps
# reference the capsule hash
# ---------------------------------------------------------------------------


def test_ac1_approve_writes_capsule_to_audit_chain(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    capsule, event = approve_and_capsule(
        chain=chain,
        sdd_dir=_sdd(tmp_path),
        plan=_plan(),
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        allowed_action_classes=["fs.read", "fs.write", "git.commit"],
        file_scope_globs=["src/pricing/**"],
        permitted_adapters=["claude"],
        egress_classes=[],
        expiry_ts=1_700_100_000,
    )
    assert event.event_type == EVENT_INTENT_CAPSULE
    assert event.details["capsule_hash"] == capsule_hash(capsule)
    assert event.details["task_id"] == _TASK_ID
    assert "prev_chain_digest" in event.details
    ok, errors = chain.verify()
    assert ok, errors
    # The capsule is retrievable offline.
    loaded, run_id = read_capsule_binding(_sdd(tmp_path), _TASK_ID)
    assert loaded is not None
    assert run_id == _RUN_ID
    assert capsule_hash(loaded) == capsule_hash(capsule)


def test_ac1_journal_steps_reference_capsule_hash(tmp_path: Path) -> None:
    from bernstein.core.replay.journal import load_events

    cap = _capsule()
    ch = capsule_hash(cap)
    journal = _build_run_journal(tmp_path, capsule_h=ch, drift=False)
    events = load_events(journal.path)
    bound = [e for e in events if e["event"] == CAPSULE_BOUND_EVENT]
    assert len(bound) == 1
    assert bound[0]["capsule_hash"] == ch
    assert bound[0]["task_id"] == _TASK_ID


def test_ac1_spawn_record_binding_carries_capsule_hash() -> None:
    cap = _capsule()
    ch = capsule_hash(cap)
    binding = capsule_spawn_binding(task_id=_TASK_ID, capsule_hash=ch)
    assert binding["intent_capsule_hash"] == ch
    assert binding["intent_task_id"] == _TASK_ID


# ---------------------------------------------------------------------------
# Event -> action-class mapping (deterministic)
# ---------------------------------------------------------------------------


def test_classify_journal_event_maps_tools_to_action_classes() -> None:
    assert classify_journal_event({"tool": "Read"}) == "fs.read"
    assert classify_journal_event({"tool": "Edit"}) == "fs.write"
    assert classify_journal_event({"tool": "WebFetch"}) == "web.fetch"
    # Explicit action_class wins over the tool table.
    assert classify_journal_event({"tool": "Bash", "action_class": "git.commit"}) == "git.commit"
    # Non-action events classify to None (ticks, capsule bindings, snapshots).
    assert classify_journal_event({"event": "task.tick"}) is None
    assert classify_journal_event({"event": CAPSULE_BOUND_EVENT, "capsule_hash": "x"}) is None


# ---------------------------------------------------------------------------
# AC2 - the conformance verdict is a pure function of (journal, capsule):
# two verifiers recompute byte-identical verdicts offline
# ---------------------------------------------------------------------------


def test_ac2_conformance_verdict_is_deterministic_across_machines(tmp_path: Path) -> None:
    cap = _capsule()
    events = _journal_events(drift=True)
    v1 = evaluate_conformance(events, cap)
    v2 = evaluate_conformance(list(events), cap)
    assert v1.verdict_hash == v2.verdict_hash
    assert v1.to_dict() == v2.to_dict()
    # A drift is detected on the web.fetch event.
    assert not v1.conformant
    assert [d.action_class for d in v1.divergences] == ["web.fetch"]


def test_ac2_clean_run_is_conformant() -> None:
    cap = _capsule()
    v = evaluate_conformance(_journal_events(drift=False), cap)
    assert v.conformant
    assert v.divergences == ()


# ---------------------------------------------------------------------------
# AC5 - deterministic replay re-derives the same drift decisions at the same
# step indices
# ---------------------------------------------------------------------------


def test_ac5_drift_decisions_at_stable_step_indices() -> None:
    cap = _capsule()
    events = _journal_events(drift=True)
    v1 = evaluate_conformance(events, cap)
    v2 = evaluate_conformance(events, cap)
    assert [d.step_index for d in v1.divergences] == [d.step_index for d in v2.divergences]
    # The divergent event is the last one (index 4).
    assert [d.step_index for d in v1.divergences] == [4]


# ---------------------------------------------------------------------------
# AC3 - tampering with the capsule bytes or reordering journal steps flips
# verification to fail
# ---------------------------------------------------------------------------


def test_ac3_clean_run_verifies(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    cap, _ = approve_and_capsule(
        chain=chain,
        sdd_dir=_sdd(tmp_path),
        plan=_plan(),
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        allowed_action_classes=["fs.read", "fs.write", "git.commit"],
        file_scope_globs=["src/pricing/**"],
        permitted_adapters=["claude"],
        egress_classes=[],
        expiry_ts=1_700_100_000,
    )
    _build_run_journal(tmp_path, capsule_h=capsule_hash(cap), drift=False)
    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)
    assert result.ok, result.reason
    assert result.conformant


def test_ac3_tampered_capsule_bytes_fail_verify(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    cap, _ = approve_and_capsule(
        chain=chain,
        sdd_dir=_sdd(tmp_path),
        plan=_plan(),
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        allowed_action_classes=["fs.read", "fs.write", "git.commit"],
        file_scope_globs=["src/pricing/**"],
        permitted_adapters=["claude"],
        egress_classes=[],
        expiry_ts=1_700_100_000,
    )
    _build_run_journal(tmp_path, capsule_h=capsule_hash(cap), drift=False)
    # Tamper the on-disk capsule: widen the allow-list after approval.
    import json as _json

    path = _sdd(tmp_path) / "intent" / "capsules" / f"{_TASK_ID}.json"
    row = _json.loads(path.read_text(encoding="utf-8"))
    row["capsule"]["allowed_action_classes"].append("web.fetch")
    path.write_text(_json.dumps(row), encoding="utf-8")

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)
    assert not result.ok
    assert "capsule" in result.reason.lower()


def test_ac3_reordered_journal_steps_fail_verify(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    cap, _ = approve_and_capsule(
        chain=chain,
        sdd_dir=_sdd(tmp_path),
        plan=_plan(),
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        allowed_action_classes=["fs.read", "fs.write", "git.commit"],
        file_scope_globs=["src/pricing/**"],
        permitted_adapters=["claude"],
        egress_classes=[],
        expiry_ts=1_700_100_000,
    )
    _build_run_journal(tmp_path, capsule_h=capsule_hash(cap), drift=False)
    journal_path = _sdd(tmp_path) / "runs" / _RUN_ID / "journal.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    # Reorder two steps: the Merkle chain no longer recomputes.
    lines[-1], lines[-2] = lines[-2], lines[-1]
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)
    assert not result.ok
    assert "journal" in result.reason.lower()


# ---------------------------------------------------------------------------
# AC4 - a drift event produces a signed escalation receipt that passes
# bernstein escalation verify and binds the capsule hash and the divergent events
# ---------------------------------------------------------------------------


def test_ac4_drift_emits_signed_escalation_binding_capsule_and_events(tmp_path: Path) -> None:
    from bernstein.core.orchestration.escalation import (
        load_or_create_escalation_identity,
        read_escalation_receipt,
    )
    from bernstein.core.replay.journal import load_events

    cap = _capsule()
    ch = capsule_hash(cap)
    journal = _build_run_journal(tmp_path, capsule_h=ch, drift=True)
    verdict = evaluate_conformance(load_events(journal.path), cap)
    assert not verdict.conformant

    private_pem, public_pem = load_or_create_escalation_identity(_sdd(tmp_path) / "identity")
    receipt = assemble_intent_drift_escalation(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_sdd(tmp_path) / "lineage",
        hmac_key=_HMAC_KEY,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        run_id=_RUN_ID,
        capsule=cap,
        verdict=verdict,
        worker_id="abcdef012345",
        session_id="sess-1",
        worktree_id="wt-1",
        install_rev="abc1234567890def",
        timestamp=1_700_000_000,
    )
    # It binds the capsule hash and the divergent events.
    assert receipt.extra_binding is not None
    assert receipt.extra_binding["kind"] == "intent_drift"
    assert receipt.extra_binding["capsule_hash"] == ch
    assert receipt.extra_binding["verdict_hash"] == verdict.verdict_hash
    assert [d["action_class"] for d in receipt.extra_binding["divergent_events"]] == ["web.fetch"]

    # It passes the existing escalation verify path (bernstein escalation verify).
    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_sdd(tmp_path) / "lineage",
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert result.ok, result.reason
    # And reloads byte-identically from disk.
    reloaded = read_escalation_receipt(_sdd(tmp_path), receipt.receipt_id)
    assert reloaded is not None
    assert reloaded.to_dict() == receipt.to_dict()


def test_ac4_drift_mirrors_into_audit_chain(tmp_path: Path) -> None:
    from bernstein.core.security.intent_capsule import record_intent_drift

    chain = _chain(tmp_path)
    cap = _capsule()
    events = _journal_events(drift=True)
    verdict = evaluate_conformance(events, cap)
    event = record_intent_drift(
        chain=chain,
        task_id=_TASK_ID,
        capsule_hash=capsule_hash(cap),
        verdict_hash=verdict.verdict_hash,
        divergent_count=len(verdict.divergences),
        escalation_journal_entry_hash="sha256:deadbeef",
    )
    assert event.event_type == EVENT_INTENT_DRIFT
    assert event.details["capsule_hash"] == capsule_hash(cap)
    assert event.details["divergent_count"] == 1
    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# AC6 - no LLM call exists on the drift-decision path (static import guard +
# runtime assertion)
# ---------------------------------------------------------------------------


def test_ac6_static_import_guard_module_is_llm_free() -> None:
    import bernstein.core.security.intent_capsule as mod

    # The decision module imports nothing from an LLM provider / adapter.
    assert_no_llm_imports(mod.__file__)


def test_ac6_static_import_guard_rejects_llm_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad_module.py"
    bad.write_text("import anthropic\n\ndef f():\n    return 1\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="llm|anthropic"):
        assert_no_llm_imports(str(bad))


def test_ac6_runtime_no_llm_module_executes_on_drift_path() -> None:
    """Profile the drift-decision path and assert no LLM module ever executes."""
    import sys

    denylisted = ("anthropic", "openai", "litellm", "cohere", "mistralai", "ollama")
    touched: set[str] = set()

    def _profiler(frame, event, arg):
        name = frame.f_globals.get("__name__", "")
        if name:
            touched.add(name)
        return None

    cap = _capsule()
    events = _journal_events(drift=True)
    sys.setprofile(_profiler)
    try:
        evaluate_conformance(events, cap)
        classify_journal_event({"tool": "WebFetch"})
    finally:
        sys.setprofile(None)

    offenders = [m for m in touched if any(m == d or m.startswith(d + ".") for d in denylisted)]
    assert not offenders, f"LLM modules executed on drift path: {offenders}"


# ---------------------------------------------------------------------------
# Drift policy (thresholds as reviewed data)
# ---------------------------------------------------------------------------


def test_drift_policy_warn_only_is_default() -> None:
    assert DriftPolicy.default().mode == "warn"
    assert DriftPolicy.from_dict({"mode": "block"}).mode == "block"
    # Round-trips.
    assert DriftPolicy.from_dict(DriftPolicy.default().to_dict()).mode == "warn"


def test_capsule_store_roundtrip(tmp_path: Path) -> None:
    cap = _capsule()
    write_capsule(_sdd(tmp_path), cap, run_id=_RUN_ID)
    loaded = read_capsule(_sdd(tmp_path), _TASK_ID)
    assert loaded is not None
    assert loaded.to_dict() == cap.to_dict()


def test_verify_refuses_a_run_journal_planted_outside_the_runs_root(tmp_path: Path) -> None:
    """The conformance verifier must not read a journal outside ``<sdd>/runs``.

    Pins the routing of ``verify_intent_conformance`` through the shared
    containment barrier. The journal's own Merkle chain is intact here - it
    is written by the real writer - so ``verify_journal`` cannot tell it
    apart from ours; only containment can. Reverting the routing to a raw
    join makes this verify a planted journal and fails the test.
    """
    import pytest

    chain = _chain(tmp_path)
    cap, _ = approve_and_capsule(
        chain=chain,
        sdd_dir=_sdd(tmp_path),
        plan=_plan(),
        task_id=_TASK_ID,
        run_id=_RUN_ID,
        allowed_action_classes=["fs.read", "fs.write", "git.commit"],
        file_scope_globs=["src/pricing/**"],
        permitted_adapters=["claude"],
        egress_classes=[],
        expiry_ts=1_700_100_000,
    )
    journal = _build_run_journal(tmp_path, capsule_h=capsule_hash(cap), drift=False)

    # Move the whole run directory out of the runs root and symlink to it.
    run_dir = journal.path.parent
    outside = tmp_path / "outside_runs"
    run_dir.rename(outside)
    try:
        run_dir.symlink_to(outside, target_is_directory=True)
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("cannot create symlinks on this platform")

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    # Unrouted, this journal reads cleanly and reports ok/conformant.
    assert not result.ok
    assert not result.conformant
    assert "invalid run id" in result.reason
