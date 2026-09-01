"""Tests for SEC-011: Permission hooks for external policy engines."""

from __future__ import annotations

import pytest
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
        assert resp.policy_digest != ""

    def test_deny_matching_action(self) -> None:
        cedar = CedarHook('forbid (action == "delete");')
        resp = cedar.evaluate(_req(action="delete"))
        assert resp.verdict == HookVerdict.DENY

    def test_abstain_unknown_action(self) -> None:
        cedar = CedarHook('permit (action == "read");')
        resp = cedar.evaluate(_req(action="deploy"))
        assert resp.verdict == HookVerdict.ABSTAIN
        assert resp.policy_digest != ""

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

    def test_multi_line_policy_same_verdict(self) -> None:
        """Multi-line Cedar policy should parse the same as single-line equivalent."""
        single_line = 'permit (action == "read");'
        multi_line = """permit
(action == "read");"""

        cedar_single = CedarHook(single_line)
        cedar_multi = CedarHook(multi_line)

        resp_single = cedar_single.evaluate(_req(action="read"))
        resp_multi = cedar_multi.evaluate(_req(action="read"))

        assert resp_single.verdict == resp_multi.verdict
        assert resp_single.policy_digest != ""
        assert resp_multi.policy_digest != ""

    def test_forbid_unsupported_when_refused(self) -> None:
        """Policy with 'when' construct should be refused at construction."""
        with pytest.raises(ValueError, match="when"):
            CedarHook('permit (action == "bash") when { context.role == "reviewer" };')

    def test_forbid_unsupported_unless_refused(self) -> None:
        """Policy with 'unless' construct should be refused at construction."""
        with pytest.raises(ValueError, match="unless"):
            CedarHook('permit (action == "bash") unless { context.role == "admin" };')

    def test_forbid_unsupported_principal_refused(self) -> None:
        """Policy with '?principal' construct should be refused at construction."""
        with pytest.raises(ValueError, match="\\?principal"):
            CedarHook('permit (action == "bash") as ?principal;')

    def test_multi_line_permit_single_line_equivalent(self) -> None:
        """Multi-line permit with action == on its own line should work."""
        policy = """permit
  principal,
  action == "bash",
  resource;"""

        cedar = CedarHook(policy)
        resp = cedar.evaluate(_req(action="bash"))
        assert resp.verdict == HookVerdict.ALLOW
        assert resp.policy_digest != ""

    def test_policy_digest_64_char_hex(self) -> None:
        """Policy digest should be a 64-char hex string (SHA-256)."""
        cedar = CedarHook('permit (action == "read");')
        resp = cedar.evaluate(_req(action="read"))
        assert isinstance(resp.policy_digest, str)
        assert len(resp.policy_digest) == 64  # SHA-256 hex digest length
        # Verify it's a valid sha256 hex
        int(resp.policy_digest, 16)


class TestOPAHook:
    def test_opa_binary_not_found_is_unavailable_not_abstain(self) -> None:
        """A missing engine did not decide anything, so it must not read as a no-match.

        This asserted ABSTAIN, which is what made an unreachable policy engine
        indistinguishable from a policy that simply had no rule for the request (#4912).
        """
        opa = OPAHook(policy_path="/nonexistent/policy.rego", opa_binary="/nonexistent/opa")
        resp = opa.evaluate(_req())
        assert resp.verdict == HookVerdict.UNAVAILABLE
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

        # `evaluate` now reports what happened - the engine did not answer - and
        # `first_decisive` is the single place that decides what unavailability MEANS.
        # Resolving it in both is how `fail_open` came to be consulted on a path that
        # could never run.
        registry = PolicyHookRegistry(fail_open=True)
        registry.register(FailingHook())
        responses = registry.evaluate(_req())
        assert responses[0].verdict == HookVerdict.UNAVAILABLE
        # fail_open: the unavailable engine is treated as having no opinion, so the
        # registry falls through to its default rather than denying.
        assert registry.first_decisive(_req()).verdict == HookVerdict.ABSTAIN

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
        assert responses[0].verdict == HookVerdict.UNAVAILABLE
        # The verdict a caller acts on: fail-closed turns unavailability into a DENY that
        # says why, rather than into the all-abstained default.
        decisive = registry.first_decisive(_req())
        assert decisive.verdict == HookVerdict.DENY
        assert "unavailable" in decisive.reason.lower()

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

    def test_policy_digest_included_in_responses(self) -> None:
        """All Cedar responses should include a policy digest."""
        cedar = CedarHook('permit (action == "read");')
        resp = cedar.evaluate(_req(action="read"))
        assert isinstance(resp.policy_digest, str)
        assert len(resp.policy_digest) == 64  # SHA-256 hex digest length
        # Verify it's a valid sha256 hex
        int(resp.policy_digest, 16)
