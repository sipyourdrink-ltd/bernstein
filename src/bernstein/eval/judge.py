"""LLM judge — evaluate code quality of agent-produced changes.

Dual-attempt strategy: standard prompt first, strict JSON suffix on parse failure.
Circuit breaker stops after consecutive failures. Retry with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Literal, cast

logger = logging.getLogger(__name__)


class CircuitBreakerTripped(RuntimeError):
    """Raised when the judge circuit breaker trips after consecutive failures."""


@dataclass(frozen=True)
class JudgeVerdict:
    """Structured verdict from the LLM judge.

    Attributes:
        correctness: Code correctness score (0-5).
        style: Code style adherence (0-5).
        test_coverage: Quality of test coverage (0-5).
        safety: Safety/security score (0-5).
        verdict: Overall pass/fail.
        issues: List of identified issues.
    """

    correctness: int = 0
    style: int = 0
    test_coverage: int = 0
    safety: int = 0
    verdict: Literal["PASS", "FAIL"] = "FAIL"
    issues: list[str] = field(default_factory=list[str])

    @property
    def average_score(self) -> float:
        """Average of all sub-scores, normalized to [0.0, 1.0]."""
        return (self.correctness + self.style + self.test_coverage + self.safety) / 20.0


_JUDGE_PROMPT = """\
You are a code review judge. Evaluate the following code changes made by an AI agent.

## Task Description
{task_description}

## Git Diff
```
{git_diff}
```

## Test Results
{test_results}

Rate the changes on these dimensions (0-5 each):
- **correctness**: Does the code correctly implement the task? Are there bugs?
- **style**: Does the code follow good style, naming conventions, and project patterns?
- **test_coverage**: Are the changes adequately tested?
- **safety**: Are there security issues, regressions, or dangerous patterns?

Respond with ONLY valid JSON in this exact format:
{{"correctness": 0, "style": 0, "test_coverage": 0, "safety": 0, "verdict": "PASS", "issues": ["issue1"]}}

verdict should be "PASS" if all scores are >= 3, otherwise "FAIL".
"""

_STRICT_SUFFIX = """

IMPORTANT: You MUST respond with ONLY a JSON object. No markdown, no explanation, no code fences.
Example: {"correctness": 4, "style": 3, "test_coverage": 4, "safety": 5, "verdict": "PASS", "issues": []}
"""

# Retry backoff schedule in seconds
_BACKOFF_SCHEDULE: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)

# Circuit breaker threshold
_CIRCUIT_BREAKER_THRESHOLD = 3


def _parse_verdict(raw: str) -> JudgeVerdict:
    """Parse a JSON string into a JudgeVerdict.

    Handles common LLM response quirks like markdown code fences.

    Args:
        raw: Raw LLM response string.

    Returns:
        Parsed JudgeVerdict.

    Raises:
        ValueError: If the response can't be parsed as valid JSON.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try to extract JSON object if surrounded by other text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]

    data = json.loads(text)

    def _clamp(val: object, low: int = 0, high: int = 5) -> int:
        if isinstance(val, int):
            return max(low, min(val, high))
        if isinstance(val, float):
            return max(low, min(int(val), high))
        return 0

    verdict_raw = data.get("verdict", "FAIL")
    verdict: Literal["PASS", "FAIL"] = "PASS" if str(verdict_raw).upper() == "PASS" else "FAIL"

    issues_raw: object = data.get("issues", [])
    issues: list[str] = [str(item) for item in cast("list[object]", issues_raw)] if isinstance(issues_raw, list) else []

    return JudgeVerdict(
        correctness=_clamp(data.get("correctness", 0)),
        style=_clamp(data.get("style", 0)),
        test_coverage=_clamp(data.get("test_coverage", 0)),
        safety=_clamp(data.get("safety", 0)),
        verdict=verdict,
        issues=issues,
    )


class EvalJudge:
    """LLM-based code quality judge with resilience patterns.

    Provides dual-attempt prompting, circuit breaker, and retry with
    exponential backoff for robust LLM-based code evaluation.

    Args:
        model: LLM model identifier.
        provider: LLM provider name.
        backoff_schedule: Retry delay sequence in seconds.
        circuit_breaker_threshold: Consecutive failures before tripping.
    """

    def __init__(
        self,
        *,
        model: str = "anthropic/claude-sonnet-4",
        provider: str = "openrouter_free",
        backoff_schedule: tuple[float, ...] = _BACKOFF_SCHEDULE,
        circuit_breaker_threshold: int = _CIRCUIT_BREAKER_THRESHOLD,
    ) -> None:
        self.model = model
        self.provider = provider
        self.backoff_schedule = backoff_schedule
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """Current consecutive failure count."""
        return self._consecutive_failures

    def reset(self) -> None:
        """Reset the circuit breaker failure counter."""
        self._consecutive_failures = 0

    def circuit_breaker(self) -> None:
        """Check circuit breaker state and raise if tripped.

        Raises:
            CircuitBreakerTripped: If consecutive failures >= threshold.
        """
        if self._consecutive_failures >= self.circuit_breaker_threshold:
            raise CircuitBreakerTripped(
                f"Judge circuit breaker tripped after {self._consecutive_failures} consecutive failures"
            )

    async def retry_with_backoff(self, attempt: int) -> None:
        """Sleep with exponential backoff for the given attempt index.

        Args:
            attempt: Zero-based attempt index into the backoff schedule.
        """
        if attempt < len(self.backoff_schedule):
            await asyncio.sleep(self.backoff_schedule[attempt])

    async def dual_attempt(self, prompt: str) -> JudgeVerdict:
        """Try standard prompt first, then strict JSON suffix on parse failure.

        Args:
            prompt: The judge prompt to send.

        Returns:
            Parsed JudgeVerdict.

        Raises:
            json.JSONDecodeError: If both attempts fail to produce valid JSON.
            ValueError: If both attempts fail to parse.
            RuntimeError: If the LLM call itself fails.
        """
        from bernstein.core.llm import call_llm

        raw = await call_llm(prompt, model=self.model, provider=self.provider, temperature=0.1)
        try:
            return _parse_verdict(raw)
        except (json.JSONDecodeError, ValueError, KeyError):
            logger.debug("Judge parse failed, retrying with strict suffix")
            raw_strict = await call_llm(
                prompt + _STRICT_SUFFIX,
                model=self.model,
                provider=self.provider,
                temperature=0.0,
            )
            return _parse_verdict(raw_strict)

    async def review_git_diff(
        self,
        *,
        task_description: str,
        git_diff: str,
        test_results: str = "",
    ) -> JudgeVerdict:
        """Judge a code change using an LLM with full resilience.

        Combines dual-attempt prompting, circuit breaker, and retry with
        exponential backoff.

        Args:
            task_description: What the agent was asked to do.
            git_diff: The git diff of the agent's changes.
            test_results: Output from running tests.

        Returns:
            JudgeVerdict with scores and issues.

        Raises:
            CircuitBreakerTripped: If consecutive failures hit the threshold.
        """
        prompt = _JUDGE_PROMPT.format(
            task_description=task_description,
            git_diff=git_diff[:8000],
            test_results=test_results[:2000],
        )

        for attempt in range(len(self.backoff_schedule)):
            self.circuit_breaker()

            try:
                verdict = await self.dual_attempt(prompt)
                self._consecutive_failures = 0
                return verdict

            except RuntimeError:
                self._consecutive_failures += 1
                logger.warning(
                    "Judge LLM call failed (attempt %d/%d)",
                    attempt + 1,
                    len(self.backoff_schedule),
                )
                if attempt < len(self.backoff_schedule) - 1:
                    await self.retry_with_backoff(attempt)

            except (json.JSONDecodeError, ValueError, KeyError):
                self._consecutive_failures += 1
                logger.warning(
                    "Judge parse failed on both attempts (attempt %d/%d)",
                    attempt + 1,
                    len(self.backoff_schedule),
                )
                if attempt < len(self.backoff_schedule) - 1:
                    await self.retry_with_backoff(attempt)

        # Final circuit breaker check after all attempts exhausted
        self.circuit_breaker()
        return JudgeVerdict(verdict="FAIL", issues=["All judge attempts exhausted"])


async def judge_code_change(
    *,
    task_description: str,
    git_diff: str,
    test_results: str = "",
    model: str = "anthropic/claude-sonnet-4",
    provider: str = "openrouter_free",
) -> JudgeVerdict:
    """Judge a code change using an LLM with resilience.

    Convenience wrapper around EvalJudge for backward compatibility.

    Args:
        task_description: What the agent was asked to do.
        git_diff: The git diff of the agent's changes.
        test_results: Output from running tests.
        model: LLM model to use for judging.
        provider: LLM provider name.

    Returns:
        JudgeVerdict with scores and issues.
    """
    judge = EvalJudge(model=model, provider=provider)
    try:
        return await judge.review_git_diff(
            task_description=task_description,
            git_diff=git_diff,
            test_results=test_results,
        )
    except CircuitBreakerTripped:
        return JudgeVerdict(verdict="FAIL", issues=["Judge circuit breaker tripped"])
