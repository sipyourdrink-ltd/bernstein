"""Receipt-gated adapter admission (#2610).

Covers the deterministic replay fingerprint, the symmetric admit/refuse
receipt shape, the sealed-receipt gate on ``get_adapter``, staleness and
fingerprint-drift detection, and the audit-chain anchor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.adapters.admission import (
    ADMISSION_EXEMPT,
    CANARY_GREEN,
    CANARY_RED,
    CANARY_UNKNOWN,
    GATE_RECEIPT_KIND,
    POLICY_ENFORCE,
    POLICY_OFF,
    POLICY_WARN,
    REASON_CANARY_RED,
    REASON_CONFORMANCE_SKIP,
    REASON_FINGERPRINT_MISMATCH,
    REASON_NO_CONTRACT,
    REASON_NO_RECEIPT,
    REASON_NO_TRANSCRIPT,
    REASON_RECEIPT_STALE,
    REASON_RECEIPT_TAMPERED,
    RECEIPT_KIND,
    VERDICT_ADMIT,
    VERDICT_REFUSE,
    AdapterAdmissionReceipt,
    AdapterAdmissionRefusal,
    AdmissionEvidence,
    AdmissionGate,
    audit_admission_no_unproven_spawn,
    build_admission_receipt,
    capability_split,
    evaluate_admission,
    gather_admission_evidence,
    load_admission_receipt,
    policy_from_env,
    receipt_is_stale,
    receipt_sha256,
    replay_fingerprint,
    seal_admission_receipt,
    verify_admission_receipt,
    write_admission_receipt,
)
from bernstein.adapters.conformance import StepResult, TranscriptResult
from bernstein.adapters.registry import get_adapter

if TYPE_CHECKING:
    from collections.abc import Iterator

GOLDEN_DIR = Path(__file__).parents[1] / "golden"

# A contract whose ``help_command`` is a self-contained subprocess printing
# exactly the required tokens, so the in-process conformance probe returns
# ``ok`` on any host without installing an upstream CLI.
_OK_CONTRACT = """\
adapter: kimi
binary: kimi
install:
  method: uv
  spec: "kimi-cli"
auth:
  required_for_help: false
  required_for_models: false
  secret_env: "MOONSHOT_API_KEY"
required_flags:
  - "--yolo"
  - "-c"
required_subcommands: []
help_command: ["python3", "-c", "print('usage: kimi --yolo -c PROMPT')"]
expected_models:
  command: []
  required_present: []
"""

_NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def contracts_dir(tmp_path: Path) -> Path:
    """A contracts directory whose ``kimi`` contract probes green."""
    target = tmp_path / "contracts"
    target.mkdir()
    (target / "kimi.yaml").write_text(_OK_CONTRACT, encoding="utf-8")
    return target


@pytest.fixture
def receipts_dir(tmp_path: Path) -> Path:
    """An empty sealed-receipt directory."""
    target = tmp_path / "receipts"
    target.mkdir()
    return target


def _which(_binary: str) -> str:
    return "/usr/local/bin/kimi"


def _version(_binary: str) -> str:
    return "kimi 0.9.1"


def _evidence(contracts_dir: Path, *, canary: str = CANARY_GREEN, version: str = "kimi 0.9.1") -> AdmissionEvidence:
    return gather_admission_evidence(
        "kimi",
        contracts_dir=contracts_dir,
        golden_dir=GOLDEN_DIR,
        which=_which,
        version_probe=lambda _b: version,
        canary_verdict=canary,
    )


# ---------------------------------------------------------------------------
# Determinism of the replay fingerprint
# ---------------------------------------------------------------------------


def test_replay_fingerprint_is_byte_identical_across_runs(contracts_dir: Path) -> None:
    """Two independent derivations against the same binary version agree."""
    first = _evidence(contracts_dir)
    second = _evidence(contracts_dir)

    assert first.replay_fingerprint == second.replay_fingerprint
    assert first.replay_fingerprint.startswith("sha256:")


def test_replay_fingerprint_changes_when_a_contract_token_changes(contracts_dir: Path) -> None:
    """Editing one contract token moves the fingerprint."""
    before = _evidence(contracts_dir).replay_fingerprint

    mutated = _OK_CONTRACT.replace('  - "-c"', '  - "-c"\n  - "--print"')
    (contracts_dir / "kimi.yaml").write_text(mutated, encoding="utf-8")
    after = _evidence(contracts_dir).replay_fingerprint

    assert before != after


def test_replay_fingerprint_changes_when_the_binary_version_changes(contracts_dir: Path) -> None:
    """A different installed version derives a different fingerprint."""
    before = _evidence(contracts_dir, version="kimi 0.9.1").replay_fingerprint
    after = _evidence(contracts_dir, version="kimi 1.0.0").replay_fingerprint

    assert before != after


def test_replay_fingerprint_ignores_step_messages() -> None:
    """Free-text step messages stay out of the preimage.

    Messages can embed a temporary working directory; folding them in would
    make the fingerprint host-dependent and destroy its whole purpose.
    """
    base = TranscriptResult(
        transcript_name="t",
        adapter_class="pkg.Adapter",
        step_results=[StepResult(step_index=0, passed=True, message="OK - pid=1 in /tmp/aaa")],
    )
    other = TranscriptResult(
        transcript_name="t",
        adapter_class="pkg.Adapter",
        step_results=[StepResult(step_index=0, passed=True, message="OK - pid=1 in /tmp/zzz")],
    )
    kwargs = {"contract_hash": "abc", "installed_version": "1.0.0"}

    assert replay_fingerprint("x", results=[base], **kwargs) == replay_fingerprint("x", results=[other], **kwargs)


def test_replay_fingerprint_is_order_independent() -> None:
    """Transcript enumeration order does not perturb the fingerprint."""
    a = TranscriptResult(transcript_name="a", adapter_class="pkg.A", step_results=[])
    b = TranscriptResult(transcript_name="b", adapter_class="pkg.B", step_results=[])
    kwargs = {"contract_hash": "abc", "installed_version": "1.0.0"}

    assert replay_fingerprint("x", results=[a, b], **kwargs) == replay_fingerprint("x", results=[b, a], **kwargs)


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------


def _bare_evidence(**overrides: object) -> AdmissionEvidence:
    fields: dict[str, object] = {
        "adapter": "kimi",
        "binary": "kimi",
        "binary_path": "/usr/local/bin/kimi",
        "installed_version": "kimi 0.9.1",
        "contract_hash": "deadbeef",
        "profile_hash": "cafe",
        "conformance_verdict": "ok",
        "conformance_detail": "",
        "transcript_names": ("kimi_adapter_spawn",),
        "replay_passed": True,
        "canary_verdict": CANARY_GREEN,
        "replay_fingerprint": "sha256:aaaa",
    }
    fields.update(overrides)
    return AdmissionEvidence(**fields)  # type: ignore[arg-type]


def test_green_evidence_admits() -> None:
    decision = evaluate_admission(_bare_evidence())

    assert decision.admitted
    assert decision.verdict == VERDICT_ADMIT
    assert decision.reason == ""
    assert "spawn" in decision.allowed_capabilities


def test_conformance_skip_refuses_rather_than_passing_through() -> None:
    """An inconclusive probe is not permission - the point of the module."""
    decision = evaluate_admission(_bare_evidence(conformance_verdict="skip", conformance_detail="binary missing"))

    assert not decision.admitted
    assert decision.reason == REASON_CONFORMANCE_SKIP
    assert decision.remediation


def test_missing_contract_refuses() -> None:
    decision = evaluate_admission(_bare_evidence(contract_hash=""))

    assert decision.reason == REASON_NO_CONTRACT


def test_missing_transcript_refuses() -> None:
    decision = evaluate_admission(_bare_evidence(replay_passed=None, transcript_names=()))

    assert decision.reason == REASON_NO_TRANSCRIPT


def test_red_canary_refuses() -> None:
    decision = evaluate_admission(_bare_evidence(canary_verdict=CANARY_RED))

    assert decision.reason == REASON_CANARY_RED


def test_refusal_forbids_every_capability_including_spawn() -> None:
    """A refusal states positively that no authority was granted."""
    decision = evaluate_admission(_bare_evidence(conformance_verdict="skip"))

    assert decision.allowed_capabilities == ()
    assert "spawn" in decision.forbidden_capabilities
    assert "mcp_client" in decision.forbidden_capabilities


def test_capability_split_reflects_the_declared_profile() -> None:
    """An admitted adapter is granted exactly what its profile declares."""
    allowed, forbidden = capability_split("pydantic_ai", admitted=True)

    assert "spawn" in allowed
    assert "local_models" in allowed
    assert "computer_use" in forbidden
    assert not set(allowed) & set(forbidden)


# ---------------------------------------------------------------------------
# Receipt shape
# ---------------------------------------------------------------------------


def test_refusal_receipt_carries_every_operator_field() -> None:
    """A refusal is as informative as an admission, not a silence."""
    decision = evaluate_admission(_bare_evidence(conformance_verdict="skip", conformance_detail="binary missing"))
    receipt = build_admission_receipt(decision, generated_at=_NOW.isoformat())

    for key in (
        "adapter",
        "binary",
        "binary_path",
        "installed_version",
        "probe_hash",
        "contract_hash",
        "replay_fingerprint",
        "conformance_run_id",
        "conformance_verdict",
        "canary_verdict",
        "verdict",
        "reason",
        "allowed_capabilities",
        "forbidden_capabilities",
        "admission_ttl_seconds",
        "remediation",
    ):
        assert key in receipt, key
    assert receipt["verdict"] == VERDICT_REFUSE
    assert receipt["reason"] == REASON_CONFORMANCE_SKIP
    assert receipt["remediation"]
    assert receipt["allowed_capabilities"] == []


def test_receipt_is_deterministic_for_a_fixed_timestamp() -> None:
    decision = evaluate_admission(_bare_evidence())
    first = build_admission_receipt(decision, generated_at=_NOW.isoformat())
    second = build_admission_receipt(decision, generated_at=_NOW.isoformat())

    assert receipt_sha256(first) == receipt_sha256(second)


def test_tampered_receipt_fails_its_content_hash(receipts_dir: Path) -> None:
    """Editing a refusal into an admission breaks the content address."""
    decision = evaluate_admission(_bare_evidence(conformance_verdict="skip"))
    receipt = build_admission_receipt(decision, generated_at=_NOW.isoformat())
    path = write_admission_receipt(receipts_dir, receipt)

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert verify_admission_receipt(doc)

    doc["receipt"]["verdict"] = VERDICT_ADMIT
    doc["receipt"]["reason"] = ""
    doc["receipt"]["allowed_capabilities"] = ["spawn"]
    path.write_text(json.dumps(doc), encoding="utf-8")

    assert not verify_admission_receipt(json.loads(path.read_text(encoding="utf-8")))
    stored, problem = load_admission_receipt(receipts_dir, "kimi")
    assert stored is None
    assert problem == REASON_RECEIPT_TAMPERED


def test_write_admission_receipt_rejects_a_hostile_adapter_name(receipts_dir: Path) -> None:
    decision = evaluate_admission(_bare_evidence(adapter="../../etc/passwd"))
    receipt = build_admission_receipt(decision, generated_at=_NOW.isoformat())

    with pytest.raises(ValueError, match="invalid adapter name"):
        write_admission_receipt(receipts_dir, receipt)


def test_receipt_is_stale_past_its_ttl() -> None:
    decision = evaluate_admission(_bare_evidence())
    receipt = build_admission_receipt(decision, generated_at=_NOW.isoformat())

    assert not receipt_is_stale(receipt, now=_NOW + timedelta(hours=1))
    assert receipt_is_stale(receipt, now=_NOW + timedelta(days=2))


def test_unparseable_timestamp_is_treated_as_stale() -> None:
    decision = evaluate_admission(_bare_evidence())
    receipt = build_admission_receipt(decision, generated_at="not-a-timestamp")

    assert receipt_is_stale(receipt, now=_NOW)


def test_load_admission_receipt_ignores_gate_receipts(receipts_dir: Path) -> None:
    """A gate decision can never masquerade as the evidence it checked."""
    decision = evaluate_admission(_bare_evidence())
    gate_receipt = build_admission_receipt(decision, generated_at=_NOW.isoformat(), kind=GATE_RECEIPT_KIND)
    write_admission_receipt(receipts_dir, gate_receipt)

    stored, problem = load_admission_receipt(receipts_dir, "kimi")
    assert stored is None
    assert problem == REASON_NO_RECEIPT


def test_load_admission_receipt_returns_the_newest(receipts_dir: Path) -> None:
    old = build_admission_receipt(evaluate_admission(_bare_evidence()), generated_at=_NOW.isoformat())
    new = build_admission_receipt(
        evaluate_admission(_bare_evidence(installed_version="kimi 1.0.0")),
        generated_at=(_NOW + timedelta(hours=3)).isoformat(),
    )
    write_admission_receipt(receipts_dir, old)
    write_admission_receipt(receipts_dir, new)

    stored, problem = load_admission_receipt(receipts_dir, "kimi")
    assert problem == ""
    assert stored is not None
    assert stored["installed_version"] == "kimi 1.0.0"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _gate(receipts_dir: Path, contracts_dir: Path, **overrides: object) -> AdmissionGate:
    fields: dict[str, object] = {
        "receipts_dir": receipts_dir,
        "policy": POLICY_ENFORCE,
        "contracts_dir": contracts_dir,
        "golden_dir": GOLDEN_DIR,
        "now": _NOW,
        "which": _which,
        "version_probe": _version,
    }
    fields.update(overrides)
    return AdmissionGate(**fields)  # type: ignore[arg-type]


def test_gate_refuses_when_no_receipt_is_sealed(receipts_dir: Path, contracts_dir: Path) -> None:
    """Green live evidence alone does not admit: the receipt is the gate."""
    with pytest.raises(AdapterAdmissionRefusal) as excinfo:
        _gate(receipts_dir, contracts_dir).admit("kimi")

    assert excinfo.value.receipt["reason"] == REASON_NO_RECEIPT
    assert excinfo.value.receipt["verdict"] == VERDICT_REFUSE
    assert excinfo.value.receipt_sha256


def test_gate_admits_once_the_receipt_is_sealed(receipts_dir: Path, contracts_dir: Path) -> None:
    evidence = _evidence(contracts_dir)
    seal_admission_receipt(
        evaluate_admission(evidence),
        receipts_dir=receipts_dir,
        generated_at=_NOW.isoformat(),
    )

    decision = _gate(receipts_dir, contracts_dir).admit("kimi")

    assert decision is not None
    assert decision.admitted


def test_stripping_the_receipt_makes_the_adapter_unspawnable(receipts_dir: Path, contracts_dir: Path) -> None:
    """Removing the anchored receipt withdraws authority, not just the log."""
    evidence = _evidence(contracts_dir)
    _, _, path = seal_admission_receipt(
        evaluate_admission(evidence),
        receipts_dir=receipts_dir,
        generated_at=_NOW.isoformat(),
    )
    assert _gate(receipts_dir, contracts_dir).admit("kimi") is not None

    path.unlink()

    with pytest.raises(AdapterAdmissionRefusal) as excinfo:
        _gate(receipts_dir, contracts_dir).admit("kimi")
    assert excinfo.value.receipt["reason"] == REASON_NO_RECEIPT


def test_gate_refuses_a_stale_receipt(receipts_dir: Path, contracts_dir: Path) -> None:
    seal_admission_receipt(
        evaluate_admission(_evidence(contracts_dir)),
        receipts_dir=receipts_dir,
        generated_at=(_NOW - timedelta(days=3)).isoformat(),
    )

    with pytest.raises(AdapterAdmissionRefusal) as excinfo:
        _gate(receipts_dir, contracts_dir).admit("kimi")
    assert excinfo.value.receipt["reason"] == REASON_RECEIPT_STALE


def test_gate_refuses_when_the_binary_drifted_under_the_receipt(
    receipts_dir: Path,
    contracts_dir: Path,
) -> None:
    """A receipt sealed against another version no longer attests this one."""
    seal_admission_receipt(
        evaluate_admission(_evidence(contracts_dir, version="kimi 0.1.0")),
        receipts_dir=receipts_dir,
        generated_at=_NOW.isoformat(),
    )

    with pytest.raises(AdapterAdmissionRefusal) as excinfo:
        _gate(receipts_dir, contracts_dir).admit("kimi")
    assert excinfo.value.receipt["reason"] == REASON_FINGERPRINT_MISMATCH


def test_gate_refuses_a_red_canary_attestation(receipts_dir: Path, contracts_dir: Path) -> None:
    """A sealed receipt does not survive the canary going red under it."""
    seal_admission_receipt(
        evaluate_admission(_evidence(contracts_dir)),
        receipts_dir=receipts_dir,
        generated_at=_NOW.isoformat(),
    )

    with pytest.raises(AdapterAdmissionRefusal) as excinfo:
        _gate(receipts_dir, contracts_dir, canary_verdict=CANARY_RED).admit("kimi")
    assert excinfo.value.receipt["reason"] == REASON_CANARY_RED


def test_warn_policy_records_the_refusal_without_blocking(
    receipts_dir: Path,
    contracts_dir: Path,
) -> None:
    """The gate becomes observable before it becomes blocking."""
    decision = _gate(receipts_dir, contracts_dir, policy=POLICY_WARN).admit("kimi")

    assert decision is not None
    assert not decision.admitted
    assert decision.reason == REASON_NO_RECEIPT


def test_off_policy_is_a_no_op(receipts_dir: Path, contracts_dir: Path) -> None:
    assert _gate(receipts_dir, contracts_dir, policy=POLICY_OFF).admit("kimi") is None


@pytest.mark.parametrize("name", sorted(ADMISSION_EXEMPT))
def test_exempt_adapters_are_never_withheld(name: str, receipts_dir: Path, contracts_dir: Path) -> None:
    """The offline/dev escape hatch: mock and generic always resolve."""
    assert _gate(receipts_dir, contracts_dir).admit(name) is None


def test_gate_writes_a_decision_receipt_when_asked(
    tmp_path: Path,
    receipts_dir: Path,
    contracts_dir: Path,
) -> None:
    decisions = tmp_path / "decisions"

    with pytest.raises(AdapterAdmissionRefusal):
        _gate(receipts_dir, contracts_dir, decisions_dir=decisions).admit("kimi")

    written = sorted(decisions.glob("kimi-*.json"))
    assert len(written) == 1
    doc = json.loads(written[0].read_text(encoding="utf-8"))
    assert verify_admission_receipt(doc)
    assert doc["receipt"]["kind"] == GATE_RECEIPT_KIND


# ---------------------------------------------------------------------------
# get_adapter integration
# ---------------------------------------------------------------------------


def test_get_adapter_without_a_gate_is_unchanged() -> None:
    """Enumeration surfaces keep resolving adapters they may not spawn."""
    assert get_adapter("kimi") is not None


def test_get_adapter_refuses_under_an_enforcing_gate(receipts_dir: Path, contracts_dir: Path) -> None:
    with pytest.raises(AdapterAdmissionRefusal):
        get_adapter("kimi", admission_gate=_gate(receipts_dir, contracts_dir))


def test_get_adapter_admits_through_the_gate_once_sealed(receipts_dir: Path, contracts_dir: Path) -> None:
    seal_admission_receipt(
        evaluate_admission(_evidence(contracts_dir)),
        receipts_dir=receipts_dir,
        generated_at=_NOW.isoformat(),
    )

    adapter = get_adapter("kimi", admission_gate=_gate(receipts_dir, contracts_dir))

    assert adapter.name() == "Kimi"


def test_get_adapter_reports_an_unknown_name_before_consulting_the_gate(
    receipts_dir: Path,
    contracts_dir: Path,
) -> None:
    with pytest.raises(ValueError, match="Unknown adapter"):
        get_adapter("definitely-not-an-adapter", admission_gate=_gate(receipts_dir, contracts_dir))


# ---------------------------------------------------------------------------
# Audit-chain anchoring
# ---------------------------------------------------------------------------


def test_receipt_is_anchored_in_the_audit_chain(tmp_path: Path, receipts_dir: Path, contracts_dir: Path) -> None:
    from bernstein.core.security.audit_chain import (
        EVENT_ADAPTER_ADMISSION_RECEIPT,
        AuditChainStore,
    )

    chain = AuditChainStore(tmp_path / "audit")
    _, sha, _ = seal_admission_receipt(
        evaluate_admission(_evidence(contracts_dir)),
        receipts_dir=receipts_dir,
        generated_at=_NOW.isoformat(),
        chain=chain,
    )

    rows = chain.query(event_type=EVENT_ADAPTER_ADMISSION_RECEIPT)
    assert [row.details["receipt_sha256"] for row in rows] == [sha]
    assert rows[0].details["adapter"] == "kimi"
    assert rows[0].details["verdict"] == VERDICT_ADMIT
    assert rows[0].details["kind"] == RECEIPT_KIND


def test_refusals_are_anchored_too(tmp_path: Path, receipts_dir: Path, contracts_dir: Path) -> None:
    """The negative path leaves a record, so a skip is never silent."""
    from bernstein.core.security.audit_chain import (
        EVENT_ADAPTER_ADMISSION_RECEIPT,
        AuditChainStore,
    )

    chain = AuditChainStore(tmp_path / "audit")
    with pytest.raises(AdapterAdmissionRefusal):
        _gate(receipts_dir, contracts_dir, chain=chain).admit("kimi")

    rows = chain.query(event_type=EVENT_ADAPTER_ADMISSION_RECEIPT)
    assert len(rows) == 1
    assert rows[0].details["verdict"] == VERDICT_REFUSE
    assert rows[0].details["reason"] == REASON_NO_RECEIPT
    assert rows[0].details["kind"] == GATE_RECEIPT_KIND
    assert "spawn" in rows[0].details["forbidden_capabilities"]


def test_receipt_is_anchored_as_a_signed_lineage_entry(tmp_path: Path) -> None:
    """The receipt rides the same signed spine every other receipt does."""
    from bernstein.adapters.admission import anchor_admission_receipt_in_lineage
    from bernstein.core.lineage.entry import canonicalise
    from bernstein.core.lineage.identity import AgentCard, generate_keypair, verify_detached
    from bernstein.core.lineage.store import LineageStore

    private_key_pem, public_key_pem = generate_keypair()
    card = AgentCard(agent_id="agent:admission-1", kid="adm-key-1", public_key_pem=public_key_pem)
    store = LineageStore(tmp_path / "lineage")

    decision = evaluate_admission(_bare_evidence(conformance_verdict="skip"))
    receipt = build_admission_receipt(decision, generated_at=_NOW.isoformat())

    entry_hash = anchor_admission_receipt_in_lineage(
        receipt,
        store=store,
        operator_hmac_key=b"k" * 32,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=private_key_pem,
        ts_ns=1_700_000_000_000_000_000,
    )

    assert entry_hash.startswith("sha256:")
    entries = [pair for pair in store.read_log() if pair[0].artefact_path == ".sdd/adapters/admission/kimi.json"]
    assert len(entries) == 1
    entry, jws = entries[0]
    assert verify_detached(canonicalise(entry), jws, card)
    # The receipt's content address rides in the signed entry, so the
    # signature covers the identity of the refusal, not just its bytes.
    assert entry.span_id == receipt_sha256(receipt)


def test_chain_slice_audit_names_every_unproven_spawn() -> None:
    events = [
        {"kind": GATE_RECEIPT_KIND, "adapter": "kimi", "verdict": VERDICT_ADMIT, "reason": ""},
        {
            "kind": GATE_RECEIPT_KIND,
            "adapter": "droid",
            "verdict": VERDICT_REFUSE,
            "reason": REASON_CONFORMANCE_SKIP,
            "replay_fingerprint": "sha256:abc",
        },
        {"kind": RECEIPT_KIND, "adapter": "droid", "verdict": VERDICT_REFUSE, "reason": "ignored"},
    ]

    ok, violations = audit_admission_no_unproven_spawn(events)

    assert not ok
    assert len(violations) == 1
    assert "droid" in violations[0]
    assert REASON_CONFORMANCE_SKIP in violations[0]


def test_clean_slice_proves_no_unproven_spawn() -> None:
    events = [{"kind": GATE_RECEIPT_KIND, "adapter": "kimi", "verdict": VERDICT_ADMIT, "reason": ""}]

    ok, violations = audit_admission_no_unproven_spawn(events)

    assert ok
    assert violations == []


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", POLICY_WARN),
        ("warn", POLICY_WARN),
        ("enforce", POLICY_ENFORCE),
        ("block", POLICY_ENFORCE),
        ("strict", POLICY_ENFORCE),
        ("off", POLICY_OFF),
        ("0", POLICY_OFF),
        ("nonsense", POLICY_WARN),
    ],
)
def test_policy_from_env(raw: str, expected: str) -> None:
    assert policy_from_env({"BERNSTEIN_ADAPTER_ADMISSION_POLICY": raw}) == expected


def test_policy_defaults_to_warn_when_unset() -> None:
    assert policy_from_env({}) == POLICY_WARN


# ---------------------------------------------------------------------------
# Typed receipt view
# ---------------------------------------------------------------------------


def test_receipt_view_reads_the_body() -> None:
    decision = evaluate_admission(_bare_evidence())
    body = build_admission_receipt(decision, generated_at=_NOW.isoformat())
    view = AdapterAdmissionReceipt(body)

    assert view.adapter == "kimi"
    assert view.admitted
    assert view.reason == ""
    assert view.replay_fingerprint == "sha256:aaaa"
    assert view.sha256 == receipt_sha256(body)


def test_receipt_view_tolerates_a_malformed_ttl() -> None:
    view = AdapterAdmissionReceipt({"admission_ttl_seconds": "not-a-number"})

    assert view.ttl_seconds > 0


# ---------------------------------------------------------------------------
# Evidence gathering against the shipped tree
# ---------------------------------------------------------------------------


def test_shipped_adapters_have_golden_transcripts() -> None:
    """droid, kimi and opencode replay clean against their transcripts."""
    for name in ("droid", "kimi", "opencode"):
        evidence = gather_admission_evidence(
            name,
            golden_dir=GOLDEN_DIR,
            which=lambda _b: None,
            version_probe=lambda _b: None,
            canary_verdict=CANARY_UNKNOWN,
        )
        assert evidence.transcript_names, name
        assert evidence.replay_passed is True, name
        assert evidence.contract_hash, name


def test_adapter_without_a_contract_refuses(tmp_path: Path) -> None:
    """An uncontracted adapter is refused, not waved through."""
    empty = tmp_path / "no-contracts"
    empty.mkdir()

    evidence = gather_admission_evidence(
        "kimi",
        contracts_dir=empty,
        golden_dir=GOLDEN_DIR,
        which=lambda _b: None,
        version_probe=lambda _b: None,
        canary_verdict=CANARY_UNKNOWN,
    )

    assert evaluate_admission(evidence).reason == REASON_NO_CONTRACT


# ---------------------------------------------------------------------------
# ``bernstein adapters verify``
# ---------------------------------------------------------------------------


def test_verify_cli_reports_a_refusal_with_exit_1(receipts_dir: Path, contracts_dir: Path) -> None:
    from bernstein.cli.commands.adapters_verify_cmd import _execute_verify  # pyright: ignore[reportPrivateUsage]

    rc = _execute_verify(
        "kimi",
        output_format="json",
        seal=False,
        receipts_dir=receipts_dir,
        contracts_dir=contracts_dir,
        golden_dir=GOLDEN_DIR,
        now=_NOW,
    )

    assert rc == 1


def test_verify_cli_seals_a_receipt_the_gate_then_accepts(receipts_dir: Path, contracts_dir: Path) -> None:
    from bernstein.cli.commands.adapters_verify_cmd import _execute_verify  # pyright: ignore[reportPrivateUsage]

    # The host has no kimi binary, so the sealed receipt records a refusal -
    # the negative path, written down rather than left silent.
    rc = _execute_verify(
        "kimi",
        output_format="text",
        seal=True,
        receipts_dir=receipts_dir,
        contracts_dir=contracts_dir,
        golden_dir=GOLDEN_DIR,
        now=_NOW,
    )

    assert rc == 1
    stored, problem = load_admission_receipt(receipts_dir, "kimi")
    assert problem == ""
    assert stored is not None
    assert stored["verdict"] == VERDICT_REFUSE


def test_verify_cli_rejects_an_unknown_adapter(receipts_dir: Path) -> None:
    from bernstein.cli.commands.adapters_verify_cmd import _execute_verify  # pyright: ignore[reportPrivateUsage]

    assert _execute_verify("nope", output_format="text", seal=False, receipts_dir=receipts_dir) == 2


def test_verify_cli_short_circuits_exempt_adapters(receipts_dir: Path) -> None:
    from bernstein.cli.commands.adapters_verify_cmd import _execute_verify  # pyright: ignore[reportPrivateUsage]

    assert _execute_verify("mock", output_format="text", seal=False, receipts_dir=receipts_dir) == 0


def test_verify_cli_is_registered_on_the_adapters_group() -> None:
    from bernstein.cli.commands.adapter_cmd import adapters_group

    assert "verify" in adapters_group.commands


# ---------------------------------------------------------------------------
# Canary matrix coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", ["droid", "kimi", "pydantic_ai"])
def test_canary_matrix_covers_the_contracted_adapters(adapter: str) -> None:
    """A broken upstream release is caught nightly, not mid-run."""
    from bernstein.adapters.canary import CANARY_MATRIX

    assert any(target.adapter == adapter for target in CANARY_MATRIX)


def test_every_canary_target_has_a_contract() -> None:
    from bernstein.adapters.canary import CANARY_MATRIX

    contracts = Path(__file__).parents[1] / "contract" / "contracts"
    missing = [t.adapter for t in CANARY_MATRIX if not (contracts / f"{t.adapter}.yaml").exists()]

    assert missing == []


@pytest.fixture(autouse=True)
def _no_ambient_policy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep an operator's own env out of the tests."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER_ADMISSION_POLICY", raising=False)
    monkeypatch.delenv("BERNSTEIN_ADAPTER_GOLDEN_DIR", raising=False)
    yield
