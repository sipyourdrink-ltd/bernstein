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

from bernstein.core.security.path_containment import (
    PathContainmentError,
    validate_relative_path,
)

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


#: Splits a path on either separator. A file manifest is written on one host
#: and read on another, so ``src\evil`` has to segment the same way on POSIX as
#: it does on Windows.
_PATH_SEPARATOR_RE = re.compile(r"[\\/]+")


def _path_segments(candidate: str) -> tuple[str, ...] | None:
    """Return *candidate* as path segments, or ``None`` if it is not containable.

    ``None`` means the value cannot sit under any relative scope at all: it is
    absolute, carries a ``..`` component, or names no component. The caller
    treats that as out of scope rather than trying to repair it, so a path
    that escapes its base is a violation instead of a comparison.

    Args:
        candidate: A path from the agent's modified-file manifest, or one
            entry of the declared scope.

    Returns:
        The non-empty path segments, or ``None`` when the value is not a safe
        relative path.
    """
    try:
        validate_relative_path(candidate, label="scope path")
    except PathContainmentError:
        return None
    return tuple(part for part in _PATH_SEPARATOR_RE.split(candidate) if part not in ("", "."))


def _within_scope(path: str, allowed: list[tuple[str, ...]]) -> bool:
    """Return True when *path* sits under one of the *allowed* scope prefixes.

    Args:
        path: One entry from the agent's modified-file manifest.
        allowed: Segment tuples of the declared scope, already validated.

    Returns:
        True when the path's leading segments equal some scope entry's
        segments; False when it is elsewhere in the tree, or is not a safe
        relative path.
    """
    segments = _path_segments(path)
    if segments is None:
        return False
    return any(segments[: len(entry)] == entry for entry in allowed)


class ScopeGuardrail:
    """Verify agent only modifies files within its scope.

    Membership is decided on whole path segments, not on the raw string. A
    prefix test answers the wrong question twice: ``"src_evil/foo.py"`` starts
    with ``"src"`` while sitting in a different directory, and
    ``"src/../etc/passwd"`` starts with ``"src/"`` while pointing outside the
    tree entirely. An agent that chooses its own ``modified_files`` list can
    spell either one, so both are refused here.
    """

    name = "scope"

    def check_input(self, _prompt: str, _context: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult(passed=True, guardrail_name=self.name)

    def check_output(self, _output: str, context: dict[str, Any]) -> GuardrailResult:
        scope: list[str] = context.get("scope", [])
        modified_files: list[str] = context.get("modified_files", [])
        if not scope or not modified_files:
            return GuardrailResult(passed=True, guardrail_name=self.name)
        # A scope entry that is not itself a safe relative path contains
        # nothing, so it is dropped rather than matched loosely. A scope list
        # made entirely of such entries admits no file at all, which is the
        # fail-closed direction.
        allowed = [segments for segments in (_path_segments(entry) for entry in scope) if segments is not None]
        violations = [
            f"File {f} is outside allowed scope {scope}" for f in modified_files if not _within_scope(f, allowed)
        ]
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

    #: One signature per credential shape a provider documents as fixed.
    #: A provider that adds a prefix does not retire the old one, so the
    #: legacy forms stay alongside the current ones. Order is cosmetic:
    #: every pattern is searched and each hit is reported separately.
    PATTERNS: ClassVar[list[str]] = [
        # OpenAI project keys. The legacy ``sk-``/``sk_`` body below stops
        # at the first hyphen, so it never covers this one.
        r"sk-proj-[A-Za-z0-9_-]{20,}",
        # Anthropic API and admin keys: sk-ant-api03-xxx, sk-ant-admin01-xxx.
        r"sk-ant-[a-z]+[0-9]*-[A-Za-z0-9_-]{20,}",
        r"(?:sk-|sk_)[a-zA-Z0-9]{20,}",
        # GitHub fine-grained personal access token.
        r"github_pat_[A-Za-z0-9_]{22,}",
        # Classic GitHub tokens, which share one 36-character body:
        # ghp_ personal, gho_ OAuth, ghu_ user-to-server,
        # ghs_ server-to-server, ghr_ refresh.
        r"gh[pousr]_[a-zA-Z0-9]{36}",
        # Any PEM private key, not only RSA. This project mints Ed25519
        # signing keys of its own, and an OPENSSH, EC, DSA or ENCRYPTED
        # body is the same disclosure as an RSA one.
        r"-----BEGIN\s+(?:[A-Z0-9]+\s+)?PRIVATE\s+KEY-----",
        r"AKIA[0-9A-Z]{16}",
    ]

    def __init__(self) -> None:
        self._compiled = [re.compile(p) for p in self.PATTERNS]

    def check_input(self, _prompt: str, _context: dict[str, Any]) -> GuardrailResult:
        return GuardrailResult(passed=True, guardrail_name=self.name)

    def check_output(self, output: str, _context: dict[str, Any]) -> GuardrailResult:
        violations = [
            f"Potential secret leak detected: {pattern.pattern}" for pattern in self._compiled if pattern.search(output)
        ]
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
    def default(cls, *, enable_owasp_asi: bool | None = None) -> GuardrailPipeline:
        """Create pipeline with all built-in guardrails.

        Args:
            enable_owasp_asi: When True, append the OWASP Top 10 for
                Agentic Apps detector pack. When False, force the pack
                off regardless of env. When None (the default), fall
                back to :func:`bernstein.core.security.owasp_asi_detectors.is_owasp_asi_enabled`,
                which is **on by default** and respects the
                ``BERNSTEIN_DISABLE_OWASP_ASI=1`` opt-out.
        """
        pipeline = cls()
        pipeline.add(PromptInjectionGuardrail())
        pipeline.add(ScopeGuardrail())
        pipeline.add(CostGuardrail())
        pipeline.add(SecretLeakGuardrail())
        opt_in = enable_owasp_asi
        if opt_in is None:
            # Late import keeps the OWASP pack optional and avoids
            # creating an import cycle inside core.security. The probe
            # itself is on-by-default with an env-var opt-out.
            try:
                from bernstein.core.security.owasp_asi_detectors import (
                    is_owasp_asi_enabled,
                )

                opt_in = is_owasp_asi_enabled()
            except Exception:
                logger.exception(
                    "Failed to evaluate OWASP ASI opt-out flag; falling back to disabled "
                    "to keep the pipeline available."
                )
                opt_in = False
        if opt_in:
            pipeline.with_owasp_asi()
        return pipeline

    def with_owasp_asi(self, **kwargs: Any) -> GuardrailPipeline:
        """Append the OWASP Top 10 for Agentic Apps detector pack.

        Returns ``self`` for fluent chaining. Detector load failures
        are caught and logged so the orchestrator keeps running with
        the existing pipeline.
        """
        try:
            from bernstein.core.security.owasp_asi_detectors import OwaspAsiGuardrail

            self.add(OwaspAsiGuardrail(**kwargs))
        except Exception:
            logger.exception("Failed to load OWASP ASI detector pack; skipping.")
        return self
