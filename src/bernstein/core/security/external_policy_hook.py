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
"""

from __future__ import annotations

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
from typing import Any

logger = logging.getLogger(__name__)


class HookVerdict(StrEnum):
    """Verdicts returned by external policy hooks."""

    ALLOW = "allow"
    DENY = "deny"
    ABSTAIN = "abstain"  # Hook has no opinion


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


@dataclass(frozen=True)
class HookResponse:
    """Response from an external policy hook.

    Attributes:
        hook_name: Name of the hook that produced this response.
        verdict: The hook's verdict.
        reason: Explanation of the verdict.
        latency_ms: Time taken for the hook evaluation in milliseconds.
        error: Error message if the hook failed.
        policy_digest: SHA-256 digest of the policy text that produced this verdict (for Cedar).
    """

    hook_name: str
    verdict: HookVerdict
    reason: str
    latency_ms: float = 0.0
    error: str = ""
    policy_digest: str = ""


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

    def evaluate(self, request: HookRequest) -> HookResponse:
        """Query OPA with the request and return the verdict.

        Args:
            request: The permission request.

        Returns:
            ALLOW, DENY, or ABSTAIN based on OPA evaluation.
        """
        start = time.monotonic()
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
                    verdict=HookVerdict.ABSTAIN,
                    reason=f"OPA evaluation failed: {result.stderr.strip()}",
                    latency_ms=latency,
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
                    )
                return HookResponse(
                    hook_name=self.name,
                    verdict=HookVerdict.DENY,
                    reason="Denied by OPA policy",
                    latency_ms=latency,
                )

            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.ABSTAIN,
                reason="OPA returned empty result",
                latency_ms=latency,
            )

        except FileNotFoundError:
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.ABSTAIN,
                reason="OPA binary not found",
                latency_ms=latency,
                error="opa not found on PATH",
            )
        except subprocess.TimeoutExpired:
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.ABSTAIN,
                reason="OPA evaluation timed out",
                latency_ms=latency,
                error="timeout",
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return HookResponse(
                hook_name=self.name,
                verdict=HookVerdict.ABSTAIN,
                reason=f"OPA hook error: {exc}",
                latency_ms=latency,
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

    Args:
        default_verdict: Verdict when all hooks abstain.
        fail_open: If True, hook errors result in ALLOW; if False, DENY.
    """

    def __init__(
        self,
        default_verdict: HookVerdict = HookVerdict.ABSTAIN,
        fail_open: bool = False,
    ) -> None:
        self._hooks: list[ExternalPolicyHook] = []
        self._default_verdict = default_verdict
        self._fail_open = fail_open

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

        Args:
            request: The permission request.

        Returns:
            List of responses from all hooks.
        """
        responses: list[HookResponse] = []
        for hook in self._hooks:
            try:
                response = hook.evaluate(request)
                responses.append(response)
            except Exception as exc:
                verdict = HookVerdict.ALLOW if self._fail_open else HookVerdict.DENY
                responses.append(
                    HookResponse(
                        hook_name=hook.name,
                        verdict=verdict,
                        reason=f"Hook error: {exc}",
                        error=str(exc),
                    ),
                )
        return responses

    def first_decisive(self, request: HookRequest) -> HookResponse:
        """Evaluate hooks and return the first non-ABSTAIN response.

        Args:
            request: The permission request.

        Returns:
            First decisive response, or a default ABSTAIN response.
        """
        responses = self.evaluate(request)
        for resp in responses:
            if resp.verdict != HookVerdict.ABSTAIN:
                return resp
        return HookResponse(
            hook_name="registry",
            verdict=self._default_verdict,
            reason="All hooks abstained",
        )
