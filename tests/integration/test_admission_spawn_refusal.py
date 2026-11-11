"""End-to-end spawn-gate tests for the executor admission policy (#4907).

These drive the real :meth:`AgentSpawner.spawn_for_tasks` path rather
than :meth:`AdmissionPolicy.evaluate`, to prove the policy is wired in
ahead of process start: a refused spawn starts no agent, reaches a
``SpawnError``, leaves a decision record naming the deciding rule, and
appends an ``admission_refusal`` event to the HMAC-chained audit log.

The policy is declared in the workdir's own ``bernstein.yaml``, which is
where an operator declares it, so the tests also cover the file-loading
seam.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnError, SpawnResult
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.security.agent_card_signer import canonicalize_jcs
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_chain import EVENT_EXTERNAL_POLICY_DECISION, AuditChainStore
from bernstein.core.security.authzen import Obligation
from bernstein.core.security.external_policy_hook import (
    ExternalPolicyHook,
    HookRequest,
    HookResponse,
    HookVerdict,
    OPAHook,
    PolicyHookRegistry,
)

_ADMISSION_RECORD_REL = Path(".sdd") / "runtime" / "spawn_admission"


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """Per-test workdir: a git repo plus the minimal role template tree."""
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "ci@example.com"],
        ["git", "config", "user.name", "ci"],
    ):
        subprocess.run(args, cwd=str(tmp_path), check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# admission integration test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(tmp_path), check=True, capture_output=True)

    role_dir = tmp_path / "templates" / "roles" / "backend"
    role_dir.mkdir(parents=True)
    (role_dir / "system_prompt.md").write_text("You are a backend specialist.", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def mock_adapter() -> MagicMock:
    """Mock :class:`CLIAdapter` that reports a successful spawn."""
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=4242, log_path=Path("/tmp/admission-test.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "mockcli"
    return adapter


@pytest.fixture()
def make_task() -> Iterator[Any]:  # type: ignore[misc]
    """Yield a factory that builds a minimal :class:`Task`."""
    from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

    def _factory(role: str = "backend") -> Task:
        return Task(
            id="T-001",
            title="Admission integration task",
            description="Drive the spawner through the admission gate.",
            role=role,
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            priority=2,
        )

    yield _factory


def _write_admission(workdir: Path, block: str) -> None:
    """Write a ``bernstein.yaml`` carrying the given ``admission:`` block."""
    (workdir / "bernstein.yaml").write_text(f"goal: integration test\n{block}", encoding="utf-8")


def _build_spawner(workdir: Path, adapter: MagicMock) -> AgentSpawner:
    """Construct an :class:`AgentSpawner` plumbed like production."""
    return AgentSpawner(
        adapter=adapter,
        templates_dir=workdir / "templates" / "roles",
        workdir=workdir,
        use_worktrees=False,
        default_model="mock-model",
    )


def _record(workdir: Path, session_id: str) -> dict[str, Any]:
    """Read back the persisted admission decision record for a session."""
    path = workdir / _ADMISSION_RECORD_REL / f"{session_id}.json"
    assert path.exists(), f"Admission decision record missing at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


class TestRefusal:
    """A subject no allow rule admits must never reach the adapter."""

    def test_denied_spawn_starts_no_agent_process(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n  rules:\n    - id: approved-adapters\n      effect: allow\n      adapters: [codex]\n",
        )
        calls: list[Any] = []
        real_popen = subprocess.Popen
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *a, **k: (calls.append(a), real_popen(*a, **k))[1],  # type: ignore[misc]
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError, match="admission"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()
        assert calls == [], "A refused spawn must not start any subprocess."

    def test_denied_spawn_records_the_rule_that_refused_it(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n"
            "  rules:\n"
            "    - id: no-unsandboxed\n"
            "      effect: deny\n"
            "      sandboxes: [none]\n"
            "    - id: approved-adapters\n"
            "      effect: allow\n"
            "      adapters: [mockcli]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks([make_task()])

        records = sorted((workdir / _ADMISSION_RECORD_REL).glob("*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding="utf-8"))
        assert record["allowed"] is False
        assert record["rule_id"] == "no-unsandboxed"
        assert record["effect"] == "deny"
        assert record["subject"]["adapter"] == "mockcli"
        assert record["subject"]["role"] == "backend"

    def test_denied_spawn_emits_admission_refusal_audit_event(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n  rules:\n    - id: approved-adapters\n      effect: allow\n      adapters: [codex]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks([make_task()])

        audit = AuditLog(audit_dir=workdir / ".sdd" / "audit")
        ok, errors = audit.verify()
        assert ok, f"Audit chain integrity broke: {errors}"
        events = list(audit.query(event_type="admission_refusal"))
        assert events, "A refused spawn must leave a verifiable audit-chain event."
        first = events[0]
        assert first.actor == "spawner"
        assert first.details.get("rule_id") == ""
        assert first.details.get("subject", {}).get("adapter") == "mockcli"

    def test_malformed_policy_refuses_rather_than_admitting(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n  rules:\n    - id: broken\n      effect: permit\n      adapters: [mockcli]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError, match="admission"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()


class TestAdmission:
    """An admitted spawn proceeds and records the rule that admitted it."""

    def test_admitted_spawn_records_the_rule_id_that_admitted_it(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n"
            "  rules:\n"
            "    - id: approved-adapters\n"
            "      effect: allow\n"
            "      adapters: [mockcli]\n"
            "      models: ['mock-*']\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        session = spawner.spawn_for_tasks([make_task()])

        assert session.pid == 4242
        record = _record(workdir, session.id)
        assert record["allowed"] is True
        assert record["rule_id"] == "approved-adapters"
        assert record["subject"]["model"] == "mock-model"

    def test_warn_mode_admits_and_records_the_refusing_rule(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n"
            "  mode: warn\n"
            "  rules:\n"
            "    - id: approved-adapters\n"
            "      effect: allow\n"
            "      adapters: [codex]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        session = spawner.spawn_for_tasks([make_task()])

        record = _record(workdir, session.id)
        assert record["allowed"] is True
        assert record["rule_id"] == ""
        assert record["mode"] == "warn"


class TestNoPolicyDeclared:
    """A repository that declares no policy is unaffected."""

    def test_absent_admission_block_leaves_the_spawn_unchanged(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        (workdir / "bernstein.yaml").write_text("goal: integration test\n", encoding="utf-8")
        spawner = _build_spawner(workdir, mock_adapter)

        session = spawner.spawn_for_tasks([make_task()])

        assert session.pid == 4242
        assert not (workdir / _ADMISSION_RECORD_REL).exists()


class _AdmissionHook(ExternalPolicyHook):
    def __init__(self, verdict: HookVerdict, *, obligations: tuple[Obligation, ...] = ()) -> None:
        self.verdict = verdict
        self.obligations = obligations
        self.requests: list[HookRequest] = []

    @property
    def name(self) -> str:
        return "spawn-test-policy"

    def evaluate(self, request: HookRequest) -> HookResponse:
        self.requests.append(request)
        return HookResponse(
            hook_name=self.name,
            verdict=self.verdict,
            reason="test policy",
            policy_digest="test-policy-digest",
            obligations=self.obligations,
        )


def _external_spawner(workdir: Path, adapter: MagicMock, registry: PolicyHookRegistry) -> AgentSpawner:
    # Behavioral RED: baseline construction succeeds but ignores this future field.
    # Public constructor integration is tested separately below.
    spawner = _build_spawner(workdir, adapter)
    vars(spawner)["_policy_hook_registry"] = registry
    return spawner


def _external_records(workdir: Path) -> list[Any]:
    audit = AuditLog(audit_dir=workdir / ".sdd" / "audit")
    ok, errors = audit.verify()
    assert ok, errors
    return list(audit.query(event_type=EVENT_EXTERNAL_POLICY_DECISION))


class TestExternalAdmission:
    @pytest.mark.parametrize("native_allow", [False, True])
    def test_deny_blocks_real_spawn_with_or_without_native_policy(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any, native_allow: bool
    ) -> None:
        if native_allow:
            _write_admission(
                workdir, "admission:\n  rules:\n    - id: allow\n      effect: allow\n      adapters: [mockcli]\n"
            )
        registry = PolicyHookRegistry()
        registry.register(_AdmissionHook(HookVerdict.DENY))
        spawner = _external_spawner(workdir, mock_adapter, registry)

        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()
        (event,) = _external_records(workdir)
        assert event.details["verdict"] == "deny"

    @pytest.mark.parametrize("fail_open", [False, True])
    def test_missing_engine_refuses_and_audits_unavailable_even_with_fail_open(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any, fail_open: bool
    ) -> None:
        registry = PolicyHookRegistry(default_verdict=HookVerdict.ALLOW, fail_open=fail_open)
        registry.register(OPAHook(policy_path=workdir / "missing.rego", opa_binary="/nonexistent/opa"))
        spawner = _external_spawner(workdir, mock_adapter, registry)

        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()
        (event,) = _external_records(workdir)
        assert event.details["verdict"] == "unavailable"

    def test_allow_reaches_action_and_audits_without_mutating_callers_registry(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any
    ) -> None:
        registry = PolicyHookRegistry()
        hook = _AdmissionHook(HookVerdict.ALLOW)
        registry.register(hook)
        spawner = _external_spawner(workdir, mock_adapter, registry)

        session = spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_called_once()
        (event,) = _external_records(workdir)
        assert event.details["verdict"] == "allow"
        assert event.details["engine"] == hook.name
        assert event.details["policy_digest"] == "test-policy-digest"
        (request,) = hook.requests
        assert request.action == "spawn"
        assert request.agent_id == session.id
        assert request.role == "backend"
        assert request.metadata["adapter"] == "mockcli"
        assert request.metadata["model"] == "mock-model"
        assert set(request.metadata) == {"role", "adapter", "model", "endpoint", "sandbox", "task_type"}
        expected_digest = hashlib.sha256(
            canonicalize_jcs(
                {
                    "action": request.action,
                    "resource": request.resource,
                    "agent_id": request.agent_id,
                    "role": request.role,
                    "scope": request.scope,
                    "metadata": request.metadata,
                }
            )
        ).hexdigest()
        assert event.details["request_digest"] == expected_digest
        registry.evaluate(HookRequest(action="inspect", resource="caller"))
        assert len(_external_records(workdir)) == 1

    def test_native_denial_is_authoritative_over_external_allow(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any
    ) -> None:
        _write_admission(workdir, "admission:\n  rules: []\n")
        registry = PolicyHookRegistry()
        hook = _AdmissionHook(HookVerdict.ALLOW)
        registry.register(hook)
        spawner = _external_spawner(workdir, mock_adapter, registry)

        with pytest.raises(SpawnError, match="admission refused"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()
        assert hook.requests == []
        assert _external_records(workdir) == []

    def test_audit_failure_blocks_real_spawn(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = PolicyHookRegistry()
        registry.register(_AdmissionHook(HookVerdict.ALLOW))
        spawner = _external_spawner(workdir, mock_adapter, registry)

        def broken_append(*args: Any, **kwargs: Any) -> None:
            raise OSError("synthetic audit append failure")

        monkeypatch.setattr(AuditChainStore, "log_with_prev_digest", broken_append)
        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_not_called()

    @pytest.mark.parametrize("register_abstain", [False, True])
    def test_no_explicit_allow_refuses(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any, register_abstain: bool
    ) -> None:
        registry = PolicyHookRegistry(default_verdict=HookVerdict.ALLOW)
        if register_abstain:
            registry.register(_AdmissionHook(HookVerdict.ABSTAIN))
        spawner = _external_spawner(workdir, mock_adapter, registry)
        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_not_called()
        events = _external_records(workdir)
        assert len(events) == int(register_abstain)

    def test_unsupported_allow_obligation_refuses(self, workdir: Path, mock_adapter: MagicMock, make_task: Any) -> None:
        registry = PolicyHookRegistry()
        registry.register(_AdmissionHook(HookVerdict.ALLOW, obligations=(Obligation(id="owner-approval"),)))
        spawner = _external_spawner(workdir, mock_adapter, registry)
        with pytest.raises(SpawnError, match="external admission.*obligation"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_not_called()
        assert _external_records(workdir)[0].details["verdict"] == "allow"

    def test_public_constructor_wires_registry_to_real_spawn(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any
    ) -> None:
        registry = PolicyHookRegistry()
        registry.register(_AdmissionHook(HookVerdict.DENY))
        spawner = AgentSpawner(
            adapter=mock_adapter,
            templates_dir=workdir / "templates" / "roles",
            workdir=workdir,
            use_worktrees=False,
            default_model="mock-model",
            policy_hook_registry=registry,
        )
        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_not_called()

    @pytest.mark.parametrize("later_verdict", [HookVerdict.DENY, HookVerdict.UNAVAILABLE])
    def test_earlier_allow_cannot_hide_later_refusal(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any, later_verdict: HookVerdict
    ) -> None:
        registry = PolicyHookRegistry(fail_open=True)
        registry.register(_AdmissionHook(HookVerdict.ALLOW))
        registry.register(_AdmissionHook(later_verdict))
        spawner = _external_spawner(workdir, mock_adapter, registry)
        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_not_called()
        assert [event.details["verdict"] for event in _external_records(workdir)] == ["allow", later_verdict.value]

    def test_hook_exception_refuses_and_records_unavailable(
        self, workdir: Path, mock_adapter: MagicMock, make_task: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = PolicyHookRegistry(fail_open=True)
        hook = _AdmissionHook(HookVerdict.ALLOW)
        registry.register(hook)

        def broken_evaluate(request: HookRequest) -> HookResponse:
            raise RuntimeError("synthetic engine failure")

        monkeypatch.setattr(hook, "evaluate", broken_evaluate)
        spawner = _external_spawner(workdir, mock_adapter, registry)
        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_not_called()
        (event,) = _external_records(workdir)
        assert event.details["verdict"] == "unavailable"
        assert event.details["error_digest"] == hashlib.sha256(b"synthetic engine failure").hexdigest()


class _ResolvedAdmissionHook(_AdmissionHook):
    def evaluate(self, request: HookRequest) -> HookResponse:
        response = super().evaluate(request)
        if request.metadata["adapter"] == "mockcli":
            return HookResponse(hook_name=self.name, verdict=HookVerdict.ALLOW, reason="primary allowed")
        return response


def _routed_external_spawner(workdir, primary, monkeypatch, *, initial="primary", verdict=HookVerdict.DENY):
    from bernstein.core.models import ModelConfig

    backup = MagicMock(spec=CLIAdapter)
    backup.name.return_value = "backupcli"
    backup.spawn.return_value = SpawnResult(pid=4243, log_path=Path("/tmp/admission-backup.log"))
    hook = _ResolvedAdmissionHook(verdict)
    registry = PolicyHookRegistry()
    registry.register(hook)
    spawner = _external_spawner(workdir, primary, registry)
    router = SimpleNamespace(
        state=SimpleNamespace(
            providers={
                name: SimpleNamespace(available=True, health=SimpleNamespace(status=None))
                for name in ("primary", "backup")
            }
        ),
        select_provider_for_task=MagicMock(
            return_value=SimpleNamespace(provider="backup", model_config=ModelConfig(model="fallback", effort="medium"))
        ),
        update_provider_health=MagicMock(),
    )
    spawner._router = router
    monkeypatch.setattr(
        spawner,
        "_resolve_routing",
        lambda *args: (
            ModelConfig(model="approved" if initial == "primary" else "fallback", effort="medium"),
            initial,
            "router",
        ),
    )
    monkeypatch.setattr(
        spawner,
        "_infer_adapter_name_for_provider",
        lambda provider, model: "mockcli" if provider == "primary" else "backupcli",
    )
    monkeypatch.setattr(
        spawner, "_get_adapter_by_name", lambda name, **kwargs: primary if name == "mockcli" else backup
    )
    monkeypatch.setattr(
        spawner,
        "_mcp_config_for_adapter",
        lambda adapter, config: {"base_url": f"https://{adapter.name()}.example/api"},
    )
    return spawner, backup, hook, router


class TestResolvedExternalAdmission:
    def test_selected_adapter_is_evaluated_before_its_action(self, workdir, mock_adapter, make_task, monkeypatch):
        spawner, backup, hook, _router = _routed_external_spawner(workdir, mock_adapter, monkeypatch, initial="backup")
        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_not_called()
        backup.spawn.assert_not_called()
        assert [request.metadata["adapter"] for request in hook.requests] == ["backupcli"]
        assert _external_records(workdir)[0].details["verdict"] == "deny"

    @pytest.mark.parametrize("refusal", ["deny", "unavailable", "obligation", "audit"])
    def test_fallback_refusal_is_not_retried(self, workdir, mock_adapter, make_task, monkeypatch, refusal):
        from bernstein.adapters.base import RateLimitError

        verdict = (
            HookVerdict.UNAVAILABLE
            if refusal == "unavailable"
            else HookVerdict.ALLOW
            if refusal in ("obligation", "audit")
            else HookVerdict.DENY
        )
        spawner, backup, hook, router = _routed_external_spawner(workdir, mock_adapter, monkeypatch, verdict=verdict)
        if refusal == "obligation":
            hook.obligations = (Obligation(id="cannot-enforce"),)
        mock_adapter.spawn.side_effect = RateLimitError("synthetic local throttle")
        if refusal == "audit":
            original = AuditChainStore.log_with_prev_digest

            def fail_backup_audit(chain, *args, **kwargs):
                details = kwargs.get("details", {})
                if details.get("request_digest") and len(hook.requests) == 2:
                    raise OSError("synthetic fallback audit failure")
                return original(chain, *args, **kwargs)

            monkeypatch.setattr(AuditChainStore, "log_with_prev_digest", fail_backup_audit)
        with pytest.raises(SpawnError, match="external admission"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_called_once()
        backup.spawn.assert_not_called()
        router.select_provider_for_task.assert_called_once()
        assert [request.metadata["model"] for request in hook.requests] == ["approved", "fallback"]
        records = _external_records(workdir)
        assert len(records) == (1 if refusal == "audit" else 2)
        if refusal != "audit":
            assert records[-1].details["verdict"] == verdict.value

    def test_allowed_fallback_binds_the_actual_subject(self, workdir, mock_adapter, make_task, monkeypatch):
        from bernstein.adapters.base import RateLimitError

        spawner, backup, hook, _router = _routed_external_spawner(
            workdir, mock_adapter, monkeypatch, verdict=HookVerdict.ALLOW
        )
        mock_adapter.spawn.side_effect = RateLimitError("synthetic local throttle")
        session = spawner.spawn_for_tasks([make_task()])
        (request_a, request_b) = hook.requests
        assert request_a.metadata["adapter"] == "mockcli"
        assert request_a.metadata["model"] == "approved"
        assert request_a.agent_id == request_b.agent_id
        assert request_b.agent_id == session.id
        assert request_b.metadata == {
            "role": "backend",
            "adapter": "backupcli",
            "model": "fallback",
            "endpoint": "https://backupcli.example/api",
            "sandbox": "none",
            "task_type": "standard",
        }
        kwargs = backup.spawn.call_args.kwargs
        assert kwargs["model_config"].model == request_b.metadata["model"]
        assert kwargs["mcp_config"]["base_url"] == request_b.metadata["endpoint"]
        records = _external_records(workdir)
        expected = hashlib.sha256(
            canonicalize_jcs(
                {
                    "action": request_b.action,
                    "resource": request_b.resource,
                    "agent_id": request_b.agent_id,
                    "role": request_b.role,
                    "scope": request_b.scope,
                    "metadata": request_b.metadata,
                }
            )
        ).hexdigest()
        assert records[-1].details["request_digest"] == expected
        assert records[0].details["request_digest"] != expected

    def test_selected_target_can_pass_native_and_external_policy(self, workdir, mock_adapter, make_task, monkeypatch):
        _write_admission(
            workdir, "admission:\n  rules:\n    - id: selected-only\n      effect: allow\n      adapters: [backupcli]\n"
        )
        spawner, backup, hook, _router = _routed_external_spawner(
            workdir, mock_adapter, monkeypatch, initial="backup", verdict=HookVerdict.ALLOW
        )
        spawner.spawn_for_tasks([make_task()])
        backup.spawn.assert_called_once()
        assert [request.metadata["adapter"] for request in hook.requests] == ["backupcli"]

    def test_selected_target_native_deny_precedes_external_allow(self, workdir, mock_adapter, make_task, monkeypatch):
        _write_admission(
            workdir, "admission:\n  rules:\n    - id: primary-only\n      effect: allow\n      adapters: [mockcli]\n"
        )
        spawner, backup, hook, _router = _routed_external_spawner(
            workdir, mock_adapter, monkeypatch, initial="backup", verdict=HookVerdict.ALLOW
        )
        with pytest.raises(SpawnError, match="admission refused"):
            spawner.spawn_for_tasks([make_task()])
        backup.spawn.assert_not_called()
        mock_adapter.spawn.assert_not_called()
        assert hook.requests == []
        assert _external_records(workdir) == []

    def test_fallback_native_deny_precedes_external_allow(self, workdir, mock_adapter, make_task, monkeypatch):
        from bernstein.adapters.base import RateLimitError

        _write_admission(
            workdir, "admission:\n  rules:\n    - id: primary-only\n      effect: allow\n      adapters: [mockcli]\n"
        )
        spawner, backup, hook, router = _routed_external_spawner(
            workdir, mock_adapter, monkeypatch, verdict=HookVerdict.ALLOW
        )
        mock_adapter.spawn.side_effect = RateLimitError("synthetic local throttle")
        with pytest.raises(SpawnError, match="admission refused"):
            spawner.spawn_for_tasks([make_task()])
        mock_adapter.spawn.assert_called_once()
        backup.spawn.assert_not_called()
        router.select_provider_for_task.assert_called_once()
        assert [request.metadata["adapter"] for request in hook.requests] == ["mockcli"]
        assert len(_external_records(workdir)) == 1

    @pytest.mark.parametrize("transport", ["bridge", "in_process", "container", "sandbox", "sandbox_session"])
    def test_unbound_transport_refuses_without_action(self, workdir, mock_adapter, make_task, monkeypatch, transport):
        from bernstein.core.agents.spawner_core import AgentBackend

        spawner, backup, hook, _router = _routed_external_spawner(
            workdir, mock_adapter, monkeypatch, verdict=HookVerdict.ALLOW
        )
        action = MagicMock()
        if transport == "bridge":
            spawner._runtime_bridge = SimpleNamespace(name=lambda: "test-bridge", config=SimpleNamespace(extra={}))
            monkeypatch.setattr(spawner, "_spawn_via_runtime_bridge", action)
        elif transport == "in_process":
            spawner._backend = AgentBackend.IN_PROCESS
            spawner._in_process = SimpleNamespace(run=action)
        elif transport == "container":
            spawner._container_mgr = SimpleNamespace()
            monkeypatch.setattr(spawner, "_spawn_in_container", action)
        elif transport == "sandbox":
            spawner._sandbox = SimpleNamespace(runtime="docker")
            monkeypatch.setattr(spawner, "_spawn_in_sandbox", action)
        else:
            monkeypatch.setattr(spawner, "_sandbox_session_routing_active", lambda: True)
            monkeypatch.setattr(spawner, "_spawn_via_sandbox_session", action)
        with pytest.raises(SpawnError, match="external admission cannot bind"):
            spawner.spawn_for_tasks([make_task()])
        action.assert_not_called()
        mock_adapter.spawn.assert_not_called()
        backup.spawn.assert_not_called()
        assert hook.requests == []

    def test_unbound_resume_refuses_without_action(self, workdir, mock_adapter, make_task, monkeypatch):
        spawner, backup, hook, _router = _routed_external_spawner(
            workdir, mock_adapter, monkeypatch, verdict=HookVerdict.ALLOW
        )
        with pytest.raises(SpawnError, match="external admission cannot bind.*resume"):
            spawner.spawn_for_resume([make_task()], worktree_path=workdir, changed_files=[])
        mock_adapter.spawn.assert_not_called()
        backup.spawn.assert_not_called()
        assert hook.requests == []
