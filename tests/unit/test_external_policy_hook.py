"""Tests for SEC-011: Permission hooks for external policy engines."""

from __future__ import annotations

from bernstein.core.external_policy_hook import (
    CedarHook,
    ExternalPolicyHook,
    HookRequest,
    HookResponse,
    HookVerdict,
    OPAHook,
    PolicyHookRegistry,
)


def _req(action: str = "bash", resource: str = "echo hello") -> HookRequest:
    return HookRequest(action=action, resource=resource, agent_id="agent-1")


class TestCedarHook:
    def test_allow_matching_action(self) -> None:
        cedar = CedarHook('permit (action == "read");')
        resp = cedar.evaluate(_req(action="read"))
        assert resp.verdict == HookVerdict.ALLOW
        assert resp.hook_name == "cedar"

    def test_deny_matching_action(self) -> None:
        cedar = CedarHook('forbid (action == "delete");')
        resp = cedar.evaluate(_req(action="delete"))
        assert resp.verdict == HookVerdict.DENY

    def test_abstain_unknown_action(self) -> None:
        cedar = CedarHook('permit (action == "read");')
        resp = cedar.evaluate(_req(action="deploy"))
        assert resp.verdict == HookVerdict.ABSTAIN

    def test_multiple_rules(self) -> None:
        policy = """
        permit (action == "read");
        permit (action == "list");
        forbid (action == "delete");
        """
        cedar = CedarHook(policy)
        assert cedar.evaluate(_req(action="read")).verdict == HookVerdict.ALLOW
        assert cedar.evaluate(_req(action="list")).verdict == HookVerdict.ALLOW
        assert cedar.evaluate(_req(action="delete")).verdict == HookVerdict.DENY

    def test_deny_takes_priority(self) -> None:
        policy = """
        permit (action == "delete");
        forbid (action == "delete");
        """
        cedar = CedarHook(policy)
        resp = cedar.evaluate(_req(action="delete"))
        # deny should override allow
        assert resp.verdict == HookVerdict.DENY

    def test_latency_recorded(self) -> None:
        cedar = CedarHook('permit (action == "read");')
        resp = cedar.evaluate(_req(action="read"))
        assert resp.latency_ms >= 0


class TestOPAHook:
    def test_opa_binary_not_found_abstains(self) -> None:
        opa = OPAHook(policy_path="/nonexistent/policy.rego", opa_binary="/nonexistent/opa")
        resp = opa.evaluate(_req())
        assert resp.verdict == HookVerdict.ABSTAIN
        assert resp.error

    def test_hook_name(self) -> None:
        opa = OPAHook(policy_path="/tmp/policy.rego")
        assert opa.name == "opa"


class TestPolicyHookRegistry:
    def test_empty_registry_abstains(self) -> None:
        registry = PolicyHookRegistry()
        resp = registry.first_decisive(_req())
        assert resp.verdict == HookVerdict.ABSTAIN
        assert "abstained" in resp.reason.lower()

    def test_first_decisive_returns_first_non_abstain(self) -> None:
        registry = PolicyHookRegistry()
        registry.register(CedarHook('permit (action == "read");'))
        resp = registry.first_decisive(_req(action="read"))
        assert resp.verdict == HookVerdict.ALLOW

    def test_evaluate_returns_all_responses(self) -> None:
        registry = PolicyHookRegistry()
        registry.register(CedarHook('permit (action == "read");'))
        registry.register(CedarHook('forbid (action == "delete");'))

        responses = registry.evaluate(_req(action="read"))
        assert len(responses) == 2

    def test_hooks_property(self) -> None:
        registry = PolicyHookRegistry()
        hook = CedarHook('permit (action == "read");')
        registry.register(hook)
        assert len(registry.hooks) == 1

    def test_fail_open_on_error(self) -> None:
        class FailingHook(ExternalPolicyHook):
            @property
            def name(self) -> str:
                return "failing"

            def evaluate(self, request: HookRequest) -> HookResponse:
                raise RuntimeError("hook error")

        registry = PolicyHookRegistry(fail_open=True)
        registry.register(FailingHook())
        responses = registry.evaluate(_req())
        assert responses[0].verdict == HookVerdict.ALLOW

    def test_fail_closed_on_error(self) -> None:
        class FailingHook(ExternalPolicyHook):
            @property
            def name(self) -> str:
                return "failing"

            def evaluate(self, request: HookRequest) -> HookResponse:
                raise RuntimeError("hook error")

        registry = PolicyHookRegistry(fail_open=False)
        registry.register(FailingHook())
        responses = registry.evaluate(_req())
        assert responses[0].verdict == HookVerdict.DENY

    def test_first_decisive_skips_abstain(self) -> None:
        registry = PolicyHookRegistry()
        # First hook abstains, second decides
        registry.register(CedarHook('permit (action == "write");'))  # abstains on "read"
        registry.register(CedarHook('permit (action == "read");'))
        resp = registry.first_decisive(_req(action="read"))
        assert resp.verdict == HookVerdict.ALLOW

    def test_custom_default_verdict(self) -> None:
        registry = PolicyHookRegistry(default_verdict=HookVerdict.DENY)
        resp = registry.first_decisive(_req())
        assert resp.verdict == HookVerdict.DENY
