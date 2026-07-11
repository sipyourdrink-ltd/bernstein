"""In-process verification hook-gate tests (issue #2360).

The scheduler-side evidence gate (issue #2362) runs a task's declared
verification producers *after* a worker finishes and re-dispatches on failure.
This module covers the in-process defence-in-depth surface: the same policy
enforced the moment a worker believes it is done, inside the session, before
the turn ends. A blocked completion or an out-of-scope write is a *gate
receipt* -- and the receipt reuses the issue #2362 evidence-bundle schema
verbatim, so a downstream verifier cannot tell (from the schema) whether the
gate fired in-process or scheduler-side.

Acceptance criteria coverage:

* AC1 -- a failing completion gate blocks the turn (``exit_code == 2``).
* AC2 -- an out-of-scope write is refused in-process and sealed as a gate
  receipt in the HMAC audit chain.
* AC3 -- an in-process receipt and a scheduler-side bundle are byte-identical
  in schema (and byte-identical bytes for identical inputs).
* AC4 -- no path enforcement configured degrades to allow-through with no
  policy weakening.
* Security -- a traversal / symlink path escapes the allowlist and is refused
  by realpath containment.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    parse_producers,
    run_evidence_gate,
    run_producers,
)
from bernstein.core.security.hook_gate import (
    ALLOW_EXIT_CODE,
    BLOCKING_EXIT_CODE,
    HookGatePolicy,
    evaluate_completion_gate,
    evaluate_path_gate,
    path_within_allowlist,
    policy_from_task_fields,
    read_policy,
    seal_gate_receipt,
    write_policy,
)

if TYPE_CHECKING:
    from pathlib import Path


def _isolate_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the install audit key under tmp so sealing is fully cwd-independent."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _pass_producer() -> EvidenceProducer:
    return EvidenceProducer(
        name="tests",
        kind="test",
        command=(sys.executable, "-c", "print('ok')"),
        required=True,
    )


def _fail_producer() -> EvidenceProducer:
    return EvidenceProducer(
        name="tests",
        kind="test",
        command=(sys.executable, "-c", "import sys; sys.exit(1)"),
        required=True,
    )


# ---------------------------------------------------------------------------
# Policy parsing + persistence
# ---------------------------------------------------------------------------


def test_policy_from_task_fields_maps_owned_files_and_producers() -> None:
    policy = policy_from_task_fields(
        "T-1",
        owned_files=["src/pkg/**", "docs/x.md"],
        evidence_producers=[
            {"name": "tests", "kind": "test", "command": ["pytest"], "required": True},
            {"name": "shot", "kind": "screenshot", "command": ["cap"], "required": False},
        ],
    )
    assert policy.task_id == "T-1"
    assert policy.path_allowlist == ("src/pkg/**", "docs/x.md")
    # Only required producers gate completion.
    assert policy.enforces_completion is True
    assert policy.enforces_paths is True
    assert policy.is_active is True


def test_empty_policy_is_inactive() -> None:
    policy = policy_from_task_fields("T-empty", owned_files=[], evidence_producers=[])
    assert policy.is_active is False
    assert policy.enforces_paths is False
    assert policy.enforces_completion is False


def test_policy_round_trips_through_disk(tmp_path: Path) -> None:
    policy = policy_from_task_fields(
        "T-rt",
        owned_files=["src/**"],
        evidence_producers=[{"name": "t", "kind": "test", "command": ["a", "b"], "required": True}],
    )
    written = write_policy(tmp_path, "role-abc123", policy)
    assert written.is_file()
    loaded = read_policy(tmp_path, "role-abc123")
    assert loaded == policy


def test_read_policy_absent_is_none(tmp_path: Path) -> None:
    assert read_policy(tmp_path, "role-missing") is None


# ---------------------------------------------------------------------------
# Path allowlist decision (+ realpath containment)
# ---------------------------------------------------------------------------


def test_path_within_allowlist_accepts_in_scope(tmp_path: Path) -> None:
    assert path_within_allowlist("src/pkg/mod.py", workdir=tmp_path, allowlist=["src/**"]) is True
    assert path_within_allowlist("src/pkg/mod.py", workdir=tmp_path, allowlist=["src/pkg"]) is True


def test_path_within_allowlist_refuses_out_of_scope(tmp_path: Path) -> None:
    assert path_within_allowlist("infra/prod.tf", workdir=tmp_path, allowlist=["src/**"]) is False


def test_empty_allowlist_allows_everything(tmp_path: Path) -> None:
    # AC4: no path enforcement configured -> allow-through, no weakening.
    assert path_within_allowlist("anything/at/all.py", workdir=tmp_path, allowlist=[]) is True


def test_traversal_escape_is_refused(tmp_path: Path) -> None:
    # Security: realpath containment rejects a ".." escape even if the textual
    # prefix would otherwise match.
    assert path_within_allowlist("src/../../etc/passwd", workdir=tmp_path, allowlist=["src/**"]) is False


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "src" / "leak"
    link.symlink_to(outside, target_is_directory=True)
    # A write "through" the in-scope symlink resolves outside the worktree.
    assert path_within_allowlist("src/leak/secret.py", workdir=tmp_path, allowlist=["src/**"]) is False


# ---------------------------------------------------------------------------
# PreToolUse path gate (AC2 decision)
# ---------------------------------------------------------------------------


def test_evaluate_path_gate_blocks_out_of_scope_write(tmp_path: Path) -> None:
    policy = policy_from_task_fields("T-2", owned_files=["src/**"], evidence_producers=[])
    outcome = evaluate_path_gate(
        policy,
        tool_name="Write",
        tool_input={"file_path": "infra/prod.tf", "content": "x"},
        workdir=tmp_path,
    )
    assert outcome is not None
    assert outcome.blocked is True
    assert outcome.exit_code == BLOCKING_EXIT_CODE
    assert outcome.event == "pretooluse"
    assert outcome.outcomes  # a synthetic refusal producer is present


def test_evaluate_path_gate_allows_in_scope_write(tmp_path: Path) -> None:
    policy = policy_from_task_fields("T-3", owned_files=["src/**"], evidence_producers=[])
    outcome = evaluate_path_gate(
        policy,
        tool_name="Edit",
        tool_input={"file_path": "src/pkg/mod.py"},
        workdir=tmp_path,
    )
    assert outcome is not None
    assert outcome.blocked is False
    assert outcome.exit_code == ALLOW_EXIT_CODE


def test_evaluate_path_gate_ignores_non_edit_tools(tmp_path: Path) -> None:
    policy = policy_from_task_fields("T-4", owned_files=["src/**"], evidence_producers=[])
    assert evaluate_path_gate(policy, tool_name="Bash", tool_input={"command": "ls"}, workdir=tmp_path) is None


def test_evaluate_path_gate_noop_without_allowlist(tmp_path: Path) -> None:
    # AC4: absent path enforcement -> no decision to record.
    policy = policy_from_task_fields("T-5", owned_files=[], evidence_producers=[])
    assert evaluate_path_gate(policy, tool_name="Write", tool_input={"file_path": "x.py"}, workdir=tmp_path) is None


# ---------------------------------------------------------------------------
# Completion gate (AC1)
# ---------------------------------------------------------------------------


def test_completion_gate_blocks_on_failing_verification(tmp_path: Path) -> None:
    policy = HookGatePolicy("T-6", producers=(_fail_producer(),), path_allowlist=())
    outcome = evaluate_completion_gate(policy, workdir=tmp_path)
    assert outcome.event == "completion"
    assert outcome.blocked is True
    assert outcome.exit_code == BLOCKING_EXIT_CODE


def test_completion_gate_passes_when_verification_passes(tmp_path: Path) -> None:
    policy = HookGatePolicy("T-7", producers=(_pass_producer(),), path_allowlist=())
    outcome = evaluate_completion_gate(policy, workdir=tmp_path)
    assert outcome.blocked is False
    assert outcome.exit_code == ALLOW_EXIT_CODE


def test_completion_gate_without_required_producers_allows(tmp_path: Path) -> None:
    # AC4: no completion enforcement -> pass-through.
    policy = HookGatePolicy(
        "T-8",
        producers=(EvidenceProducer("adv", "generic", ("true",), required=False),),
        path_allowlist=(),
    )
    outcome = evaluate_completion_gate(policy, workdir=tmp_path)
    assert outcome.blocked is False


# ---------------------------------------------------------------------------
# Receipt sealing into the chain (AC2 receipt)
# ---------------------------------------------------------------------------


def test_refusal_seals_gate_receipt_in_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    policy = policy_from_task_fields("T-9", owned_files=["src/**"], evidence_producers=[])
    outcome = evaluate_path_gate(
        policy,
        tool_name="Write",
        tool_input={"file_path": "infra/prod.tf"},
        workdir=tmp_path,
    )
    assert outcome is not None and outcome.blocked
    bundle = seal_gate_receipt(
        workdir=tmp_path,
        task_id="T-9#gate:pretooluse",
        outcomes=outcome.outcomes,
        timestamp=1_700_000_000,
    )
    assert bundle.gate_passed is False
    assert bundle.journal_entry_hash
    assert bundle.signature

    from bernstein.core.security.audit_chain import EVENT_EVIDENCE_BUNDLE, AuditChainStore

    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    events = chain.query(event_type=EVENT_EVIDENCE_BUNDLE)
    assert any(e.details.get("gate_passed") is False for e in events)


# ---------------------------------------------------------------------------
# Schema parity in-process vs scheduler-side (AC3)
# ---------------------------------------------------------------------------


def test_receipt_schema_is_byte_identical_to_scheduler_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    ts = 1_700_000_100
    specs = [{"name": "tests", "kind": "test", "command": [sys.executable, "-c", "print('x')"], "required": True}]
    producers = parse_producers(specs)

    # Scheduler-side authoritative gate.
    sched_dir = tmp_path / "sched"
    sched_dir.mkdir()
    sched_bundle, _ = run_evidence_gate(workdir=sched_dir, task_id="T-parity", producers=producers, timestamp=ts)

    # In-process gate: same producers, run in-session, sealed as a receipt.
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    outcomes = run_producers(producers, runner=_subprocess_runner(proc_dir))
    proc_bundle = seal_gate_receipt(workdir=proc_dir, task_id="T-parity", outcomes=outcomes, timestamp=ts)

    # Same schema (identical key structure + version) ...
    assert set(sched_bundle.to_dict()) == set(proc_bundle.to_dict())
    assert sched_bundle.schema_version == proc_bundle.schema_version
    # ... and, for identical inputs, byte-identical canonical bindings.
    assert sched_bundle.to_canonical_bytes() == proc_bundle.to_canonical_bytes()


def _subprocess_runner(cwd: Path):
    from bernstein.core.evidence.bundle import _default_runner

    return _default_runner(cwd, timeout=60)


# ---------------------------------------------------------------------------
# The gate receipt IS a verifiable evidence bundle (verifiability axis)
# ---------------------------------------------------------------------------


def test_sealed_gate_receipt_verifies_offline_and_detects_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_audit_key(tmp_path, monkeypatch)
    from bernstein.core.evidence.bundle import EvidenceStore, verify_evidence_bundle
    from bernstein.core.security.audit import load_or_create_audit_key

    policy = policy_from_task_fields("T-verify", owned_files=["src/**"], evidence_producers=[])
    outcome = evaluate_path_gate(policy, tool_name="Write", tool_input={"file_path": "infra/prod.tf"}, workdir=tmp_path)
    assert outcome is not None and outcome.blocked
    task_id = "T-verify#gate:pretooluse:1700000000"
    bundle = seal_gate_receipt(workdir=tmp_path, task_id=task_id, outcomes=outcome.outcomes, timestamp=1_700_000_000)

    hmac_key = load_or_create_audit_key()
    ok = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        task_id=task_id,
    )
    assert ok.ok is True

    # Tamper the stored refusal blob: verification must fail and name the item.
    store = EvidenceStore(tmp_path / ".sdd" / "evidence")
    blob_path = store.blob_path(bundle.items[0].content_hash)
    blob_path.write_bytes(b"forged: this write was actually allowed\n")
    tampered = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        task_id=task_id,
    )
    assert tampered.ok is False
