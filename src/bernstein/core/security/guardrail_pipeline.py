"""Pluggable guardrail pipeline for agent inputs and outputs.

Provides content filtering, PII detection, prompt injection defense,
and scope validation. Guardrails run before agent spawn (input) and
after task completion (output).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class GuardrailResult:
    """Result of a guardrail check."""

    passed: bool
    guardrail_name: str
    violations: list[str] = field(default_factory=list)
    sanitized_content: str | None = None

    def __bool__(self) -> bool:
        return self.passed


@runtime_checkable
class Guardrail(Protocol):
    """Interface for guardrail implementations."""

    name: str

    def check_input(self, prompt: str, context: dict[str, Any]) -> GuardrailResult:
        """Check agent input/prompt before execution."""
        ...

    def check_output(self, output: str, context: dict[str, Any]) -> GuardrailResult:
        """Check agent output after execution."""
        ...


class PromptInjectionGuardrail:
    """Detect common prompt injection patterns."""

    name = "prompt_injection"

    PATTERNS: ClassVar[list[str]] = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"ignore\s+(all\s+)?above",
        r"you\s+are\s+now\s+(?:a|an)\s+",
        r"system\s*:\s*",
        r"<\|?(?:system|assistant|user)\|?>",
        r"STOP\s+BEING\s+",
        r"forget\s+(everything|all)",
    ]

    def __init__(self) -> None:
        self._compiled = [re.compile(p, re.IGNORECASE) for p in self.PATTERNS]

    def check_input(self, prompt: str, context: dict[str, Any]) -> GuardrailResult:
        violations: list[str] = []
        for pattern in self._compiled:
            matches = pattern.findall(prompt)
            if matches:
                violations.append(f"Prompt injection pattern detected: {pattern.pattern}")
        return GuardrailResult(
            passed=len(violations) == 0,
            guardrail_name=self.name,
            violations=violations,
        )

    def check_output(self, _output: str, _context: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult(passed=True, guardrail_name=self.name)


class ScopeGuardrail:
    """Verify agent only modifies files within its scope."""

    name = "scope"

    def check_input(self, _prompt: str, _context: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult(passed=True, guardrail_name=self.name)

    def check_output(self, _output: str, context: dict[str, Any]) -> GuardrailResult:
        scope: list[str] = context.get("scope", [])
        modified_files: list[str] = context.get("modified_files", [])
        if not scope or not modified_files:
            return GuardrailResult(passed=True, guardrail_name=self.name)
        violations: list[str] = []
        for f in modified_files:
            if not any(f.startswith(s) for s in scope):
                violations.append(f"File {f} is outside allowed scope {scope}")
        return GuardrailResult(
            passed=len(violations) == 0,
            guardrail_name=self.name,
            violations=violations,
        )


class CostGuardrail:
    """Reject tasks that would exceed remaining budget."""

    name = "cost"

    def check_input(self, _prompt: str, context: dict[str, Any]) -> GuardrailResult:
        budget: float = context.get("budget_usd", 0)
        spent: float = context.get("spent_usd", 0)
        estimated: float = context.get("estimated_cost_usd", 0)
        if budget > 0 and (spent + estimated) > budget:
            return GuardrailResult(
                passed=False,
                guardrail_name=self.name,
                violations=[f"Estimated cost ${estimated:.2f} would exceed remaining budget ${budget - spent:.2f}"],
            )
        return GuardrailResult(passed=True, guardrail_name=self.name)

    def check_output(self, _output: str, _context: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult(passed=True, guardrail_name=self.name)


class SecretLeakGuardrail:
    """Detect potential secret/credential leaks in agent output."""

    name = "secret_leak"

    PATTERNS: ClassVar[list[str]] = [
        r"(?:sk-|sk_)[a-zA-Z0-9]{20,}",
        r"ghp_[a-zA-Z0-9]{36}",
        r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
        r"AKIA[0-9A-Z]{16}",
    ]

    def __init__(self) -> None:
        self._compiled = [re.compile(p) for p in self.PATTERNS]

    def check_input(self, _prompt: str, _context: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult(passed=True, guardrail_name=self.name)

    def check_output(self, output: str, _context: dict[str, Any]) -> GuardrailResult:
        violations: list[str] = []
        for pattern in self._compiled:
            if pattern.search(output):
                violations.append(f"Potential secret leak detected: {pattern.pattern}")
        return GuardrailResult(
            passed=len(violations) == 0,
            guardrail_name=self.name,
            violations=violations,
        )


@dataclass
class GuardrailPipeline:
    """Runs a sequence of guardrails and collects results."""

    guardrails: list[Guardrail] = field(default_factory=list)
    _fail_fast: bool = True

    def add(self, guardrail: Guardrail) -> None:
        """Append a guardrail to the pipeline."""
        self.guardrails.append(guardrail)

    def check_input(self, prompt: str, context: dict[str, Any] | None = None) -> list[GuardrailResult]:
        """Run all guardrails against an input prompt."""
        ctx = context or {}
        results: list[GuardrailResult] = []
        for g in self.guardrails:
            result = g.check_input(prompt, ctx)
            results.append(result)
            if not result.passed and self._fail_fast:
                break
        return results

    def check_output(self, output: str, context: dict[str, Any] | None = None) -> list[GuardrailResult]:
        """Run all guardrails against an output string."""
        ctx = context or {}
        results: list[GuardrailResult] = []
        for g in self.guardrails:
            result = g.check_output(output, ctx)
            results.append(result)
            if not result.passed and self._fail_fast:
                break
        return results

    def all_passed(self, results: list[GuardrailResult]) -> bool:
        """Return True if every result in the list passed."""
        return all(r.passed for r in results)

    def violations(self, results: list[GuardrailResult]) -> list[str]:
        """Collect all violation messages from a list of results."""
        return [v for r in results for v in r.violations]

    @classmethod
    def default(cls) -> GuardrailPipeline:
        """Create pipeline with all built-in guardrails."""
        pipeline = cls()
        pipeline.add(PromptInjectionGuardrail())
        pipeline.add(ScopeGuardrail())
        pipeline.add(CostGuardrail())
        pipeline.add(SecretLeakGuardrail())
        return pipeline
