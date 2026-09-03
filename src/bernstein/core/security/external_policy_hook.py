"""SEC-011: Permission hooks for external policy engines (OPA, Cedar).

Hook fires before permission check, response overrides default.  External
engines can enforce organizational policies that are maintained outside
of Bernstein's configuration.

Usage::

    from bernstein.core.security.external_policy_hook import (
        ExternalPolicyHook,
        PolicyHookRegistry,
        HookRequest,
    )

    registry = PolicyHookRegistry()
    registry.register(opa_hook)
    result = registry.evaluate(HookRequest(action="bash", resource="rm -rf /"))

Requests and responses here are expressible in the AuthZEN 1.0 evaluation shape
(:mod:`bernstein.core.security.authzen`), and the registry normalises every
request through it before dispatch, so the decision boundary speaks one
vocabulary whether the caller is internal or a foreign enforcement point.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.security.agent_card_signer import canonicalize_jcs

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore

from bernstein.core.security.authzen import (
    RESOURCE_TYPE_OPAQUE,
    SUBJECT_TYPE_AGENT,
    AuthZenAction,
    AuthZenError,
    AuthZenRequest,
    AuthZenResource,
    AuthZenResponse,
    AuthZenSubject,
    Obligation,
)

logger = logging.getLogger(__name__)

#: Subject properties an internal :class:`HookRequest` can carry.
_CARRIED_SUBJECT_PROPERTIES = frozenset({"role"})


def _sha256_hex(data: bytes) -> str:
    """Return the bare lower-case SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


class HookVerdict(StrEnum):
    """Verdicts returned by external policy hooks."""

    ALLOW = "allow"
    DENY = "deny"
    ABSTAIN = "abstain"  # Hook has no opinion
    #: The engine did not produce an answer: binary missing, non-zero exit, timeout,
    #: unparseable output, or an exception. Distinct from ABSTAIN on purpose - "no rule
    #: matched" is a decision the policy made, and this is the absence of one. Collapsing
    #: them makes an unreachable policy engine indistinguishable from a permissive one,
    #: which is the wrong default for a decision boundary (#4912).
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class HookRequest:
    """Request sent to external policy hooks.

    Attributes:
        action: The action being requested.
        resource: The resource being acted upon.
        agent_id: Agent identifier.
        role: Agent role.
        scope: Task scope.
        metadata: Additional context.
    """

    action: str
    resource: str
    agent_id: str = ""
    role: str = ""
    scope: str = ""
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_authzen(self) -> AuthZenRequest:
        """Return this request in the AuthZEN 1.0 evaluation shape.

        Raises:
            AuthZenError: If the request cannot be expressed in the standard
                shape - an empty action, or metadata keyed in a way the
                standard context cannot carry.
        """
        properties: dict[str, Any] = {"role": self.role} if self.role else {}
        context: dict[str, Any] = {}
        if self.scope:
            context["scope"] = self.scope
        if self.metadata:
            context["metadata"] = dict(self.metadata)
        return AuthZenRequest(
            subject=AuthZenSubject(type=SUBJECT_TYPE_AGENT, id=self.agent_id, properties=properties),
            resource=AuthZenResource(type=RESOURCE_TYPE_OPAQUE, id=self.resource),
            action=AuthZenAction(name=self.action),
            context=context,
        )

    @classmethod
    def from_authzen(cls, request: AuthZenRequest) -> HookRequest:
        """Build an internal request from the AuthZEN shape.

        Entity properties this request cannot carry are refused rather than
        dropped, for the same reason unknown context is refused: an engine
        answering over fewer attributes than it was sent has answered a
        different question.

        Raises:
            AuthZenError: If the payload carries attributes that would be lost.
        """
        stray_subject = sorted(set(request.subject.properties) - _CARRIED_SUBJECT_PROPERTIES)
        if stray_subject:
            raise AuthZenError(f"subject properties an internal request cannot carry: {', '.join(stray_subject)}")
        if request.resource.properties:
            raise AuthZenError(
                f"resource properties an internal request cannot carry: "
                f"{', '.join(sorted(request.resource.properties))}",
            )
        if request.action.properties:
            raise AuthZenError(
                f"action properties an internal request cannot carry: {', '.join(sorted(request.action.properties))}",
            )
        role = request.subject.properties.get("role", "")
        if not isinstance(role, str):
            raise AuthZenError("subject property 'role' must be a string")
        metadata = request.context.get("metadata", {})
        if not isinstance(metadata, dict):
            raise AuthZenError("context field 'metadata' must be a JSON object")
        scope = request.context.get("scope", "")
        if not isinstance(scope, str):
            raise AuthZenError("context field 'scope' must be a string")
        return cls(
            action=request.action.name,
            resource=request.resource.id,
            agent_id=request.subject.id,
            role=role,
            scope=scope,
            metadata=dict(metadata),  # pyright: ignore[reportUnknownArgumentType]
        )


def _request_digest(request: HookRequest) -> str:
    """Return the SHA-256 digest of *request* in RFC 8785 canonical form.

    The digest covers every field including ``metadata``, so a holder of the
    original request can recompute it and match it against a chain record whose
    payload deliberately does not carry the caller's free-form context.
    """
    return _sha256_hex(
        canonicalize_jcs(
            {
                "action": request.action,
                "resource": request.resource,
                "agent_id": request.agent_id,
                "role": request.role,
                "scope": request.scope,
                "metadata": request.metadata,
            },
        ),
    )


@dataclass(frozen=True)
class HookResponse:
    """Response from an external policy hook.

    Attributes:
        hook_name: Name of the hook that produced this response.
        verdict: The hook's verdict.
        reason: Explanation of the verdict.
        latency_ms: Time taken for the hook evaluation in milliseconds.
        error: Error message if the hook failed.
        policy_digest: SHA-256 digest of the policy that produced this verdict --
            the policy text for Cedar, the policy file's bytes for OPA. Empty when
            the engine could not name a policy (an unreadable file, say).
        obligations: Conditions attached to the verdict.  A permit carrying one
            has not permitted the request as it was asked.
    """

    hook_name: str
    verdict: HookVerdict
    reason: str
    latency_ms: float = 0.0
    error: str = ""
    policy_digest: str = ""
    obligations: tuple[Obligation, ...] = ()

    def to_authzen(self) -> AuthZenResponse:
        """Return this response in the AuthZEN 1.0 evaluation shape.

        Only :attr:`HookVerdict.ALLOW` becomes a permit.  The bernstein verdict
        travels alongside the boolean because AuthZEN's ``decision`` cannot tell
        a denial apart from an abstention or an unreachable engine.
        """
        return AuthZenResponse(
            decision=self.verdict is HookVerdict.ALLOW,
            obligations=self.obligations,
            reason=self.reason,
            verdict=str(self.verdict),
            hook_name=self.hook_name,
        )


class ExternalPolicyHook(ABC):
    """Abstract base for external policy engine hooks."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this hook."""

    @abstractmethod
    def evaluate(self, request: HookRequest) -> HookResponse:
        """Evaluate the request against the external policy.

        Args:
            request: The permission request to evaluate.

        Returns:
            The hook's response.
        """


class OPAHook(ExternalPolicyHook):
    """Hook that queries Open Policy Agent (OPA) for decisions.

    Sends the request as JSON input to OPA's REST API or CLI and parses
    the ``result.allow`` field from the response.

    Args:
        policy_path: Path to the Rego policy file.
        opa_binary: Path to the OPA binary (auto-detected if not provided).
        package_name: Rego package name for query (default ``"bernstein.authz"``).
    """

    def __init__(
        self,
        policy_path: Path | str,
        opa_binary: str | None = None,
        package_name: str = "bernstein.authz",
    ) -> None:
        self._policy_path = Path(policy_path)
        self._opa_binary = opa_binary or shutil.which("opa") or "opa"
        self._package_name = package_name

    @property
    def name(self) -> str:
        return "opa"

    def _current_policy_digest(self) -> str:
        """Return the SHA-256 of the policy file as it stands on disk right now.

        Read per evaluation rather than cached at construction: a decision record
        names the policy that produced *this* verdict, and a policy file edited
        under a long-lived hook would otherwise be attested by a digest of the
        bytes it no longer has. An unreadable file yields ``""`` -- the engine
        could not name a policy, which is exactly what an empty digest means.
        """
        try:
            return _sha256_hex(self._policy_path.read_bytes())
        except OSError:
            return ""

    def evaluate(self, request: HookRequest) -> HookResponse:
        """Query OPA with the request and return the verdict.

        Args:
            request: The permission request.

        Returns:
            ALLOW, DENY, or ABSTAIN based on OPA evaluation.
        """
        start = time.monotonic()
        policy_digest = self._current_policy_digest()
        input_data = {
            "input": {
                "action": request.action,
                "resource": request.resource,
                "agent_id": request.agent_id,
                "role": request.role,
                "scope": request.scope,
                "metadata": request.metadata,
            },
        }

        input_file = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
            ) as f:
                json.dump(input_data, f)
                input_file = f.name

            query = f"data.{self._package_name}.allow"
            result = subprocess.run(
                [
                    self._opa_binary,
                    "eval",
                    "--data",
                    str(self._policy_path),
                    "--input",
                    input_file,
                    query,
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )

            latency = (time.monotonic() - start) * 1000

            if result.returncode != 0:
                return HookResponse(
                    hook_name=self.name,
                    verdict=HookVerdict.UNAVAILABLE,
                    reason=f"OPA evaluation failed: {result.stderr.strip()}",
                    latency_ms=latency,
                    policy_digest=policy_digest,
                    error=result.stderr.strip(),
                )

            parsed = json.loads(result.stdout)
            expressions = parsed.get("result", [{}])
            if expressions:
                first_expr = expressions[0]
                allowed = first_expr.get("expressions", [{}])
                if allowed and allowed[0].get("value") is True:
                    return HookResponse(
                        hook_name=self.name,
                        verdict=HookVerdict.ALLOW,
                        reason="Allowed by OPA policy",
                        latency_ms=latency,
                        policy_digest=policy_digest,
                    )
                return HookResponse(
                    hook_name=self.name,
                    verdict=HookVerdict.DENY,
                    reason="Denied by OPA policy",
                    latency_ms=latency,
                    policy_digest=policy_digest,
                )

            # Deliberately ABSTAIN, not UNAVAILABLE: OPA ran, exited zero and answered
            # with nothing, which is a genuine no-match rather than an engine that could
            # not be reached. The distinction this verdict draws is about availability.
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.ABSTAIN,
                reason="OPA returned empty result",
                latency_ms=latency,
                policy_digest=policy_digest,
            )

        except FileNotFoundError:
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.UNAVAILABLE,
                reason="OPA binary not found",
                latency_ms=latency,
                policy_digest=policy_digest,
                error="opa not found on PATH",
            )
        except subprocess.TimeoutExpired:
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.UNAVAILABLE,
                reason="OPA evaluation timed out",
                latency_ms=latency,
                policy_digest=policy_digest,
                error="timeout",
            )
        except Exception as exc:
            # json.JSONDecodeError lands here too: output we cannot parse is output the
            # engine did not give us, whatever its exit code said.
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.UNAVAILABLE,
                reason=f"OPA hook error: {exc}",
                latency_ms=latency,
                policy_digest=policy_digest,
                error=str(exc),
            )
        finally:
            if input_file:
                Path(input_file).unlink(missing_ok=True)


class CedarHook(ExternalPolicyHook):
    """Hook that evaluates Cedar policies.

    Cedar policies are evaluated by constructing a request and checking
    it against the policy set.  This implementation uses a simplified
    in-process evaluation since Cedar doesn't have a standard CLI like OPA.

    Args:
        policy_text: Cedar policy text.
    """

    def __init__(self, policy_text: str) -> None:
        self._policy_text = policy_text
        self._allow_patterns: list[str] = []
        self._deny_patterns: list[str] = []
        self._policy_digest: str = ""
        self._parse_policy()

    def _parse_policy(self) -> None:
        """Parse Cedar policy text into simple allow/deny patterns.

        This is a simplified parser for the subset of Cedar used in
        Bernstein.  Full Cedar evaluation would require a Cedar engine.

        Parses whole policy statements (to the terminating `;`), not lines,
        so conventional multi-line formatting is read correctly. Rejects any
        policy containing unsupported constructs at construction time.

        Raises:
            ValueError: If the policy contains unsupported constructs.
        """
        # Compute digest of the original policy text for verdicts
        self._policy_digest = self._compute_digest(self._policy_text)

        # Parse the policy text into statements
        statements = self._split_into_statements(self._policy_text)

        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue

            # Parse the statement into components
            parsed = self._parse_statement(stmt)

            # Extract action patterns from parsed statement
            if parsed["type"] == "permit":
                if parsed["has_when"]:
                    raise ValueError(f"Unsupported construct 'when' in policy statement: {stmt}")
                for action in parsed["actions"]:
                    self._allow_patterns.append(action)
            elif parsed["type"] == "forbid":
                if parsed["has_when"]:
                    raise ValueError(f"Unsupported construct 'when' in policy statement: {stmt}")
                for action in parsed["actions"]:
                    self._deny_patterns.append(action)
            else:
                raise ValueError(f"Unsupported statement type in policy: {parsed['type']}")

    def _compute_digest(self, text: str) -> str:
        """Compute a digest of the policy text for verdicts."""
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _split_into_statements(self, policy_text: str) -> list[str]:
        """Split policy text into individual statements delimited by semicolons.

        This handles multi-line statements by finding the matching semicolon
        that terminates each complete policy statement.
        """
        statements = []
        current = []
        bracket_depth = 0
        paren_depth = 0

        for char in policy_text:
            current.append(char)
            if char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth -= 1
            elif char == "{":
                bracket_depth += 1
            elif char == "}":
                bracket_depth -= 1
            elif char == ";" and paren_depth == 0 and bracket_depth == 0:
                statements.append("".join(current))
                current = []

        # Add any remaining text as a partial statement
        if current:
            statements.append("".join(current))

        return statements

    def _parse_statement(self, stmt: str) -> dict:
        """Parse a single policy statement into components.

        Returns a dict with:
        - type: "permit" or "forbid"
        - actions: list of action strings
        - has_when: True if statement contains 'when' clause
        """
        # Normalize whitespace
        stmt = " ".join(stmt.split())

        # Extract statement type
        if stmt.startswith("permit"):
            stmt_type = "permit"
        elif stmt.startswith("forbid"):
            stmt_type = "forbid"
        else:
            raise ValueError(f"Unsupported statement type: {stmt}")

        # Check for unsupported constructs
        if "when" in stmt:
            raise ValueError(f"Unsupported construct 'when' in policy statement: {stmt}")
        if "unless" in stmt:
            raise ValueError(f"Unsupported construct 'unless' in policy statement: {stmt}")
        if "?principal" in stmt:
            raise ValueError(f"Unsupported construct '?principal' in policy statement: {stmt}")

        # Extract actions from "action == \"...\""
        import re

        action_pattern = r'action\s*==\s*"([^"]+)"'
        actions = re.findall(action_pattern, stmt)

        return {
            "type": stmt_type,
            "actions": actions,
            "has_when": "when" in stmt,
        }

    @property
    def name(self) -> str:
        return "cedar"

    def evaluate(self, request: HookRequest) -> HookResponse:
        """Evaluate the request against Cedar policies.

        Args:
            request: The permission request.

        Returns:
            ALLOW, DENY, or ABSTAIN based on Cedar evaluation.
        """
        start = time.monotonic()

        if request.action in self._deny_patterns:
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.DENY,
                reason=f"Denied by Cedar policy for action {request.action!r}",
                latency_ms=latency,
                policy_digest=self._policy_digest,
            )

        if request.action in self._allow_patterns:
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.ALLOW,
                reason=f"Allowed by Cedar policy for action {request.action!r}",
                latency_ms=latency,
                policy_digest=self._policy_digest,
            )

        latency = (time.monotonic() - start) * 1000
        return HookResponse(
            hook_name=self.name,
            verdict=HookVerdict.ABSTAIN,
            reason=f"No Cedar policy for action {request.action!r}",
            latency_ms=latency,
            policy_digest=self._policy_digest,
        )


class PolicyHookRegistry:
    """Registry that manages and evaluates multiple external policy hooks.

    Hooks are evaluated in registration order.  The first non-ABSTAIN
    verdict wins.  If all hooks abstain, the default verdict applies.

    An engine that could not answer is decisive and DENIES. UNAVAILABLE is never
    collapsed into the all-abstained default: "the policy engine is unreachable" and
    "no rule matched" have opposite safety properties, and reporting the first as the
    second makes a broken decision boundary look like a permissive one (#4912).

    When an audit chain is supplied, every hook evaluation appends one
    ``external_policy.decision`` record to it - allow, deny, abstain and
    unavailable alike. A refusal an operator cannot demonstrate afterwards is a
    refusal they can only assert, and an evidence trail that carried only
    refusals could not distinguish an engine that was consulted and had no rule
    from one that was never consulted at all (#4912).

    Args:
        default_verdict: Verdict when all hooks abstain.
        fail_open: If True, an engine that cannot answer is treated as having no
            opinion and evaluation continues; if False (the default), it denies.
            This used to be consulted only for exceptions escaping ``hook.evaluate``,
            which cannot happen because that method catches everything - so the flag
            was unreachable. It is now the per-deployment switch it was written to be.
        audit_chain: Optional chain store receiving one decision record per hook
            evaluation. Recording is opt-in; an unwired registry still decides.
    """

    def __init__(
        self,
        default_verdict: HookVerdict = HookVerdict.ABSTAIN,
        fail_open: bool = False,
        audit_chain: AuditChainStore | None = None,
    ) -> None:
        self._hooks: list[ExternalPolicyHook] = []
        self._default_verdict = default_verdict
        self._fail_open = fail_open
        self._audit_chain = audit_chain

    @property
    def hooks(self) -> list[ExternalPolicyHook]:
        """Return the list of registered hooks."""
        return self._hooks.copy()

    def register(self, hook: ExternalPolicyHook) -> None:
        """Register an external policy hook.

        Args:
            hook: The hook to register.
        """
        self._hooks.append(hook)
        logger.info("Registered external policy hook: %s", hook.name)

    def evaluate(self, request: HookRequest) -> list[HookResponse]:
        """Evaluate a request against all registered hooks.

        The request is normalised through the AuthZEN shape before any hook sees
        it, so an internal decision and one arriving from a foreign enforcement
        point are evaluated over the same bytes.  A request the standard shape
        cannot express never reaches an engine.

        Each response is appended to the audit chain when one is configured, so
        the record set matches the evaluation set exactly. A failure to record is
        not swallowed: it propagates, because a decision boundary that keeps
        deciding after its evidence trail has stopped being written is the silent
        failure this module exists to remove.

        Args:
            request: The permission request.

        Returns:
            List of responses from all hooks.

        Raises:
            AuthZenError: If the request cannot be expressed in the AuthZEN shape.
        """
        request = HookRequest.from_authzen(request.to_authzen())
        responses: list[HookResponse] = []
        digest = _request_digest(request) if self._audit_chain is not None else ""
        for hook in self._hooks:
            try:
                response = hook.evaluate(request)
            except Exception as exc:
                # An escaping exception is an engine that did not answer, like any other
                # unavailability. Reported as UNAVAILABLE rather than resolved to
                # ALLOW/DENY here so that ONE place decides what unavailability means -
                # `first_decisive`, which honours `fail_open`. Deciding it twice is how
                # the flag came to be consulted on a path that could never run.
                response = HookResponse(
                    hook_name=hook.name,
                    verdict=HookVerdict.UNAVAILABLE,
                    reason=f"Hook error: {exc}",
                    error=str(exc),
                )
            responses.append(response)
            self._record(request, response, digest)
        return responses

    def _record(self, request: HookRequest, response: HookResponse, digest: str) -> None:
        """Append one decision record for *response*, if a chain is configured.

        The engine's own verdict is written down, never the registry's resolution
        of it: an ``UNAVAILABLE`` engine that :meth:`first_decisive` turns into a
        denial is recorded as unavailable, so the chain says why the run stopped
        rather than only that it did.
        """
        chain = self._audit_chain
        if chain is None:
            return

        from bernstein.core.security.audit_chain import record_external_policy_decision

        record_external_policy_decision(
            chain=chain,
            engine=response.hook_name,
            verdict=response.verdict.value,
            reason=response.reason,
            action=request.action,
            resource=request.resource,
            agent_id=request.agent_id,
            role=request.role,
            scope=request.scope,
            request_digest=digest,
            policy_digest=response.policy_digest,
            error_digest=_sha256_hex(response.error.encode("utf-8")) if response.error else "",
            latency_ms=response.latency_ms,
        )

    def first_decisive(self, request: HookRequest) -> HookResponse:
        """Evaluate hooks and return the first decisive response.

        An UNAVAILABLE engine is decisive and denies, unless ``fail_open`` is set, in
        which case it is treated as having no opinion and evaluation continues. Either
        way the verdict says which happened: the caller is never handed "all hooks
        abstained" about an engine that was never reached.

        Args:
            request: The permission request.

        Returns:
            First decisive response, or a default ABSTAIN response.

        Raises:
            AuthZenError: If the request cannot be expressed in the AuthZEN shape.
        """
        responses = self.evaluate(request)
        for resp in responses:
            if resp.verdict == HookVerdict.UNAVAILABLE:
                if self._fail_open:
                    continue
                return HookResponse(
                    hook_name=resp.hook_name,
                    verdict=HookVerdict.DENY,
                    reason=f"Policy engine unavailable, denying: {resp.reason}",
                    latency_ms=resp.latency_ms,
                    error=resp.error,
                    policy_digest=resp.policy_digest,
                )
            if resp.verdict != HookVerdict.ABSTAIN:
                return resp
        return HookResponse(
            hook_name="registry",
            verdict=self._default_verdict,
            reason="All hooks abstained",
        )
