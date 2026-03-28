"""Cross-model verification: route completed task diffs to a different model for review.

Writer != reviewer. After an agent finishes, the git diff is sent to a different
model with a focused code-review prompt. Uses a cheap model; configurable per-task
and globally via OrchestratorConfig.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from bernstein.core.llm import call_llm

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# Cost-control constants
_MAX_DIFF_CHARS = 12_000
_MAX_TOKENS = 512
_PROVIDER = "openrouter"
_DEFAULT_REVIEWER = "google/gemini-flash-1.5"

# Model family → cheap reviewer from a different provider
_WRITER_TO_REVIEWER: dict[str, str] = {
    "claude": "google/gemini-flash-1.5",
    "gemini": "anthropic/claude-haiku-3-5",
    "gpt": "google/gemini-flash-1.5",
    "codex": "anthropic/claude-haiku-3-5",
    "qwen": "anthropic/claude-haiku-3-5",
}

_REVIEW_PROMPT_TEMPLATE = """\
You are a code reviewer. A different AI agent wrote the code below. Review it \
for correctness and security only.

## Task
**Title:** {title}
**Description:**
{description}

## Diff
```diff
{diff}
```

## Instructions
Focus on:
1. Correctness — does the diff accomplish what the task description asks?
2. Security — any obvious vulnerabilities (injection, hardcoded secrets, \
insecure defaults, missing auth checks)?
3. Bugs — off-by-one errors, missing error handling for likely failures.

Do NOT flag style issues, missing docs, or formatting.

Output a JSON object with exactly these fields:
{{
  "verdict": "approve | request_changes",
  "feedback": "One or two sentence summary",
  "issues": ["Specific issue 1", "Specific issue 2"]
}}

Output ONLY the JSON. No markdown fences. No extra text.
"""


@dataclass(frozen=True)
class CrossModelVerifierConfig:
    """Configuration for cross-model code review.

    Attributes:
        enabled: Master on/off switch.
        reviewer_model: OpenRouter model for review (None = auto-select based on writer).
        provider: LLM provider key passed to call_llm.
        max_diff_chars: Truncate diff at this length for cost control.
        max_tokens: Token cap for the reviewer response.
        block_on_issues: When True, a ``request_changes`` verdict prevents merge
            and creates a fix task.  When False, findings are logged only.
    """

    enabled: bool = False
    reviewer_model: str | None = None
    provider: str = _PROVIDER
    max_diff_chars: int = _MAX_DIFF_CHARS
    max_tokens: int = _MAX_TOKENS
    block_on_issues: bool = True


@dataclass(frozen=True)
class CrossModelVerdict:
    """Result of a cross-model code review.

    Attributes:
        verdict: "approve" or "request_changes".
        feedback: One-line summary from the reviewer.
        issues: Specific issues found (empty when approved).
        reviewer_model: Model that performed the review.
    """

    verdict: Literal["approve", "request_changes"]
    feedback: str
    issues: list[str] = field(default_factory=list[str])
    reviewer_model: str = ""


def select_reviewer_model(writer_model: str, override: str | None = None) -> str:
    """Choose a reviewer that differs from the writer.

    Args:
        writer_model: Model identifier used by the writing agent.
        override: Explicit model override (per-task or global config).

    Returns:
        OpenRouter model identifier for the reviewer.
    """
    if override:
        return override
    lower = writer_model.lower()
    for prefix, reviewer in _WRITER_TO_REVIEWER.items():
        if prefix in lower:
            return reviewer
    return _DEFAULT_REVIEWER


def _get_diff(worktree_path: Path, owned_files: list[str]) -> str:
    """Get the git diff from a worktree, truncation handled by caller."""
    try:
        cmd = ["git", "diff", "HEAD~1", "--"]
        if owned_files:
            cmd.extend(owned_files)
        result = subprocess.run(cmd, cwd=worktree_path, capture_output=True, text=True, timeout=30)
        diff = result.stdout.strip()
        if not diff:
            # Fallback: uncommitted staged changes
            result = subprocess.run(
                ["git", "diff", "HEAD", "--"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            diff = result.stdout.strip()
        return diff or "(no diff available)"
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("cross_model_verifier: git diff failed: %s", exc)
        return "(failed to get git diff)"


def _build_prompt(task: Task, diff: str) -> str:
    return _REVIEW_PROMPT_TEMPLATE.format(
        title=task.title,
        description=task.description[:2000],
        diff=diff,
    )


def _parse_response(raw: str, reviewer_model: str) -> CrossModelVerdict:
    """Parse the reviewer LLM response into a CrossModelVerdict.

    Defaults to "approve" when the response cannot be parsed, so a reviewer
    outage never blocks work permanently.
    """
    text = raw.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()

    data: dict[str, object] = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(text[start:end])

    if not data:
        logger.warning(
            "cross_model_verifier: unparseable response — defaulting to approve: %.200s",
            text,
        )
        return CrossModelVerdict(
            verdict="approve",
            feedback="Reviewer returned unparseable response — defaulting to approve",
            issues=[],
            reviewer_model=reviewer_model,
        )

    raw_verdict = str(data.get("verdict", "approve")).lower()
    verdict: Literal["approve", "request_changes"] = (
        "request_changes" if raw_verdict == "request_changes" else "approve"
    )
    issues_raw: object = data.get("issues", [])
    issues: list[str] = []
    if isinstance(issues_raw, list):
        issues = [str(item) for item in cast("list[object]", issues_raw)]

    return CrossModelVerdict(
        verdict=verdict,
        feedback=str(data.get("feedback", "")),
        issues=issues,
        reviewer_model=reviewer_model,
    )


async def verify_with_cross_model(
    task: Task,
    worktree_path: Path,
    writer_model: str,
    config: CrossModelVerifierConfig,
) -> CrossModelVerdict:
    """Run a cross-model review on a completed task's diff.

    Selects a reviewer different from the writer, fetches the diff, and
    calls the reviewer LLM.  On LLM failure, returns an "approve" verdict so
    a transient outage never blocks the pipeline permanently.

    Args:
        task: The completed task.
        worktree_path: Path to the agent's git worktree (or main workdir).
        writer_model: Model that wrote the code — used to select a different reviewer.
        config: Verifier configuration.

    Returns:
        CrossModelVerdict with approve/request_changes decision.
    """
    reviewer = select_reviewer_model(writer_model, override=config.reviewer_model)

    diff = _get_diff(worktree_path, task.owned_files)
    if len(diff) > config.max_diff_chars:
        diff = diff[: config.max_diff_chars] + "\n... (truncated)"

    prompt = _build_prompt(task, diff)
    logger.info(
        "cross_model_verifier: task=%s writer=%s reviewer=%s diff_chars=%d",
        task.id,
        writer_model,
        reviewer,
        len(diff),
    )

    try:
        raw = await call_llm(
            prompt=prompt,
            model=reviewer,
            provider=config.provider,
            max_tokens=config.max_tokens,
            temperature=0.0,
        )
    except RuntimeError as exc:
        logger.warning(
            "cross_model_verifier: LLM call failed for task %s: %s — defaulting to approve",
            task.id,
            exc,
        )
        return CrossModelVerdict(
            verdict="approve",
            feedback=f"Reviewer call failed: {exc}",
            issues=[],
            reviewer_model=reviewer,
        )

    verdict = _parse_response(raw, reviewer)
    logger.info(
        "cross_model_verifier: task=%s verdict=%s issues=%d",
        task.id,
        verdict.verdict,
        len(verdict.issues),
    )
    return verdict


def run_cross_model_verification_sync(
    task: Task,
    worktree_path: Path,
    writer_model: str,
    config: CrossModelVerifierConfig,
) -> CrossModelVerdict:
    """Synchronous wrapper for verify_with_cross_model.

    Runs the async verifier in a new event loop.  Safe to call from sync
    orchestrator code (no running loop in the orchestrator's main thread).

    Args:
        task: Completed task.
        worktree_path: Worktree path for git diff.
        writer_model: Model that wrote the code.
        config: Verifier configuration.

    Returns:
        CrossModelVerdict.
    """
    return asyncio.run(verify_with_cross_model(task, worktree_path, writer_model, config))
