"""An unreachable policy engine must not read as "no rule matched" (#4912).

`external_policy_hook` resolved every failure inside `OPAHook.evaluate` to
`ABSTAIN`: missing binary, non-zero exit, timeout, unparseable output, any
exception. `PolicyHookRegistry.first_decisive` then fell through to its
all-abstained default, which is also `ABSTAIN`. So "the engine could not be
reached" and "the policy had no rule for this" produced the same verdict, and
the two have opposite safety properties.

`fail_open` existed for exactly this and could never fire: it was consulted only
for exceptions escaping `hook.evaluate`, and that method catches everything.

Nothing consults the registry yet (scope item 1 of #4912), so this changes no
live decision. It makes the boundary fail closed before it is given traffic.
"""

from __future__ import annotations

from bernstein.core.security.external_policy_hook import (
    ExternalPolicyHook,
    HookRequest,
    HookResponse,
    HookVerdict,
    OPAHook,
    PolicyHookRegistry,
)


def _req() -> HookRequest:
    return HookRequest(action="deploy", resource="prod", agent_id="agent-1")


class _Unreachable(ExternalPolicyHook):
    """An engine that raises rather than answering."""

    @property
    def name(self) -> str:
        return "unreachable"

    def evaluate(self, request: HookRequest) -> HookResponse:
        raise RuntimeError("engine down")


class _Abstains(ExternalPolicyHook):
    """An engine that answers, with no opinion."""

    @property
    def name(self) -> str:
        return "quiet"

    def evaluate(self, request: HookRequest) -> HookResponse:
        return HookResponse(hook_name=self.name, verdict=HookVerdict.ABSTAIN, reason="no rule")


class _Allows(ExternalPolicyHook):
    @property
    def name(self) -> str:
        return "permissive"

    def evaluate(self, request: HookRequest) -> HookResponse:
        return HookResponse(hook_name=self.name, verdict=HookVerdict.ALLOW, reason="ok")


def test_a_missing_binary_denies_by_default() -> None:
    """The headline: an engine that cannot run refuses, rather than waving it through."""
    registry = PolicyHookRegistry()
    registry.register(OPAHook(policy_path="/nonexistent.rego", opa_binary="/nonexistent/opa"))
    decisive = registry.first_decisive(_req())
    assert decisive.verdict == HookVerdict.DENY


def test_the_denial_says_the_engine_was_unavailable() -> None:
    """A refusal an operator cannot explain is a refusal they will disable."""
    registry = PolicyHookRegistry()
    registry.register(OPAHook(policy_path="/nonexistent.rego", opa_binary="/nonexistent/opa"))
    decisive = registry.first_decisive(_req())
    assert "unavailable" in decisive.reason.lower()
    assert decisive.error


def test_unavailable_is_never_reported_as_all_hooks_abstained() -> None:
    """The exact collapse this change exists to prevent."""
    registry = PolicyHookRegistry()
    registry.register(_Unreachable())
    decisive = registry.first_decisive(_req())
    assert "abstained" not in decisive.reason.lower()


def test_a_genuine_no_match_still_abstains() -> None:
    """An engine that ANSWERED with no opinion must keep answering that.

    The over-correction guard: if every quiet path became a denial, a policy set
    with no rule for an action would start refusing it, which is a different bug
    in the same place.
    """
    registry = PolicyHookRegistry()
    registry.register(_Abstains())
    assert registry.first_decisive(_req()).verdict == HookVerdict.ABSTAIN


def test_fail_open_treats_an_unavailable_engine_as_no_opinion() -> None:
    """The per-deployment switch, finally reachable.

    Before this it was consulted only for exceptions escaping `hook.evaluate`, which
    catches everything - so no configuration of the flag changed any outcome.
    """
    registry = PolicyHookRegistry(fail_open=True)
    registry.register(_Unreachable())
    assert registry.first_decisive(_req()).verdict == HookVerdict.ABSTAIN


def test_fail_open_keeps_evaluating_later_hooks() -> None:
    """Treating it as no-opinion means CONTINUING, not stopping with a default.

    A registry that returned its default on the first unavailable hook would silently
    drop every engine registered after it.
    """
    registry = PolicyHookRegistry(fail_open=True)
    registry.register(_Unreachable())
    registry.register(_Allows())
    assert registry.first_decisive(_req()).verdict == HookVerdict.ALLOW


def test_an_unavailable_engine_outranks_a_later_allow_when_failing_closed() -> None:
    """Order matters: the boundary stops at the engine that could not speak.

    Otherwise a permissive hook registered behind a broken one would answer for it,
    which is the failure mode dressed as working.
    """
    registry = PolicyHookRegistry(fail_open=False)
    registry.register(_Unreachable())
    registry.register(_Allows())
    assert registry.first_decisive(_req()).verdict == HookVerdict.DENY
