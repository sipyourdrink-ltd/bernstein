"""DAG executor for the review pipeline DSL.

The runner walks a :class:`ReviewPipeline` stage by stage:

* Stages are sequential at the top level — no diamond joins.  Stage *N+1*
  starts only once stage *N* finishes (deliberate, per spec).
* Within a stage, agents run in parallel up to ``stage.parallelism`` via an
  ``asyncio.Semaphore``.
* Stage outputs (verdict + structured findings) are forwarded to the next
  stage's prompt context using the existing :class:`BulletinBoard` —
  posted as ``finding`` messages tagged with the stage name. No new IPC.
* Each stage logs an HMAC-chained audit event when an :class:`AuditLog` is
  injected, with stage-level breakdown.

Strict-superset rule: a 1-stage / 1-agent pipeline using strategy
``any`` reproduces today's single-pass cross-model verifier output, so
existing janitor flows do not silently change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.communication.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.llm import call_llm
from bernstein.core.quality.cross_model_verifier import (
    _MAX_DIFF_CHARS,
    _MAX_TOKENS,
    _PROVIDER,
    _build_prompt,
    _get_diff,
    _parse_response,
    select_reviewer_model,
)
from bernstein.core.quality.review_pipeline.verdict import (
    AgentVerdict,
    PipelineVerdict,
    StageVerdict,
    aggregate_pipeline,
    aggregate_stage,
)

if TYPE_CHECKING:
    from bernstein.core.models import Task
    from bernstein.core.quality.review_pipeline.schema import (
        AgentSpec,
        ReviewPipeline,
        StageSpec,
    )
    from bernstein.core.security.audit import AuditLog

logger = logging.getLogger(__name__)


# Type alias for the pluggable LLM caller — the runner default uses the
# same provider/temperature settings as the cross-model verifier.
LLMCaller = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------------------
# Diff source abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffSource:
    """Describes the diff under review.

    Attributes:
        title: Short subject line shown to reviewers.
        description: Longer body / task description.
        diff: Unified diff text.  Already truncated by the caller.
        pr_number: Optional PR number for audit / display.
        owned_files: Files in scope (used by the prompt builder).
    """

    title: str
    description: str
    diff: str
    pr_number: int | None = None
    owned_files: list[str] = field(default_factory=list[str])


def diff_from_task(task: Task, worktree: Path, max_chars: int = _MAX_DIFF_CHARS) -> DiffSource:
    """Build a :class:`DiffSource` from a completed task's worktree.

    Reuses the cross-model verifier's diff helper so a 1-stage pipeline
    produces the byte-identical prompt today's verifier sends.
    """
    diff = _get_diff(worktree, task.owned_files)
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... (truncated)"
    return DiffSource(
        title=task.title,
        description=task.description,
        diff=diff,
        owned_files=list(task.owned_files),
    )


def diff_from_pr(pr_number: int, *, repo_root: Path | None = None, max_chars: int = _MAX_DIFF_CHARS) -> DiffSource:
    """Fetch a PR's diff via ``gh pr diff <N>`` and wrap it as a :class:`DiffSource`.

    Args:
        pr_number: GitHub PR number.
        repo_root: Repository working directory; defaults to ``cwd``.
        max_chars: Truncate diff at this many characters.

    Returns:
        Populated :class:`DiffSource`.

    Raises:
        RuntimeError: If ``gh`` is unavailable or the diff cannot be fetched.
    """
    import subprocess

    cwd = repo_root if repo_root is not None else Path.cwd()
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "title,body"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"gh pr view failed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed: {proc.stderr.strip()}")
    try:
        meta_obj: object = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh pr view returned invalid JSON: {exc}") from exc
    meta = cast("dict[str, object]", meta_obj if isinstance(meta_obj, dict) else {})

    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"gh pr diff failed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr diff failed: {proc.stderr.strip()}")

    diff = proc.stdout or "(no diff)"
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... (truncated)"
    return DiffSource(
        title=str(meta.get("title", f"PR #{pr_number}")),
        description=str(meta.get("body", "")),
        diff=diff,
        pr_number=pr_number,
    )


# ---------------------------------------------------------------------------
# Default LLM caller (mirrors cross_model_verifier semantics)
# ---------------------------------------------------------------------------


async def _default_llm_caller(
    *,
    prompt: str,
    model: str,
    provider: str = _PROVIDER,
    max_tokens: int = _MAX_TOKENS,
    temperature: float = 0.0,
) -> str:
    """Default LLM caller — keyword-only mirror of ``call_llm``.

    Lives here so tests can monkeypatch a single attribute without the
    indirection of patching ``bernstein.core.llm.call_llm``.
    """
    return await call_llm(
        prompt=prompt,
        model=model,
        provider=provider,
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ---------------------------------------------------------------------------
# Prompt assembly with stage-context propagation
# ---------------------------------------------------------------------------


def _format_prior_context(stage_verdicts: list[StageVerdict]) -> str:
    """Render prior stage verdicts as a markdown block for the next stage."""
    if not stage_verdicts:
        return ""
    lines: list[str] = ["", "## Prior stage findings", ""]
    for sv in stage_verdicts:
        marker = "approved" if sv.verdict == "approve" else "request_changes"
        lines.append(f"### {sv.stage} — {marker}")
        for av in sv.agents:
            head = f"- {av.role} ({av.model}): {av.verdict}"
            if av.feedback:
                head += f" — {av.feedback}"
            lines.append(head)
            for issue in av.issues:
                lines.append(f"  - issue: {issue}")
        lines.append("")
    return "\n".join(lines)


def _build_agent_prompt(
    diff_src: DiffSource,
    prior_stages: list[StageVerdict],
    *,
    extra: str = "",
) -> str:
    """Build the full prompt for one agent, with prior stage context.

    Reuses :func:`bernstein.core.quality.cross_model_verifier._build_prompt`
    so a 1-stage / 1-agent pipeline produces the same prompt verbatim
    today's single-pass verifier sends.
    """
    fake_task = _ProxyTask(
        title=diff_src.title,
        description=diff_src.description,
        owned_files=diff_src.owned_files,
    )
    base = _build_prompt(cast("Any", fake_task), diff_src.diff)
    suffix = _format_prior_context(prior_stages)
    if extra:
        suffix += "\n" + extra
    return f"{base}{suffix}" if suffix else base


@dataclass(frozen=True)
class _ProxyTask:
    """Minimal shim that quacks like ``bernstein.core.models.Task`` for the prompt builder."""

    title: str
    description: str
    owned_files: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Per-agent execution
# ---------------------------------------------------------------------------


async def _run_one_agent(
    agent: AgentSpec,
    diff_src: DiffSource,
    prior_stages: list[StageVerdict],
    *,
    llm_caller: LLMCaller,
    provider: str,
    max_tokens: int,
) -> AgentVerdict:
    """Run a single agent and return its verdict.

    On adapter / LLM failure, defaults to ``approve`` so a transient outage
    never permanently blocks merge — same behaviour as today's verifier.
    """
    # Cost-aware routing — if the spec pinned a model use it; otherwise let
    # the cascade pick one based on writer style ("low" effort → cheap).
    model = agent.model or select_reviewer_model("any", override=None)
    prompt = _build_agent_prompt(diff_src, prior_stages)

    started = time.monotonic()
    try:
        raw = await llm_caller(
            prompt=prompt,
            model=model,
            provider=provider,
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except (TimeoutError, RuntimeError, OSError) as exc:
        logger.warning(
            "review_pipeline: agent %s/%s LLM call failed: %s — defaulting to approve",
            agent.role,
            model,
            exc,
        )
        return AgentVerdict(
            role=agent.role,
            model=model,
            verdict="approve",
            feedback=f"reviewer call failed: {exc}",
            issues=[],
            confidence=0.0,
        )

    parsed = _parse_response(raw, model)
    elapsed = time.monotonic() - started
    logger.debug(
        "review_pipeline: agent %s/%s verdict=%s issues=%d (%.2fs)",
        agent.role,
        model,
        parsed.verdict,
        len(parsed.issues),
        elapsed,
    )
    return AgentVerdict(
        role=agent.role,
        model=model,
        verdict=parsed.verdict,
        feedback=parsed.feedback,
        issues=list(parsed.issues),
    )


async def _run_stage(
    stage: StageSpec,
    diff_src: DiffSource,
    prior_stages: list[StageVerdict],
    *,
    llm_caller: LLMCaller,
    provider: str,
    max_tokens: int,
    bulletin: BulletinBoard | None,
    pipeline: ReviewPipeline,
) -> StageVerdict:
    """Run a single stage's agents (parallel, capped by ``parallelism``)."""
    sem = asyncio.Semaphore(stage.parallelism)

    async def _gated(agent: AgentSpec) -> AgentVerdict:
        async with sem:
            return await _run_one_agent(
                agent,
                diff_src,
                prior_stages,
                llm_caller=llm_caller,
                provider=provider,
                max_tokens=max_tokens,
            )

    started = time.monotonic()
    agent_verdicts = await asyncio.gather(*[_gated(a) for a in stage.agents])
    elapsed = time.monotonic() - started

    sv = aggregate_stage(stage, list(agent_verdicts), pipeline)

    # Forward stage context via bulletin board — same mechanism agents use
    # for cross-agent findings.  No new IPC.
    if bulletin is not None:
        for av in sv.agents:
            bulletin.post(
                BulletinMessage(
                    agent_id=f"review_pipeline:{sv.stage}:{av.role}",
                    type="finding",
                    content=(f"[{sv.stage}/{av.role}] verdict={av.verdict} feedback={av.feedback[:280]}"),
                )
            )
        bulletin.post(
            BulletinMessage(
                agent_id=f"review_pipeline:{sv.stage}",
                type="status",
                content=sv.feedback,
            )
        )

    logger.info(
        "review_pipeline: stage %s verdict=%s (%d/%d, %.2fs)",
        sv.stage,
        sv.verdict,
        sv.approve_count,
        sv.total_count,
        elapsed,
    )
    return sv


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_pipeline(
    pipeline: ReviewPipeline,
    diff_src: DiffSource,
    *,
    llm_caller: LLMCaller | None = None,
    provider: str = _PROVIDER,
    max_tokens: int = _MAX_TOKENS,
    bulletin: BulletinBoard | None = None,
    audit_log: AuditLog | None = None,
    actor: str = "review_pipeline",
) -> PipelineVerdict:
    """Execute *pipeline* against *diff_src* and return the final verdict.

    Args:
        pipeline: Validated pipeline spec.
        diff_src: Diff under review (task or PR).
        llm_caller: Pluggable LLM caller; defaults to :func:`call_llm` with
            the same provider / temperature as the cross-model verifier.
        provider: LLM provider key.
        max_tokens: Per-agent response cap.
        bulletin: Bulletin board used for stage-to-stage context.  When
            ``None``, a private board is created so context still flows
            forward; the caller can pass an existing one to merge with the
            wider orchestrator board.
        audit_log: When supplied, each stage and the final verdict are
            written as HMAC-chained events.
        actor: Audit ``actor`` field — defaults to ``review_pipeline``.

    Returns:
        :class:`PipelineVerdict`.
    """
    caller = llm_caller or _default_llm_caller
    board = bulletin if bulletin is not None else BulletinBoard()
    pipeline_started = time.monotonic()
    pr_resource = f"pr-{diff_src.pr_number}" if diff_src.pr_number is not None else f"task:{diff_src.title[:60]}"

    if audit_log is not None:
        with contextlib.suppress(OSError):
            audit_log.log(
                event_type="review_pipeline.start",
                actor=actor,
                resource_type="review_pipeline",
                resource_id=pr_resource,
                details={
                    "pipeline_name": pipeline.name,
                    "stages": [s.name for s in pipeline.stages],
                    "block_on_fail": pipeline.block_on_fail,
                },
            )

    stage_verdicts: list[StageVerdict] = []
    for stage in pipeline.stages:
        sv = await _run_stage(
            stage,
            diff_src,
            stage_verdicts,
            llm_caller=caller,
            provider=provider,
            max_tokens=max_tokens,
            bulletin=board,
            pipeline=pipeline,
        )
        stage_verdicts.append(sv)
        if audit_log is not None:
            with contextlib.suppress(OSError):
                audit_log.log(
                    event_type="review_pipeline.stage",
                    actor=actor,
                    resource_type="review_pipeline_stage",
                    resource_id=f"{pr_resource}:{sv.stage}",
                    details={
                        "stage": sv.stage,
                        "verdict": sv.verdict,
                        "approve_count": sv.approve_count,
                        "total_count": sv.total_count,
                        "pass_score": sv.pass_score,
                        "agents": [
                            {
                                "role": a.role,
                                "model": a.model,
                                "verdict": a.verdict,
                                "issues": a.issues,
                            }
                            for a in sv.agents
                        ],
                    },
                )

    final = aggregate_pipeline(pipeline, stage_verdicts)
    if audit_log is not None:
        with contextlib.suppress(OSError):
            audit_log.log(
                event_type="review_pipeline.complete",
                actor=actor,
                resource_type="review_pipeline",
                resource_id=pr_resource,
                details={
                    "pipeline_name": pipeline.name,
                    "verdict": final.verdict,
                    "pass_score": final.pass_score,
                    "stages_passed": sum(1 for s in stage_verdicts if s.verdict == "approve"),
                    "stages_total": len(stage_verdicts),
                    "elapsed_sec": round(time.monotonic() - pipeline_started, 3),
                    "block_on_fail": final.block_on_fail,
                },
            )
    logger.info(
        "review_pipeline: complete verdict=%s score=%.2f (%d stages, %.2fs)",
        final.verdict,
        final.pass_score,
        len(stage_verdicts),
        time.monotonic() - pipeline_started,
    )
    return final


def run_pipeline_sync(
    pipeline: ReviewPipeline,
    diff_src: DiffSource,
    **kwargs: Any,
) -> PipelineVerdict:
    """Synchronous wrapper for :func:`run_pipeline`.

    Safe to call from sync orchestrator code (no running loop).
    """
    return asyncio.run(run_pipeline(pipeline, diff_src, **kwargs))


# ---------------------------------------------------------------------------
# Janitor integration
# ---------------------------------------------------------------------------


def should_block_merge(verdict: PipelineVerdict) -> bool:
    """Return True when the pipeline should block the janitor merge gate.

    Mirrors the cross-model verifier's ``block_on_issues`` behaviour:
    ``request_changes`` + ``block_on_fail`` → block.
    """
    return verdict.verdict == "request_changes" and verdict.block_on_fail


def to_cross_model_verdict(verdict: PipelineVerdict) -> Any:
    """Adapt a :class:`PipelineVerdict` to the legacy ``CrossModelVerdict``.

    Lets the orchestrator's existing
    :func:`bernstein.core.tasks.task_lifecycle._run_cross_model_check`
    consume a pipeline verdict without conditional branching.

    Imported lazily to avoid circular imports.
    """
    from bernstein.core.quality.cross_model_verifier import CrossModelVerdict

    issues = verdict.issues
    feedback = verdict.feedback
    reviewer_model = ", ".join(verdict.reviewer_models) or "review_pipeline"
    return CrossModelVerdict(
        verdict=verdict.verdict,
        feedback=feedback,
        issues=issues,
        reviewer_model=reviewer_model,
    )
