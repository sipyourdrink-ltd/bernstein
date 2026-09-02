"""A grant valid at issue must be re-decided at dispatch (issue #5022).

The issued record's Ed25519 signature stays valid after the grant is revoked or
narrowed, so a boundary that only verifies the signature keeps authorising
calls whose authority has lapsed. These tests pin the re-decision at the
dispatch boundary, the chain position a refusal must name, and the ordering the
refusal takes on the grant chain.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.identity import grants
from bernstein.core.security.grant_precondition import (
    DispatchGrantGate,
    GrantPreconditionIndex,
    GrantRefusedError,
    RedecisionOutcome,
)
from bernstein.core.security.toolcall_interlock import (
    AttestationMode,
    ToolCallAttestationInterlock,
    ToolCallIntent,
    VerifiedDispatchEvidence,
)

RUN_ID = "run-5022"
SCOPE_ID = "scope:run-5022:agent-1"


class _RecordingProvider:
    """Evidence provider that always succeeds, so refusals are unambiguous."""

    def __init__(self) -> None:
        self.intents: list[ToolCallIntent] = []

    async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
        self.intents.append(intent)
        return VerifiedDispatchEvidence(
            attestation_ref=f"attestation:{intent.span_id}",
            dispatch_ref=f"dispatch:{intent.span_id}",
            intent_digest=intent.digest(),
        )


@pytest.fixture
def ledger(tmp_path: Path) -> grants.GrantLedger:
    return grants.GrantLedger(
        root=tmp_path,
        key=b"k" * 32,
        signer=grants.GrantSigner.generate(issuer="manager:test"),
    )


def _intent(tool_name: str) -> ToolCallIntent:
    return ToolCallIntent.from_request(
        scope_id=SCOPE_ID,
        server_name="filesystem",
        method="tools/call",
        tool_name=tool_name,
        request_id=1,
        span_id=f"span-{tool_name}",
        arguments={"path": "/tmp/x"},
    )


def _issue(ledger: grants.GrantLedger, *, ceiling: tuple[str, ...], expiry: int = 0) -> grants.GrantReceipt:
    return ledger.issue_grant(
        run_id=RUN_ID,
        task_id="t-1",
        secret_name="ANTHROPIC_API_KEY",
        audience="api.anthropic.com",
        expiry=expiry,
        capability_ceiling=ceiling,
    )


def _gate(ledger: grants.GrantLedger, grant_id: str) -> DispatchGrantGate:
    return DispatchGrantGate(
        index=GrantPreconditionIndex.for_run(ledger, RUN_ID),
        grant_id=grant_id,
        run_id=RUN_ID,
        task_id="t-1",
        ledger=ledger,
    )


def _interlock(
    gate: DispatchGrantGate,
    *,
    mode: AttestationMode = AttestationMode.ENFORCED,
) -> ToolCallAttestationInterlock:
    return ToolCallAttestationInterlock(
        provider=_RecordingProvider(),
        scope_id=SCOPE_ID,
        mode=mode,
        grant_gate=gate,
    )


def _records(ledger: grants.GrantLedger) -> list[dict[str, Any]]:
    path = ledger.receipt_path(RUN_ID)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_revoked_grant_is_refused_at_dispatch_not_at_issue(ledger: grants.GrantLedger) -> None:
    """The same interlock permits, then refuses, once the chain records revocation."""
    grant = _issue(ledger, ceiling=("read_file",))
    interlock = _interlock(_gate(ledger, grant.grant_id))
    intent = _intent("read_file")

    assert await interlock.before_dispatch(intent) is not None

    ledger.revoke_grant(run_id=RUN_ID, grant_id=grant.grant_id, reason="group membership withdrawn")

    with pytest.raises(GrantRefusedError) as excinfo:
        await interlock.before_dispatch(intent)
    assert excinfo.value.decision.outcome is RedecisionOutcome.REVOKED


@pytest.mark.asyncio
async def test_refusal_names_the_superseding_event(ledger: grants.GrantLedger) -> None:
    """The refusal states which record superseded the grant, and at what position."""
    grant = _issue(ledger, ceiling=("read_file",))
    interlock = _interlock(_gate(ledger, grant.grant_id))
    ledger.revoke_grant(run_id=RUN_ID, grant_id=grant.grant_id, reason="playbook withdrew the group")

    with pytest.raises(GrantRefusedError) as excinfo:
        await interlock.before_dispatch(_intent("read_file"))

    decision = excinfo.value.decision
    assert decision.issued_index == 0
    assert decision.superseded_index == 1
    assert decision.superseding_kind == grants.GRANT_REVOKED
    assert "issued at chain position 0" in decision.reason
    assert "superseded by grant_revoked at chain position 1" in decision.reason


@pytest.mark.asyncio
async def test_narrowed_capability_permits_the_narrower_call_and_refuses_the_wider_one(
    ledger: grants.GrantLedger,
) -> None:
    """Re-issuing the grant with a smaller ceiling narrows it from that position on."""
    grant = _issue(ledger, ceiling=("read_file", "write_file"))
    interlock = _interlock(_gate(ledger, grant.grant_id))
    assert await interlock.before_dispatch(_intent("write_file")) is not None

    ledger.issue_grant(
        run_id=RUN_ID,
        task_id="t-1",
        secret_name="ANTHROPIC_API_KEY",
        audience="api.anthropic.com",
        capability_ceiling=("read_file",),
        grant_id=grant.grant_id,
    )

    assert await interlock.before_dispatch(_intent("read_file")) is not None
    with pytest.raises(GrantRefusedError) as excinfo:
        await interlock.before_dispatch(_intent("write_file"))

    decision = excinfo.value.decision
    assert decision.outcome is RedecisionOutcome.NARROWED
    assert decision.superseded_index == 1
    assert "'write_file'" in decision.reason
    assert "superseded by grant_issued at chain position 1" in decision.reason


@pytest.mark.asyncio
async def test_unchanged_preconditions_do_not_produce_an_event(ledger: grants.GrantLedger) -> None:
    """Re-deciding an unchanged grant appends nothing, however many calls run."""
    grant = _issue(ledger, ceiling=("read_file",))
    interlock = _interlock(_gate(ledger, grant.grant_id))
    before = _records(ledger)

    for _ in range(5):
        assert await interlock.before_dispatch(_intent("read_file")) is not None

    assert _records(ledger) == before


def test_re_decision_reads_only_the_chain() -> None:
    """The re-decision path imports no network, subprocess, or model module."""
    module_root = Path(grants.__file__).parents[3] / "bernstein" / "core" / "security"
    redecision = module_root / "grant_precondition.py"
    interlock = module_root / "toolcall_interlock.py"

    forbidden = {
        "aiohttp",
        "httpx",
        "requests",
        "socket",
        "ssl",
        "subprocess",
        "urllib",
        "urllib3",
        "websockets",
    }
    for path in (redecision, interlock):
        assert _imported_roots(path).isdisjoint(forbidden), path.name
        assert not any(root.startswith("bernstein.core.llm") for root in _imported_modules(path)), path.name

    # The re-decision module itself is held to an allowlist, so a later edit
    # cannot reach a live service through a module that is merely not on the
    # denylist above.
    assert _imported_roots(redecision) == {
        "__future__",
        "bernstein",
        "collections",
        "dataclasses",
        "enum",
        "hmac",
        "json",
        "pathlib",
        "time",
        "typing",
    }
    assert {m for m in _imported_modules(redecision) if m.startswith("bernstein")} == {"bernstein.core.identity.grants"}


def _imported_modules(path: Path) -> set[str]:
    """Return every module name imported anywhere in ``path``, nested included."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def _imported_roots(path: Path) -> set[str]:
    return {name.split(".")[0] for name in _imported_modules(path)}


@pytest.mark.asyncio
async def test_grant_valid_at_issue_and_invalid_at_use_is_ordered_correctly_on_the_chain(
    ledger: grants.GrantLedger,
) -> None:
    """Issue, revocation, and the refusal it caused verify in that chain order."""
    grant = _issue(ledger, ceiling=("read_file",))
    interlock = _interlock(_gate(ledger, grant.grant_id))
    assert await interlock.before_dispatch(_intent("read_file")) is not None
    ledger.revoke_grant(run_id=RUN_ID, grant_id=grant.grant_id, reason="budget envelope exhausted")
    with pytest.raises(GrantRefusedError):
        await interlock.before_dispatch(_intent("read_file"))

    result = grants.verify_grant_chain(root=ledger.root, run_id=RUN_ID, key=ledger.hmac_key)
    assert result.valid, result.errors
    assert [r.kind for r in result.records] == [
        grants.GRANT_ISSUED,
        grants.GRANT_REVOKED,
        grants.GRANT_REFUSED,
    ]
    refusal = result.records[2]
    assert refusal.record_index > result.records[1].record_index
    assert refusal.grant_id == grant.grant_id
    assert "chain position 1" in refusal.reason


@pytest.mark.asyncio
async def test_observed_mode_does_not_downgrade_a_grant_refusal(ledger: grants.GrantLedger) -> None:
    """Observed mode softens missing evidence, never a lapsed authority."""
    grant = _issue(ledger, ceiling=("read_file",))
    interlock = _interlock(_gate(ledger, grant.grant_id), mode=AttestationMode.OBSERVED)
    ledger.revoke_grant(run_id=RUN_ID, grant_id=grant.grant_id, reason="certification lapsed")

    with pytest.raises(GrantRefusedError):
        await interlock.before_dispatch(_intent("read_file"))


@pytest.mark.asyncio
async def test_tampered_grant_record_refuses_every_dispatch(ledger: grants.GrantLedger) -> None:
    """A chain that no longer authenticates refuses instead of falling open."""
    grant = _issue(ledger, ceiling=("read_file",))
    interlock = _interlock(_gate(ledger, grant.grant_id))
    path = ledger.receipt_path(RUN_ID)
    entry = _records(ledger)[0]
    entry["capability_ceiling"] = ["read_file", "write_file"]
    path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(GrantRefusedError) as excinfo:
        await interlock.before_dispatch(_intent("read_file"))
    assert "record 0" in excinfo.value.decision.reason
    assert grant.grant_id == excinfo.value.decision.grant_id


@pytest.mark.asyncio
async def test_expired_grant_is_refused_even_though_its_signature_verifies(
    ledger: grants.GrantLedger,
) -> None:
    """An elapsed expiry withdraws authority the issued signature still covers."""
    grant = _issue(ledger, ceiling=("read_file",), expiry=1_000_000_000)
    gate = _gate(ledger, grant.grant_id)
    assert gate.index.decide(grant.grant_id, "read_file", now=999_999_999.0).permitted

    with pytest.raises(GrantRefusedError) as excinfo:
        await _interlock(gate).before_dispatch(_intent("read_file"))
    assert "expired at 1000000000" in excinfo.value.decision.reason
