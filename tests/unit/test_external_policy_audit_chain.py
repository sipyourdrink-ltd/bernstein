"""Every external policy evaluation leaves a record in the audit chain (#4912).

`PolicyHookRegistry` resolved OPA and Cedar verdicts and returned them to its
caller. Nothing was written down. An operator whose run stopped because the
policy engine was unreachable had the refusal and no way to show, later and
offline, that it happened, which engine could not answer, or which policy was
being asked.

These tests pin the record: one entry per hook evaluation -- allow, deny,
abstain and unavailable alike -- carrying the engine name, the policy digest,
the request fields, the verdict, and the latency, chained so a verifier holding
only the log can check it.

`UNAVAILABLE` is recorded as itself. Collapsing it to `ABSTAIN` in the log would
rebuild, in the evidence layer, exactly the conflation #4971 removed from the
decision layer.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import cast

import pytest

from bernstein.core.security.agent_card_signer import canonicalize_jcs
from bernstein.core.security.audit_chain import (
    EVENT_EXTERNAL_POLICY_DECISION,
    AuditChainStore,
)
from bernstein.core.security.external_policy_hook import (
    ExternalPolicyHook,
    HookRequest,
    HookResponse,
    HookVerdict,
    OPAHook,
    PolicyHookRegistry,
)

_KEY = b"0" * 32

_ALLOW_REGO = 'package bernstein.authz\n\ndefault allow = false\n\nallow {\n  input.action == "read"\n}\n'


def _req() -> HookRequest:
    return HookRequest(
        action="deploy",
        resource="prod/cluster-a",
        agent_id="agent-1",
        role="backend",
        scope="release",
    )


def _events(chain: AuditChainStore) -> list[dict[str, object]]:
    return [dict(ev.details) for ev in chain.query(event_type=EVENT_EXTERNAL_POLICY_DECISION)]


def _fake_opa(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable stand-in for the ``opa`` binary and return its path."""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


class _Allows(ExternalPolicyHook):
    @property
    def name(self) -> str:
        return "permissive"

    def evaluate(self, request: HookRequest) -> HookResponse:
        return HookResponse(hook_name=self.name, verdict=HookVerdict.ALLOW, reason="ok")


class _Abstains(ExternalPolicyHook):
    @property
    def name(self) -> str:
        return "quiet"

    def evaluate(self, request: HookRequest) -> HookResponse:
        return HookResponse(hook_name=self.name, verdict=HookVerdict.ABSTAIN, reason="no rule")


def test_unavailable_engine_is_recorded_as_unavailable_not_abstain(tmp_path: Path) -> None:
    """1. The load-bearing one: the refusal receipt names unavailability.

    A record that said ``abstain`` here would be indistinguishable from a policy
    that simply had no rule, which is the defect this issue is about.
    """
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(OPAHook(policy_path=tmp_path / "absent.rego", opa_binary="/nonexistent/opa"))

    decisive = registry.first_decisive(_req())
    assert decisive.verdict == HookVerdict.DENY

    records = _events(chain)
    assert len(records) == 1
    assert records[0]["verdict"] == HookVerdict.UNAVAILABLE.value
    assert records[0]["verdict"] != HookVerdict.ABSTAIN.value
    assert records[0]["engine"] == "opa"


def test_nonzero_exit_records_the_stderr_digest(tmp_path: Path) -> None:
    """2. A failing engine's own words are pinned by hash, not copied into the log."""
    policy = tmp_path / "p.rego"
    policy.write_text(_ALLOW_REGO, encoding="utf-8")
    opa = _fake_opa(tmp_path, "opa-broken", 'echo "rego_parse_error: unexpected token" >&2\nexit 1')

    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(OPAHook(policy_path=policy, opa_binary=opa))

    decisive = registry.first_decisive(_req())
    assert decisive.verdict == HookVerdict.DENY

    (record,) = _events(chain)
    assert record["verdict"] == HookVerdict.UNAVAILABLE.value
    expected = hashlib.sha256(b"rego_parse_error: unexpected token").hexdigest()
    assert record["error_digest"] == expected


def test_deny_record_carries_the_policy_digest_of_the_file_on_disk(tmp_path: Path) -> None:
    """3. The record names which policy denied, checkable against the file."""
    policy = tmp_path / "p.rego"
    policy.write_text(_ALLOW_REGO, encoding="utf-8")
    opa = _fake_opa(tmp_path, "opa-deny", 'echo \'{"result":[{"expressions":[{"value":false}]}]}\'')

    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(OPAHook(policy_path=policy, opa_binary=opa))

    decisive = registry.first_decisive(_req())
    assert decisive.verdict == HookVerdict.DENY

    (record,) = _events(chain)
    assert record["verdict"] == HookVerdict.DENY.value
    assert record["policy_digest"] == hashlib.sha256(policy.read_bytes()).hexdigest()


def test_an_abstaining_engine_still_leaves_proof_it_was_consulted(tmp_path: Path) -> None:
    """4. "Consulted, no rule matched" must be distinguishable from "never consulted"."""
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(_Abstains())

    registry.first_decisive(_req())

    (record,) = _events(chain)
    assert record["engine"] == "quiet"
    assert record["verdict"] == HookVerdict.ABSTAIN.value


def test_every_hook_that_ran_gets_its_own_record(tmp_path: Path) -> None:
    """5. The record set equals the evaluation set, in evaluation order."""
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(_Abstains())
    registry.register(OPAHook(policy_path=tmp_path / "absent.rego", opa_binary="/nonexistent/opa"))

    registry.first_decisive(_req())

    records = _events(chain)
    assert [r["engine"] for r in records] == ["quiet", "opa"]


def test_decision_records_chain_and_the_chain_verifies(tmp_path: Path) -> None:
    """6. The refusal is provable offline: chained, at a named position."""
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(OPAHook(policy_path=tmp_path / "absent.rego", opa_binary="/nonexistent/opa"))

    registry.first_decisive(_req())

    (record,) = _events(chain)
    assert record["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors


def test_request_fields_are_recorded_and_the_request_digest_recomputes(tmp_path: Path) -> None:
    """7. An operator can tell what was asked, and re-derive the digest from it."""
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(_Abstains())

    request = _req()
    registry.first_decisive(request)

    (record,) = _events(chain)
    assert record["action"] == "deploy"
    assert record["resource"] == "prod/cluster-a"
    assert record["agent_id"] == "agent-1"
    assert record["role"] == "backend"
    assert record["scope"] == "release"
    assert isinstance(record["latency_ms"], float)

    expected = hashlib.sha256(
        canonicalize_jcs(
            {
                "action": "deploy",
                "resource": "prod/cluster-a",
                "agent_id": "agent-1",
                "role": "backend",
                "scope": "release",
                "metadata": {},
            },
        ),
    ).hexdigest()
    assert record["request_digest"] == expected


def test_request_metadata_never_reaches_the_record_verbatim(tmp_path: Path) -> None:
    """8. Caller-supplied context is bound by hash, not copied into the chain."""
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    registry = PolicyHookRegistry(audit_chain=chain)
    registry.register(_Abstains())

    registry.first_decisive(
        HookRequest(action="deploy", resource="prod", metadata={"ticket": "SECRET-123"}),
    )

    (record,) = _events(chain)
    assert "SECRET-123" not in json.dumps(record)
    assert record["request_digest"]


def test_registry_without_a_chain_appends_nothing(tmp_path: Path) -> None:
    """9. Recording is opt-in; an unwired registry still decides."""
    registry = PolicyHookRegistry()
    registry.register(OPAHook(policy_path=tmp_path / "absent.rego", opa_binary="/nonexistent/opa"))

    assert registry.first_decisive(_req()).verdict == HookVerdict.DENY
    assert not list(tmp_path.rglob("*.jsonl"))


class _BrokenChain:
    """A chain store whose append always fails."""

    def log_with_prev_digest(self, **kwargs: object) -> object:
        raise OSError("audit chain is read-only")


def test_a_chain_that_cannot_be_written_stops_the_decision(tmp_path: Path) -> None:
    """10. Evidence failure is decision failure, never a silent grant.

    A registry configured to record and unable to record must not keep answering
    as if it were: the caller gets an error and grants nothing, rather than an
    allow whose justification was never written down.
    """
    registry = PolicyHookRegistry(audit_chain=cast(AuditChainStore, _BrokenChain()))
    registry.register(_Allows())

    with pytest.raises(OSError, match="read-only"):
        registry.first_decisive(_req())
