"""Security-layer hook for the interactive approval queue.

This module owns the "miss the allow-list" branch: it checks the always
allow engine, and if no rule matches, reads ``bernstein.yaml`` to decide
whether to enqueue an interactive approval. A blocking call
(:func:`gate_tool_call`) and an async variant (:func:`await_tool_call`)
are provided so both synchronous and coroutine-based callers can plug in
without extra glue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from bernstein.core.approval.models import (
    ApprovalDecision,
    ApprovalTimeoutError,
    PendingApproval,
    ResolvedApproval,
)
from bernstein.core.approval.queue import (
    DEFAULT_TTL_SECONDS,
    ApprovalQueue,
    get_default_queue,
    promote_to_always_allow,
)
from bernstein.core.security.always_allow import (
    AlwaysAllowEngine,
    load_always_allow_rules,
)
from bernstein.core.security.auto_approve import (
    ApprovalResult as ClassifierResult,
)
from bernstein.core.security.auto_approve import (
    Decision,
    classify_tool_call,
)
from bernstein.core.security.guardrails import check_always_allow_tool
from bernstein.core.security.permission_policy import (
    PermissionProfile,
    PolicyChecker,
    ToolCall,
    resolve_profile,
)
from bernstein.core.security.policy_engine import DecisionType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalConfig:
    """``approvals:`` section of ``bernstein.yaml``.

    Attributes:
        interactive: When ``True`` the gate queues missed calls for
            operator review. When ``False`` the gate is a no-op and the
            caller falls back to whatever legacy ask-mode behaviour was
            configured.
        timeout_seconds: How long the gate blocks waiting for a
            decision. Defaults to ten minutes.
        smart_auto_approve: When ``True`` a classifier APPROVE verdict
            short-circuits the queue and auto-approves the call. The
            default is ``False`` (fail-closed): a classifier APPROVE only
            suppresses an interactive prompt when the operator has opted
            in. The classifier deny-list and write-tool ASK rules apply
            regardless of this flag - this flag only controls whether a
            *safe* verdict skips human review, never whether a *risky*
            one is blocked.
    """

    interactive: bool = False
    timeout_seconds: int = DEFAULT_TTL_SECONDS
    smart_auto_approve: bool = False


def load_approval_config(workdir: Path | None = None) -> ApprovalConfig:
    """Read the ``approvals:`` block from ``bernstein.yaml``.

    Missing fields fall back to safe defaults (``interactive=False`` so
    existing headless runs are unaffected).
    """
    root = workdir if workdir is not None else Path.cwd()
    path = root / "bernstein.yaml"
    if not path.exists():
        return ApprovalConfig()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not read bernstein.yaml for approval config: %s", exc)
        return ApprovalConfig()
    if not isinstance(raw, dict):
        return ApprovalConfig()
    raw_section: object = cast("dict[str, Any]", raw).get("approvals")
    if not isinstance(raw_section, dict):
        return ApprovalConfig()
    section = cast("dict[str, Any]", raw_section)
    interactive = bool(section.get("interactive", False))
    timeout = int(section.get("timeout_seconds", DEFAULT_TTL_SECONDS))
    smart_auto_approve = bool(section.get("smart_auto_approve", False))
    return ApprovalConfig(
        interactive=interactive,
        timeout_seconds=max(1, timeout),
        smart_auto_approve=smart_auto_approve,
    )


def _always_allow_hit(
    tool_name: str,
    tool_args: dict[str, Any],
    engine: AlwaysAllowEngine | None,
) -> bool:
    """Return ``True`` when *tool_name* + *tool_args* hits the allow list."""
    if engine is None or not engine.rules:
        return False
    return check_always_allow_tool(tool_name, tool_args, engine).matched


def _policy_reject(
    *,
    session_id: str,
    agent_role: str,
    tool_name: str,
    tool_args: dict[str, Any],
    workdir: Path | None,
    profile: PermissionProfile | None = None,
) -> ResolvedApproval | None:
    """Run the per-tool permission policy ahead of the approval queue.

    Returns a synthetic :class:`ResolvedApproval` carrying
    :class:`ApprovalDecision.REJECT` when the active profile denies the
    invocation. Returns ``None`` when there is no profile or when the
    profile allows the call (i.e. continue with the legacy approval
    flow). Denials are logged to the audit trail by
    :class:`PolicyChecker`.
    """
    effective = profile if profile is not None else resolve_profile(workdir=workdir)
    if effective is None:
        return None

    checker = PolicyChecker(effective)
    call = ToolCall(
        tool=tool_name,
        path=tool_args.get("path") or tool_args.get("file_path"),
        host=tool_args.get("host") or tool_args.get("url_host"),
        shell_cmd=tool_args.get("command") or tool_args.get("shell_cmd"),
        session_id=session_id,
        actor=agent_role,
        extra={"tool_args_keys": sorted(tool_args.keys())},
    )
    decision = checker.check_and_record(call, workdir=workdir)
    if decision.type == DecisionType.DENY:
        return ResolvedApproval(
            approval_id=f"policy-deny:{effective.name}",
            decision=ApprovalDecision.REJECT,
            reason=decision.reason,
        )
    return None


def _classify(tool_name: str, tool_args: dict[str, Any]) -> ClassifierResult:
    """Run the smart auto-approve classifier, fail-closed on any error.

    Any unexpected exception inside the classifier is mapped to an
    :class:`Decision.ASK` result so an errored classifier can never
    silently auto-approve a call. The deny-list and write-tool rules are
    enforced by :func:`classify_tool_call`; this wrapper only guarantees
    the gate degrades to "ask the human" rather than to "allow" when the
    classifier itself misbehaves.
    """
    try:
        return classify_tool_call(tool_name, tool_args)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Auto-approve classifier raised for tool %s: %s -- failing closed to ASK",
            tool_name,
            exc,
        )
        return ClassifierResult(Decision.ASK, f"classifier error: {exc}")


def _record_classifier_decision(
    *,
    classification: ClassifierResult,
    session_id: str,
    agent_role: str,
    tool_name: str,
    tool_args: dict[str, Any],
    workdir: Path | None,
) -> None:
    """Append the classifier verdict to the HMAC-chained audit log.

    Every APPROVE/DENY/ASK verdict that the gate acts on is recorded as a
    signed, replayable ``auto_approve_decision`` event so an auditor can
    prove, after the fact, exactly which calls were auto-approved or
    rejected and which pattern drove the decision. Audit persistence is
    best-effort: a write failure is logged but never blocks a deny (the
    REJECT is returned to the caller regardless).
    """
    # Imported lazily so the approval gate does not pull in the audit
    # subsystem (and its key material) unless a decision is actually
    # being recorded.
    from bernstein.core.security.audit import AuditLog

    root = workdir if workdir is not None else Path.cwd()
    cmd = str(tool_args.get("command") or tool_args.get("shell_cmd") or "")
    details: dict[str, Any] = {
        "decision": classification.decision.value,
        "tool": tool_name,
        "reason": classification.reason,
        "matched_pattern": classification.matched_pattern,
        "session_id": session_id,
        "agent_role": agent_role,
    }
    if cmd:
        details["command"] = cmd
    try:
        log = AuditLog(audit_dir=root / ".sdd" / "audit")
        log.log(
            event_type="auto_approve_decision",
            actor=agent_role or "agent",
            resource_type="tool_call",
            resource_id=tool_name,
            details=details,
        )
    except Exception as exc:  # pragma: no cover - filesystem/permission
        logger.warning(
            "Could not record auto-approve decision to audit log: %s",
            exc,
        )


def _smart_classifier_decision(
    *,
    session_id: str,
    agent_role: str,
    tool_name: str,
    tool_args: dict[str, Any],
    workdir: Path | None,
    smart_auto_approve: bool,
) -> ResolvedApproval | None:
    """Apply the smart classifier ahead of the interactive queue.

    Precedence is deny-wins:

    * ``DENY``  -> reject immediately and record the verdict, regardless
      of ``smart_auto_approve`` or interactive mode.
    * ``APPROVE`` -> auto-approve (and record) only when the operator has
      opted in via ``smart_auto_approve``; otherwise fall through so the
      call is still surfaced for review.
    * ``ASK`` / anything else -> return ``None`` so the existing approval
      flow handles it. Uncertainty never auto-approves (fail-closed).
    """
    classification = _classify(tool_name, tool_args)

    if classification.decision is Decision.DENY:
        _record_classifier_decision(
            classification=classification,
            session_id=session_id,
            agent_role=agent_role,
            tool_name=tool_name,
            tool_args=tool_args,
            workdir=workdir,
        )
        return ResolvedApproval(
            approval_id=f"auto-approve-deny:{tool_name}",
            decision=ApprovalDecision.REJECT,
            reason=classification.reason,
        )

    if classification.decision is Decision.APPROVE and smart_auto_approve:
        _record_classifier_decision(
            classification=classification,
            session_id=session_id,
            agent_role=agent_role,
            tool_name=tool_name,
            tool_args=tool_args,
            workdir=workdir,
        )
        return ResolvedApproval(
            approval_id=f"auto-approve-allow:{tool_name}",
            decision=ApprovalDecision.ALLOW,
            reason=classification.reason,
        )

    # ASK, or APPROVE without opt-in: defer to the existing approval flow.
    return None


async def await_tool_call(
    *,
    session_id: str,
    agent_role: str,
    tool_name: str,
    tool_args: dict[str, Any],
    workdir: Path | None = None,
    queue: ApprovalQueue | None = None,
    engine: AlwaysAllowEngine | None = None,
    config: ApprovalConfig | None = None,
) -> ResolvedApproval | None:
    """Async variant of :func:`gate_tool_call`.

    Returns ``None`` when the gate is disabled or when the allow-list
    already permits the invocation. When the caller should block the
    tool call, the returned :class:`ResolvedApproval` carries the
    operator's decision.

    Raises:
        ApprovalTimeoutError: When the TTL expires without an operator
            decision. The caller MUST treat this as a rejection.
    """
    # Per-tool permission policy runs first - it must apply regardless
    # of whether the interactive approval queue is on, so a fail-closed
    # profile cannot be bypassed by disabling approvals.
    policy_decision = _policy_reject(
        session_id=session_id,
        agent_role=agent_role,
        tool_name=tool_name,
        tool_args=tool_args,
        workdir=workdir,
    )
    if policy_decision is not None:
        return policy_decision

    cfg = config if config is not None else load_approval_config(workdir)

    # Smart auto-approve classifier. The deny-list runs unconditionally
    # (deny wins, even headless), so a deny-listed command is rejected and
    # audited regardless of interactive mode or the opt-in flag. A safe
    # (APPROVE) verdict only short-circuits the queue when the operator
    # opted in; uncertainty (ASK) always falls through to human review.
    classifier_decision = _smart_classifier_decision(
        session_id=session_id,
        agent_role=agent_role,
        tool_name=tool_name,
        tool_args=tool_args,
        workdir=workdir,
        smart_auto_approve=cfg.smart_auto_approve,
    )
    if classifier_decision is not None:
        return classifier_decision

    if not cfg.interactive:
        return None

    root = workdir if workdir is not None else Path.cwd()
    allow_engine = engine if engine is not None else load_always_allow_rules(root, strict=False)
    if _always_allow_hit(tool_name, tool_args, allow_engine):
        return None

    q = queue if queue is not None else get_default_queue(root / ".sdd" / "runtime" / "approvals")
    approval = PendingApproval(
        session_id=session_id,
        agent_role=agent_role,
        tool_name=tool_name,
        tool_args=tool_args.copy(),
        ttl_seconds=cfg.timeout_seconds,
    )
    q.push(approval)
    logger.info(
        "Approval gate: tool=%s role=%s session=%s queued as %s",
        tool_name,
        agent_role,
        session_id,
        approval.id,
    )
    resolution = await q.wait_for(approval.id, timeout_seconds=cfg.timeout_seconds)
    if resolution.decision is ApprovalDecision.ALWAYS:
        try:
            promote_to_always_allow(approval, workdir=root)
        except OSError as exc:
            logger.warning("Could not promote approval %s to always-allow: %s", approval.id, exc)
    return resolution


def gate_tool_call(
    *,
    session_id: str,
    agent_role: str,
    tool_name: str,
    tool_args: dict[str, Any],
    workdir: Path | None = None,
    queue: ApprovalQueue | None = None,
    engine: AlwaysAllowEngine | None = None,
    config: ApprovalConfig | None = None,
) -> ResolvedApproval | None:
    """Blocking entry point used from sync guardrail code.

    Returns ``None`` when the gate is disabled or when the allow-list
    already permits the invocation; otherwise a :class:`ResolvedApproval`
    with the operator's decision.

    Raises:
        ApprovalTimeoutError: When the TTL expires without a decision.
    """
    try:
        return asyncio.run(
            await_tool_call(
                session_id=session_id,
                agent_role=agent_role,
                tool_name=tool_name,
                tool_args=tool_args,
                workdir=workdir,
                queue=queue,
                engine=engine,
                config=config,
            )
        )
    except RuntimeError as exc:
        # Fallback: when called from inside an already-running loop we
        # cannot spin up a new one, so busy-wait on the queue state in
        # an explicit executor to preserve sync semantics.
        if "cannot be called from a running event loop" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                await_tool_call(
                    session_id=session_id,
                    agent_role=agent_role,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    workdir=workdir,
                    queue=queue,
                    engine=engine,
                    config=config,
                )
            )
        finally:
            loop.close()


__all__ = [
    "ApprovalConfig",
    "ApprovalTimeoutError",
    "await_tool_call",
    "gate_tool_call",
    "load_approval_config",
]
