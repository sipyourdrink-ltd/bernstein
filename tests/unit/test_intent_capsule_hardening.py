"""Hardening regressions for intent capsules (#2649).

Every value a verifier treats as authoritative must be derived from signed or
Merkle-chained state, never from caller-supplied or unsigned input:

* the run a capsule is verified against comes from the signed ``intent.capsule``
  audit event, not from the unsigned on-disk sidecar, and the journal must carry
  exactly one matching ``intent.capsule_bound`` anchor;
* the declared capsule scope (file globs, adapters, expiry) is enforced rather
  than merely recorded, and ``allow_unclassified`` actually gates unclassified
  events;
* a worker-stamped ``action_class`` cannot override the reviewed tool mapping;
* a drift escalation signs a verdict recomputed from the journal, never the
  verdict its caller handed in;
* the read-only ``intent verify`` path never mints audit key material.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.replay.journal import EventJournal, load_events, verify_journal
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.intent_capsule import (
    DriftPolicy,
    IntentCapsule,
    IntentCapsuleError,
    approve_and_capsule,
    assemble_intent_drift_escalation,
    bind_capsule_into_journal,
    capsule_hash,
    classify_journal_event,
    compile_capsule,
    evaluate_conformance,
    path_in_scope,
    read_capsule_binding,
    seal_run_journal,
    verify_intent_conformance,
)
from bernstein.core.tasks.models import TaskCostEstimate, TaskPlan

_HMAC_KEY = b"k" * 32
_RUN_ID = "run-intent-h1"
_TASK_ID = "task-hardening-1"

#: Far-future expiry so fixtures that build real journals (whose events carry a
#: wall-clock ``ts``) are not incidentally expired. Expiry enforcement itself is
#: covered by its own regression below.
_FUTURE_EXPIRY = 4_102_444_800  # 2100-01-01T00:00:00Z


def _sdd(tmp_path: Path) -> Path:
    return tmp_path / ".sdd"


def _plan() -> TaskPlan:
    return TaskPlan(
        id="planh1",
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


def _capsule(**overrides) -> IntentCapsule:
    # No adapter allowlist by default: an allowlist now denies events that
    # record no adapter (#2649), which would entangle every unrelated
    # conformance assertion. The adapter control has its own tests below.
    kwargs = {
        "allowed_action_classes": ["fs.read", "fs.write", "git.commit"],
        "file_scope_globs": ["src/pricing/**"],
        "permitted_adapters": [],
        "egress_classes": [],
        "expiry_ts": _FUTURE_EXPIRY,
    }
    kwargs.update(overrides)
    return compile_capsule(plan=_plan(), task_id=_TASK_ID, **kwargs)


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(_sdd(tmp_path) / "audit", key=_HMAC_KEY)


def _approve(tmp_path: Path, *, run_id: str = _RUN_ID, **overrides) -> IntentCapsule:
    kwargs = {
        "allowed_action_classes": ["fs.read", "fs.write", "git.commit"],
        "file_scope_globs": ["src/pricing/**"],
        "permitted_adapters": ["claude"],
        "egress_classes": [],
        "expiry_ts": _FUTURE_EXPIRY,
    }
    kwargs.update(overrides)
    capsule, _ = approve_and_capsule(
        chain=_chain(tmp_path),
        sdd_dir=_sdd(tmp_path),
        plan=_plan(),
        task_id=_TASK_ID,
        run_id=run_id,
        **kwargs,
    )
    return capsule


def _journal(
    tmp_path: Path,
    run_id: str,
    *,
    capsule_h: str,
    bind: bool = True,
    drift: bool = False,
    seal: IntentCapsule | None = None,
) -> EventJournal:
    journal = EventJournal(run_id, _sdd(tmp_path))
    if bind:
        bind_capsule_into_journal(journal, task_id=_TASK_ID, capsule_hash=capsule_h)
    journal.record("tool.call", tool="Read", adapter="claude", path="src/pricing/rates.py", seq=1)
    journal.record("tool.call", tool="Edit", adapter="claude", path="src/pricing/rates.py", seq=2)
    if drift:
        journal.record("tool.call", tool="WebFetch", adapter="claude", seq=3)
    if seal is not None:
        seal_run_journal(chain=_chain(tmp_path), sdd_dir=_sdd(tmp_path), task_id=_TASK_ID, run_id=run_id, capsule=seal)
    return journal


def _sidecar_path(tmp_path: Path) -> Path:
    return _sdd(tmp_path) / "intent" / "capsules" / f"{_TASK_ID}.json"


def _rewrite_sidecar_run_id(tmp_path: Path, run_id: str) -> None:
    path = _sidecar_path(tmp_path)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["run_id"] = run_id
    path.write_text(json.dumps(row), encoding="utf-8")


# ---------------------------------------------------------------------------
# Critical: the verified run comes from the signed audit event, not the sidecar
# ---------------------------------------------------------------------------


def test_forged_sidecar_run_id_is_rejected(tmp_path: Path) -> None:
    """Repointing the unsigned sidecar at a clean run must not launder drift."""
    capsule = _approve(tmp_path)
    ch = capsule_hash(capsule)
    # The run the audit chain actually attests to drifted.
    _journal(tmp_path, _RUN_ID, capsule_h=ch, drift=True)
    # The attacker stages a clean run and repoints the unsigned sidecar at it.
    _journal(tmp_path, "run-decoy", capsule_h=ch, drift=False)
    _rewrite_sidecar_run_id(tmp_path, "run-decoy")

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert not result.conformant
    assert "run_id" in result.reason
    # The authoritative run_id is the signed one, not the forged sidecar value.
    assert result.run_id == _RUN_ID


def test_verify_uses_audit_run_id_when_sidecar_is_silent(tmp_path: Path) -> None:
    """An empty sidecar run_id makes no claim; the signed run_id still governs."""
    capsule = _approve(tmp_path)
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), seal=capsule)
    _rewrite_sidecar_run_id(tmp_path, "")

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert result.ok, result.reason
    assert result.run_id == _RUN_ID


def test_verify_requires_a_capsule_bound_anchor(tmp_path: Path) -> None:
    """A journal that never bound the capsule is not attributable to it."""
    capsule = _approve(tmp_path)
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), bind=False)

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "capsule_bound" in result.reason


def test_verify_rejects_a_capsule_bound_anchor_for_another_capsule(tmp_path: Path) -> None:
    """An anchor naming a different capsule does not attribute this run."""
    _approve(tmp_path)
    _journal(tmp_path, _RUN_ID, capsule_h="sha256:" + "0" * 64)

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "0 matching" in result.reason
    assert "capsule_bound" in result.reason


def test_verify_rejects_duplicate_capsule_bound_anchors(tmp_path: Path) -> None:
    """Exactly one anchor: two bindings make attribution ambiguous."""
    capsule = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), seal=capsule)
    bind_capsule_into_journal(journal, task_id=_TASK_ID, capsule_hash=capsule_hash(capsule))

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "capsule_bound" in result.reason


# ---------------------------------------------------------------------------
# The declared capsule scope is enforced, not merely recorded
# ---------------------------------------------------------------------------


def test_file_scope_globs_are_enforced(tmp_path: Path) -> None:
    capsule = _capsule(file_scope_globs=["src/pricing/**"])
    events = [
        {"event": "tool.call", "tool": "Edit", "path": "src/pricing/rates.py"},
        {"event": "tool.call", "tool": "Edit", "path": "src/billing/secrets.py"},
    ]

    verdict = evaluate_conformance(events, capsule)

    assert not verdict.conformant
    assert [d.step_index for d in verdict.divergences] == [1]
    assert verdict.divergences[0].reason == "file_scope_violation"


def test_file_scope_globs_do_not_match_across_directory_separators(tmp_path: Path) -> None:
    """A single ``*`` must not silently span ``/`` and widen the approved scope."""
    capsule = _capsule(file_scope_globs=["src/*.py"])
    events = [{"event": "tool.call", "tool": "Edit", "path": "src/nested/deep.py"}]

    verdict = evaluate_conformance(events, capsule)

    assert not verdict.conformant
    assert verdict.divergences[0].reason == "file_scope_violation"


def test_file_scope_globs_are_not_escapable_by_traversal(tmp_path: Path) -> None:
    """``..`` must be collapsed before matching, or the prefix is a free pass."""
    capsule = _capsule(file_scope_globs=["src/pricing/**"])
    events = [
        {"event": "tool.call", "tool": "Edit", "path": "src/pricing/../../etc/passwd"},
        {"event": "tool.call", "tool": "Edit", "path": "src/pricing/./rates.py"},
    ]

    verdict = evaluate_conformance(events, capsule)

    assert [d.step_index for d in verdict.divergences] == [0]
    assert verdict.divergences[0].reason == "file_scope_violation"


def test_path_in_scope_collapses_traversal_and_dot_segments() -> None:
    from bernstein.core.security.intent_capsule import path_in_scope

    globs = ("src/pricing/**",)
    assert path_in_scope("src/pricing/rates.py", globs)
    assert path_in_scope("./src/pricing/rates.py", globs)
    assert path_in_scope("src/pricing/nested/deep.py", globs)
    assert not path_in_scope("src/pricing/../secrets.py", globs)
    assert not path_in_scope("src/pricing/../../../etc/passwd", globs)


def test_empty_file_scope_globs_leave_writes_unconstrained(tmp_path: Path) -> None:
    capsule = _capsule(file_scope_globs=[])
    events = [{"event": "tool.call", "tool": "Edit", "path": "anywhere/at/all.py"}]

    assert evaluate_conformance(events, capsule).conformant


def test_permitted_adapters_are_enforced(tmp_path: Path) -> None:
    capsule = _capsule(permitted_adapters=["claude"])
    events = [
        {"event": "tool.call", "tool": "Read", "adapter": "claude"},
        {"event": "tool.call", "tool": "Read", "adapter": "codex"},
    ]

    verdict = evaluate_conformance(events, capsule)

    assert not verdict.conformant
    assert [d.step_index for d in verdict.divergences] == [1]
    assert verdict.divergences[0].reason == "adapter_not_permitted"


def test_a_mutating_action_that_names_no_path_fails_closed(tmp_path: Path) -> None:
    """A declared scope that cannot be checked is not a control."""
    capsule = _capsule(file_scope_globs=["src/pricing/**"])

    verdict = evaluate_conformance([{"event": "tool.call", "tool": "Edit"}], capsule)

    assert not verdict.conformant
    assert verdict.divergences[0].reason == "path_unrecorded"


def test_a_mutating_action_with_an_unrecognised_path_key_fails_closed(tmp_path: Path) -> None:
    capsule = _capsule(file_scope_globs=["src/pricing/**"])

    verdict = evaluate_conformance([{"event": "tool.call", "tool": "Edit", "target": "/etc/passwd"}], capsule)

    assert not verdict.conformant
    assert verdict.divergences[0].reason == "path_unrecorded"


def test_no_declared_scope_does_not_require_a_path(tmp_path: Path) -> None:
    capsule = _capsule(file_scope_globs=[])

    assert evaluate_conformance([{"event": "tool.call", "tool": "Edit"}], capsule).conformant


def test_verdict_does_not_depend_on_unauthenticated_journal_timestamps(tmp_path: Path) -> None:
    """``ts`` is outside the Merkle chain, so it must not decide a signed verdict.

    ``_NON_DETERMINISTIC_FIELDS`` in ``core/replay/journal.py`` strips ``ts``
    before ``payload_hash``, so a row's timestamp can be rewritten in place with
    no chain break. Routing it into ``verdict_hash`` would let anyone erase or
    manufacture a divergence, and would make a faithful replay -- which produces
    fresh wall-clock values -- disagree with the original run.
    """
    capsule = _capsule(expiry_ts=1_700_000_000)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), seal=capsule)
    before = evaluate_conformance(load_events(journal.path), capsule)

    rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        row["ts"] = 1_600_000_000
    journal.path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    after = evaluate_conformance(load_events(journal.path), capsule)

    assert verify_journal(journal.path).ok, "editing ts alone does not break the journal chain"
    assert before.verdict_hash == after.verdict_hash
    assert [d.to_dict() for d in before.divergences] == [d.to_dict() for d in after.divergences]


def test_expiry_is_enforced_against_authenticated_chain_timestamps(tmp_path: Path) -> None:
    """Expiry uses audit-chain timestamps, which are covered by the entry HMAC."""
    from bernstein.core.security.audit_chain import EVENT_INTENT_CAPSULE
    from bernstein.core.security.intent_capsule import chain_expiry_violation

    capsule = _approve(tmp_path, expiry_ts=1_700_000_000)
    entries = [
        e for e in _chain(tmp_path).query(event_type=EVENT_INTENT_CAPSULE) if e.details.get("task_id") == _TASK_ID
    ]

    assert chain_expiry_violation(entries, capsule), "approval recorded after expiry is a violation"
    assert not chain_expiry_violation(entries, _capsule(expiry_ts=_FUTURE_EXPIRY))


def test_verify_rejects_a_capsule_used_past_its_expiry(tmp_path: Path) -> None:
    capsule = _approve(tmp_path, expiry_ts=1_700_000_000)
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), seal=capsule)

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "expired" in result.reason


def test_allow_unclassified_false_counts_unclassified_tool_calls(tmp_path: Path) -> None:
    capsule = _capsule()
    events = [{"event": "tool.call", "tool": "some_unknown_tool"}]

    assert evaluate_conformance(events, capsule, policy=DriftPolicy()).conformant
    strict = evaluate_conformance(events, capsule, policy=DriftPolicy(allow_unclassified=False))
    assert not strict.conformant
    assert strict.divergences[0].reason == "unclassified_event"


def test_allow_unclassified_false_ignores_structural_journal_events(tmp_path: Path) -> None:
    """The strict knob must stay usable on a real journal.

    Every one of these event types is recorded somewhere in the repo. Flagging
    them as drift would make ``allow_unclassified: false`` unusable, which is
    the same practical outcome as the no-op it replaced.
    """
    capsule = _capsule()
    strict = DriftPolicy(allow_unclassified=False)
    structural = [
        "intent.capsule_bound",
        "task.tick",
        "snapshot",
        "retry.decision",
        "retry.checkpoint",
        "context.capsule_recorded",
        "mcp.stateless_call",
        "provider_state_capability",
        "provider_state_mutation",
        "webhook_node.inbound",
        "dispatch_knob_selection",
        "worktree.gc",
        "worktree.reap",
        "a2a.message",
        "artifact_posted",
        "subagent.delegation",
        "thread.step",
        "task.suspend",
        "task.resume",
        "fork",
        "activity.result",
        "schedule.fire_projection",
    ]

    flagged = [e for e in structural if not evaluate_conformance([{"event": e}], capsule, policy=strict).conformant]

    assert flagged == []


# ---------------------------------------------------------------------------
# A worker-stamped action_class cannot override the reviewed tool mapping
# ---------------------------------------------------------------------------


def test_worker_action_class_cannot_relabel_a_recognized_tool() -> None:
    """A shell call stamped ``git.commit`` still classifies as ``shell.exec``."""
    assert classify_journal_event({"tool": "Bash", "action_class": "git.commit"}) == "shell.exec"
    assert classify_journal_event({"tool": "WebFetch", "action_class": "fs.read"}) == "web.fetch"


@pytest.mark.parametrize("tool", ["Bash", "bash", "BASH", "Bash ", " bash", "  BaSh  ", "\tbash\n"])
def test_tool_name_variants_cannot_dodge_the_reviewed_map(tool: str) -> None:
    """One stray character must not defeat the guard.

    The lookup is keyed on the tool name, and the tool name arrives in the same
    worker-controlled event as the stamped label, so an unnormalised key lets a
    worker escape by mutating the field the guard keys on.
    """
    assert classify_journal_event({"tool": tool, "action_class": "fs.read"}) == "shell.exec"


@pytest.mark.parametrize("tool", ["sh", "zsh", "pwsh", "powershell", "run_shell_command", "subprocess"])
def test_common_shell_aliases_resolve_to_shell_exec(tool: str) -> None:
    assert classify_journal_event({"tool": tool, "action_class": "fs.read"}) == "shell.exec"


def test_explicit_action_class_is_the_fallback_for_unknown_tools() -> None:
    assert classify_journal_event({"tool": "custom_mcp_tool", "action_class": "fs.read"}) == "fs.read"


def test_relabelled_shell_call_surfaces_as_drift(tmp_path: Path) -> None:
    capsule = _capsule()
    events = [{"event": "tool.call", "tool": "Bash", "action_class": "git.commit"}]

    verdict = evaluate_conformance(events, capsule)

    assert not verdict.conformant
    assert verdict.divergences[0].action_class == "shell.exec"


# ---------------------------------------------------------------------------
# Escalation: both the capsule and the verdict must come from chained state
# ---------------------------------------------------------------------------


def _identity(tmp_path: Path) -> tuple[str, str]:
    from bernstein.core.orchestration.escalation import load_or_create_escalation_identity

    return load_or_create_escalation_identity(_sdd(tmp_path) / "identity")


def _escalate(tmp_path: Path, capsule: IntentCapsule, verdict, **overrides):
    private_pem, public_pem = _identity(tmp_path)
    kwargs = {
        "sdd_dir": _sdd(tmp_path),
        "lineage_root": _sdd(tmp_path) / "lineage",
        "hmac_key": _HMAC_KEY,
        "private_key_pem": private_pem,
        "public_key_pem": public_pem,
        "chain": _chain(tmp_path),
        "run_id": _RUN_ID,
        "capsule": capsule,
        "verdict": verdict,
        "worker_id": "abcdef012345",
        "session_id": "sess-1",
        "worktree_id": "wt-1",
        "install_rev": "abc1234567890def",
        "timestamp": 1_700_000_000,
    }
    kwargs.update(overrides)
    return assemble_intent_drift_escalation(**kwargs)


def _drifted_run(tmp_path: Path) -> tuple[IntentCapsule, list]:
    """Approve a capsule on the chain and drift the bound run against it."""
    capsule = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), drift=True, seal=capsule)
    return capsule, load_events(journal.path)


def test_escalation_refuses_a_capsule_that_was_never_approved(tmp_path: Path) -> None:
    """The headline case: a fabricated capsule must not reach a signed receipt.

    Recomputing the verdict is not enough, because a verdict only means anything
    relative to a capsule. A caller who invents a capsule permitting nothing can
    hand in a self-consistent verdict and have a fully conformant run attested
    as drift.
    """
    approved = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(approved), drift=False, seal=approved)
    events = load_events(journal.path)
    assert evaluate_conformance(events, approved).conformant, "the real run is conformant"

    forged = IntentCapsule(
        v=approved.v,
        task_id=approved.task_id,
        plan_id=approved.plan_id,
        goal_digest=approved.goal_digest,
        allowed_action_classes=(),
        file_scope_globs=(),
        permitted_adapters=(),
        egress_classes=(),
        cost_envelope_ref=approved.cost_envelope_ref,
        expiry_ts=approved.expiry_ts,
    )
    forged_verdict = evaluate_conformance(events, forged)
    assert not forged_verdict.conformant, "the forged capsule makes the clean run look drifted"

    with pytest.raises(IntentCapsuleError, match="capsule"):
        _escalate(tmp_path, forged, forged_verdict)


def test_escalation_refuses_a_run_id_the_chain_did_not_sign(tmp_path: Path) -> None:
    capsule, _ = _drifted_run(tmp_path)
    decoy = _journal(tmp_path, "run-decoy", capsule_h=capsule_hash(capsule), drift=True)
    verdict = evaluate_conformance(load_events(decoy.path), capsule)

    with pytest.raises(IntentCapsuleError, match="run_id"):
        _escalate(tmp_path, capsule, verdict, run_id="run-decoy")


def test_escalation_refuses_when_the_journal_lacks_the_capsule_anchor(tmp_path: Path) -> None:
    capsule = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), bind=False, drift=True)
    verdict = evaluate_conformance(load_events(journal.path), capsule)

    with pytest.raises(IntentCapsuleError, match="capsule_bound"):
        _escalate(tmp_path, capsule, verdict)


def test_escalation_refuses_a_forged_verdict(tmp_path: Path) -> None:
    """A caller cannot have a verdict signed that the journal does not support."""
    capsule, events = _drifted_run(tmp_path)
    real = evaluate_conformance(events, capsule)
    forged = type(real)(
        conformant=False,
        capsule_hash=real.capsule_hash,
        policy_mode=real.policy_mode,
        divergences=(),
        verdict_hash="sha256:" + "0" * 64,
    )

    with pytest.raises(IntentCapsuleError, match="verdict"):
        _escalate(tmp_path, capsule, forged)


def test_escalation_refuses_a_conformant_verdict(tmp_path: Path) -> None:
    capsule = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), drift=False, seal=capsule)
    verdict = evaluate_conformance(load_events(journal.path), capsule)
    assert verdict.conformant

    with pytest.raises(IntentCapsuleError, match="conformant"):
        _escalate(tmp_path, capsule, verdict)


def test_escalation_refuses_when_the_journal_is_missing(tmp_path: Path) -> None:
    capsule, events = _drifted_run(tmp_path)
    verdict = evaluate_conformance(events, capsule)
    (_sdd(tmp_path) / "runs" / _RUN_ID / "journal.jsonl").unlink()

    with pytest.raises(IntentCapsuleError, match="journal"):
        _escalate(tmp_path, capsule, verdict)


def test_escalation_refuses_a_tampered_journal(tmp_path: Path) -> None:
    capsule, events = _drifted_run(tmp_path)
    verdict = evaluate_conformance(events, capsule)
    path = _sdd(tmp_path) / "runs" / _RUN_ID / "journal.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1], lines[-2] = lines[-2], lines[-1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(IntentCapsuleError, match="journal"):
        _escalate(tmp_path, capsule, verdict)


def test_escalation_happy_path_binds_the_approved_capsule_and_divergences(tmp_path: Path) -> None:
    """Shape check for a genuine drift on an approved capsule.

    Named for what it asserts: this is the happy path, not proof of the
    recomputation (the hash-match gate makes caller and recomputed verdict equal
    by the time the binding is built, so no assertion here can tell them apart).
    The containment is carried by the refusal tests above.
    """
    capsule, events = _drifted_run(tmp_path)
    verdict = evaluate_conformance(events, capsule)

    receipt = _escalate(tmp_path, capsule, verdict)

    assert receipt.extra_binding is not None
    assert receipt.extra_binding["capsule_hash"] == capsule_hash(capsule)
    assert receipt.extra_binding["verdict_hash"] == verdict.verdict_hash
    assert [d["action_class"] for d in receipt.extra_binding["divergent_events"]] == ["web.fetch"]


def test_escalation_recomputes_under_the_supplied_policy(tmp_path: Path) -> None:
    """The recomputation must use the same policy the caller's verdict used."""
    capsule, events = _drifted_run(tmp_path)
    policy = DriftPolicy(mode="block")
    verdict = evaluate_conformance(events, capsule, policy=policy)

    receipt = _escalate(tmp_path, capsule, verdict, policy=policy)

    assert receipt.extra_binding is not None
    assert receipt.extra_binding["verdict_hash"] == verdict.verdict_hash


# ---------------------------------------------------------------------------
# The recomputation must agree with what was actually persisted
# ---------------------------------------------------------------------------


def _approved_then_reloaded(tmp_path: Path, **overrides) -> tuple[IntentCapsule, IntentCapsule, str]:
    """Approve through the production path, then read the capsule back off disk."""
    from bernstein.core.security.audit_chain import EVENT_INTENT_CAPSULE

    in_memory = _approve(tmp_path, **overrides)
    from_disk, _ = read_capsule_binding(_sdd(tmp_path), _TASK_ID)
    assert from_disk is not None
    entries = [
        e for e in _chain(tmp_path).query(event_type=EVENT_INTENT_CAPSULE) if e.details.get("task_id") == _TASK_ID
    ]
    return in_memory, from_disk, str(entries[-1].details["capsule_hash"])


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="defaults"),
        pytest.param({"expiry_ts": 0}, id="zero-expiry"),
        pytest.param(
            {"file_scope_globs": [], "permitted_adapters": [], "egress_classes": []},
            id="empty-collections",
        ),
        pytest.param({"file_scope_globs": ["src/prix-cafe/**"]}, id="non-ascii-glob"),
        pytest.param({"permitted_adapters": ["", "claude"]}, id="empty-string-entry"),
        pytest.param({"expiry_ts": float(_FUTURE_EXPIRY)}, id="float-expiry"),
    ],
)
def test_capsule_hash_survives_the_write_read_round_trip(tmp_path: Path, overrides: dict) -> None:
    """A reloaded capsule must hash to the value the chain recorded.

    Verification compares a hash recomputed from a capsule object against the
    hash persisted in the chain, so any transformation between the in-memory
    object and the stored bytes rejects an honest capsule as tampered.
    ``from_dict`` coerces every field on read; the constructor has to apply the
    same normalisation or the two disagree. A float ``expiry_ts`` -- what an
    upstream ``time.time() + ttl`` produces, since type hints are not enforced
    -- is the case that actually diverged.
    """
    in_memory, from_disk, chain_hash = _approved_then_reloaded(tmp_path, **overrides)

    assert capsule_hash(in_memory) == chain_hash
    assert capsule_hash(from_disk) == chain_hash
    assert from_disk.to_dict() == in_memory.to_dict()


def test_verify_accepts_an_honest_capsule_with_a_float_expiry(tmp_path: Path) -> None:
    """End to end: the loosely-typed value must not read back as tampering."""
    capsule = _approve(tmp_path, expiry_ts=float(_FUTURE_EXPIRY))
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), seal=capsule)

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert result.ok, result.reason


def test_escalation_accepts_a_capsule_reloaded_from_disk(tmp_path: Path) -> None:
    """The honest path must survive persistence, not just in-memory equality.

    Escalating with the capsule object still in memory would hide any lossy
    step between the object and the stored bytes, so this reloads it first.
    """
    _approve(tmp_path)
    from_disk, sidecar_run_id = read_capsule_binding(_sdd(tmp_path), _TASK_ID)
    assert from_disk is not None
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(from_disk), drift=True, seal=from_disk)
    verdict = evaluate_conformance(load_events(journal.path), from_disk)

    receipt = _escalate(tmp_path, from_disk, verdict, run_id=sidecar_run_id or _RUN_ID)

    assert receipt.extra_binding is not None
    assert receipt.extra_binding["capsule_hash"] == capsule_hash(from_disk)
    assert [d["action_class"] for d in receipt.extra_binding["divergent_events"]] == ["web.fetch"]


def test_divergence_set_is_stable_across_journal_reloads(tmp_path: Path) -> None:
    """Ordering of the stored form and the recomputed form must not differ."""
    capsule = _approve(tmp_path, allowed_action_classes=["fs.read"])
    journal = EventJournal(_RUN_ID, _sdd(tmp_path))
    bind_capsule_into_journal(journal, task_id=_TASK_ID, capsule_hash=capsule_hash(capsule))
    for tool in ("WebFetch", "Bash", "git_push", "WebSearch"):
        journal.record("tool.call", tool=tool)

    first = evaluate_conformance(load_events(journal.path), capsule)
    second = evaluate_conformance(load_events(journal.path), capsule)

    assert len(first.divergences) == 4
    assert first.verdict_hash == second.verdict_hash
    assert [d.to_dict() for d in first.divergences] == [d.to_dict() for d in second.divergences]


# ---------------------------------------------------------------------------
# Absent and malformed input must deny, not slip past the control
# ---------------------------------------------------------------------------


def test_truncating_the_journal_does_not_launder_drift(tmp_path: Path) -> None:
    """Deleting the rows that convict you must not produce a clean verdict.

    The journal's Merkle chain recomputes from genesis using positional
    indices, so every prefix of a valid journal is itself a valid journal. A
    proof that reads only what remains cannot tell what was removed; the sealed
    head and length are the independent commitment that makes it detectable.
    """
    capsule = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), drift=True, seal=capsule)
    before = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)
    assert not before.ok and not before.conformant, "the drifted run is caught before truncation"

    lines = journal.path.read_text(encoding="utf-8").splitlines()
    journal.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    after = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert verify_journal(journal.path).ok, "a truncated prefix still verifies on its own"
    assert not after.ok
    assert not after.conformant
    assert "sealed" in after.reason


def test_appending_after_the_seal_is_rejected(tmp_path: Path) -> None:
    capsule = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), seal=capsule)
    journal.record("tool.call", tool="WebFetch", adapter="claude")

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "sealed" in result.reason


def test_an_unsealed_run_cannot_be_verified(tmp_path: Path) -> None:
    """Absent commitment is a denial, not a pass."""
    capsule = _approve(tmp_path)
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule))

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "journal_seal" in result.reason


def test_a_seal_for_another_run_does_not_satisfy_this_one(tmp_path: Path) -> None:
    capsule = _approve(tmp_path)
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule))
    _journal(tmp_path, "run-other", capsule_h=capsule_hash(capsule), seal=None)
    seal_run_journal(
        chain=_chain(tmp_path),
        sdd_dir=_sdd(tmp_path),
        task_id=_TASK_ID,
        run_id="run-other",
        capsule=capsule,
    )

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "journal_seal" in result.reason


def test_sealing_a_diverged_journal_is_refused(tmp_path: Path) -> None:
    capsule = _approve(tmp_path)
    journal = _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), drift=True)
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    lines[-1], lines[-2] = lines[-2], lines[-1]
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(IntentCapsuleError, match="diverges"):
        seal_run_journal(
            chain=_chain(tmp_path), sdd_dir=_sdd(tmp_path), task_id=_TASK_ID, run_id=_RUN_ID, capsule=capsule
        )


def test_expiry_fires_on_activity_after_approval_not_only_at_mint_time(tmp_path: Path) -> None:
    """Scanning approval entries alone makes the control structurally inert.

    An approval is by construction at or before the expiry it declares, so a
    check that only sees approvals can fire solely for a capsule minted
    already-expired. The seal, written when the run finishes, is what evidences
    a capsule still being acted on past its expiry.
    """
    import time

    capsule = _approve(tmp_path, expiry_ts=int(time.time()) + 1)
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule))
    time.sleep(1.2)
    seal_run_journal(chain=_chain(tmp_path), sdd_dir=_sdd(tmp_path), task_id=_TASK_ID, run_id=_RUN_ID, capsule=capsule)

    result = verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID)

    assert not result.ok
    assert "expired" in result.reason


def test_a_run_inside_a_live_ttl_still_verifies(tmp_path: Path) -> None:
    capsule = _approve(tmp_path, expiry_ts=_FUTURE_EXPIRY)
    _journal(tmp_path, _RUN_ID, capsule_h=capsule_hash(capsule), seal=capsule)

    assert verify_intent_conformance(sdd_dir=_sdd(tmp_path), chain=_chain(tmp_path), task_id=_TASK_ID).ok


def test_omitting_the_adapter_field_does_not_evade_the_allowlist(tmp_path: Path) -> None:
    """An allowlist that applies only when the field is volunteered is not one."""
    capsule = _capsule(permitted_adapters=["claude"])

    omitted = evaluate_conformance([{"event": "tool.call", "tool": "Edit", "path": "src/pricing/a.py"}], capsule)
    foreign = evaluate_conformance(
        [{"event": "tool.call", "tool": "Edit", "adapter": "codex", "path": "src/pricing/a.py"}], capsule
    )
    permitted = evaluate_conformance(
        [{"event": "tool.call", "tool": "Edit", "adapter": "claude", "path": "src/pricing/a.py"}], capsule
    )

    assert not omitted.conformant
    assert omitted.divergences[0].reason == "adapter_unrecorded"
    assert not foreign.conformant
    assert foreign.divergences[0].reason == "adapter_not_permitted"
    assert permitted.conformant


def test_no_declared_adapter_allowlist_stays_unconstrained(tmp_path: Path) -> None:
    capsule = _capsule(permitted_adapters=[])

    assert evaluate_conformance([{"event": "tool.call", "tool": "Read"}], capsule).conformant


@pytest.mark.parametrize(
    ("path", "globs"),
    [
        ("/tmp/evil", ("tmp/**",)),
        ("/src/pricing/x.py", ("src/pricing/**",)),
        ("/etc/passwd", ("**",)),
    ],
)
def test_absolute_paths_never_satisfy_a_workspace_relative_glob(path: str, globs: tuple[str, ...]) -> None:
    """A containment check must reject its input, not rewrite it into a pass.

    Stripping the leading separator reinterprets an absolute path as the
    workspace-relative one the scope was approved for, silently widening it.
    """
    assert not path_in_scope(path, globs)


@pytest.mark.parametrize("path", ["src/pricing/x.py", "./src/pricing/x.py", "src/pricing/nested/y.py"])
def test_workspace_relative_paths_still_match(path: str) -> None:
    assert path_in_scope(path, ("src/pricing/**",))
