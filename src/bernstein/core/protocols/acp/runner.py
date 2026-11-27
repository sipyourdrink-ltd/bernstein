"""Embedded workspace orchestrator runner and approval-gate bridge for ACP v1.

Enables ``bernstein acp serve --stdio`` to execute governed workspace turns
in-process without requiring an external ``--server-url`` task server:

1. Persists tasks in ``<cwd>/.sdd/runtime/tasks.jsonl`` (:class:`TaskStore`)
   with authoritative lifecycle transitions (``open`` -> ``claimed`` ->
   ``done`` / ``failed`` / ``cancelled``) flushed to disk, and records every
   step in ``<cwd>/.sdd/runs/<run_id>/journal.jsonl`` (:class:`EventJournal`).
2. Streams live ``session/update`` notifications projected from the canonical
   :class:`EventJournal` via :class:`JournalACPProjector`.
3. Bridges :func:`bernstein.core.approval.gate.await_tool_call` (:class:`ApprovalQueue`)
   onto outbound ACP v1 ``session/request_permission`` RPCs, attributing decisions
   with a structured :class:`ApprovalPrincipal` and single-use nonce, and draining
   pending approvals cleanly on ``session/cancel`` or transport shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.approval.gate import ApprovalConfig, ApprovalTimeoutError, await_tool_call, load_approval_config
from bernstein.core.approval.models import (
    ApprovalDecision,
    ApprovalNonceExpired,
    ApprovalPrincipal,
    PendingApproval,
    PrincipalKind,
    internal_principal,
)
from bernstein.core.approval.queue import ApprovalQueue
from bernstein.core.config.platform_compat import reap_process_group
from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    PromptResult,
    PromptRunOutcome,
    extract_prompt_text,
)
from bernstein.core.protocols.acp.projection import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    JournalACPProjector,
    infer_tool_kind,
)
from bernstein.core.protocols.acp.schema import INTERNAL_ERROR, ACPSchemaError
from bernstein.core.replay.journal import EventJournal
from bernstein.core.tasks.models import ModelConfig
from bernstein.core.tasks.task_store_core import TaskStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from bernstein.adapters.base import SpawnResult
    from bernstein.core.protocols.acp.session import ACPSession

logger = logging.getLogger(__name__)

_PROBE_CLI_ADAPTERS: tuple[str, ...] = ("claude", "codex", "gemini", "agy", "qwen", "aider")
_NON_CLI_RUNTIME_ADAPTERS: frozenset[str] = frozenset({"generic", "openai_agents", "python_runtime"})
_MAX_DIFF_SNAPSHOT_BYTES: int = 2 * 1024 * 1024
_FILE_HINT_RE = re.compile(r"\b([A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8})\b")
_CANCEL_PRINCIPAL = internal_principal("acp-runner/session-cancel")


@dataclass
class _ActiveWorker:
    """Live spawned worker process and its owning adapter for group reaping."""

    adapter: Any
    proc: subprocess.Popen[bytes] | None
    pid: int


def _terminate_and_reap(proc: subprocess.Popen[bytes] | None) -> None:
    """Terminate *proc* and guarantee it has exited before returning."""
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=0.5)


def _reap_worker(worker: _ActiveWorker | None) -> None:
    """Reap an entire worker process group (including adapter wrappers) and wait on Popen."""
    if worker is None:
        return
    if worker.pid > 0:
        kill_fn = getattr(worker.adapter, "kill", None)
        if callable(kill_fn):
            with contextlib.suppress(Exception):
                kill_fn(worker.pid)
        with contextlib.suppress(Exception):
            reap_process_group(worker.pid)
    _terminate_and_reap(worker.proc)


def _read_utf8_text_if_valid(path: Path | None) -> str | None:
    """Return UTF-8 text content of *path*, or ``None`` for missing/oversized/binary/non-UTF-8 files."""
    if path is None or not path.is_file():
        return None
    try:
        if path.stat().st_size > _MAX_DIFF_SNAPSHOT_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _which_in_env(binary: str, env: Mapping[str, str] | None) -> str | None:
    if not binary:
        return None
    if env is not None:
        return shutil.which(binary, path=env.get("PATH", ""))
    return shutil.which(binary)


def adapter_binary_name(adapter_name: str) -> str:
    """Resolve the CLI executable name on ``PATH`` for a registered adapter key."""
    from bernstein.adapters.capability_profile import profile_binary_for
    from bernstein.adapters.report import _BINARY_OVERRIDES  # pyright: ignore[reportPrivateUsage]

    key = adapter_name.strip().lower()
    if key == "generic":
        return "generic-cli"
    override = _BINARY_OVERRIDES.get(key)
    if override is not None:
        return override
    return profile_binary_for(key) or key


def find_installed_cli_adapters(env: Mapping[str, str] | None = None) -> list[str]:
    """Return selectable CLI-backed adapter names whose binaries resolve on ``PATH``."""
    from bernstein.adapters.registry import selectable_adapter_names

    selectable = selectable_adapter_names()
    ordered_candidates: list[str] = [
        *[name for name in _PROBE_CLI_ADAPTERS if name in selectable],
        *sorted(selectable - set(_PROBE_CLI_ADAPTERS) - _NON_CLI_RUNTIME_ADAPTERS),
    ]
    found: list[str] = []
    for name in ordered_candidates:
        binary = adapter_binary_name(name)
        if binary and _which_in_env(binary, env) is not None:
            found.append(name)
    return found


def is_adapter_usable(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return ``True`` when the pinned adapter (or at least one selectable CLI adapter) is usable."""
    from bernstein.adapters.registry import selectable_adapter_names

    eff_env = env if env is not None else os.environ
    pinned = (explicit or eff_env.get("BERNSTEIN_DEFAULT_CLI") or "").strip().lower()
    if pinned == "mock":
        return True
    if pinned and pinned != "auto":
        if pinned not in selectable_adapter_names():
            return False
        if pinned in {"openai_agents", "python_runtime"}:
            return (_which_in_env("python3", eff_env) or _which_in_env("python", eff_env)) is not None
        binary = adapter_binary_name(pinned)
        return bool(binary) and _which_in_env(binary, eff_env) is not None
    return bool(find_installed_cli_adapters(eff_env))


@dataclass(slots=True)
class _ACPTaskRequest:
    """Minimal ``TaskCreateRequest``-compatible payload for embedded ACP turns."""

    title: str
    description: str
    role: str
    priority: int = 1
    scope: str = "small"
    complexity: str = "medium"
    task_type: str = "standard"
    estimated_minutes: int | None = None
    parent_task_id: str | None = None
    depends_on_repo: str | None = None
    tenant_id: str = "default"
    cell_id: str | None = None
    repo: str | None = None
    model: str | None = None
    effort: str | None = None
    batch_eligible: bool = False
    approval_required: bool = False
    eu_ai_act_risk: str = "minimal"
    risk_level: str = "low"
    parent_session_id: str | None = None
    parent_context: str | None = None
    retry_count: int | None = None
    max_retries: int | None = None
    retry_delay_s: float | None = None
    terminal_reason: str | None = None
    max_output_tokens: int | None = None

    @property
    def depends_on(self) -> tuple[str, ...]:
        return ()

    @property
    def owned_files(self) -> tuple[str, ...]:
        return ()

    @property
    def upgrade_details(self) -> None:
        return None

    @property
    def completion_signals(self) -> tuple[object, ...]:
        return ()

    @property
    def slack_context(self) -> None:
        return None

    @property
    def meta_messages(self) -> tuple[str, ...] | None:
        return None


class _NotifyingApprovalQueue(ApprovalQueue):
    """``ApprovalQueue`` subclass signalling an ``asyncio.Event`` on in-process ``push``."""

    def __init__(self, base_dir: Path, *, workdir: Path, wake_event: asyncio.Event) -> None:
        super().__init__(base_dir=base_dir, workdir=workdir)
        self._wake_event = wake_event

    def push(self, approval: PendingApproval) -> PendingApproval:
        pushed = super().push(approval)
        self._wake_event.set()
        return pushed

    def refresh_from_disk(self) -> None:
        """Reload any out-of-process pending approvals written to disk."""
        self._load_from_disk()


class ACPApprovalGateBridge:
    """Forwards pending :class:`ApprovalQueue` items to ACP ``session/request_permission``."""

    def __init__(
        self,
        *,
        workdir: Path,
        session: ACPSession,
        registry: ACPHandlerRegistry,
    ) -> None:
        self._workdir = workdir
        self._session = session
        self._registry = registry
        self._wake_event = asyncio.Event()
        self.queue = _NotifyingApprovalQueue(
            workdir / ".sdd" / "runtime" / "approvals",
            workdir=workdir,
            wake_event=self._wake_event,
        )
        self._handled_ids: set[str] = set()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start background watcher forwarding queued approvals to the ACP client."""
        self._task = asyncio.create_task(self._watch_loop())

    def reject_all_pending(self, reason: str) -> None:
        """Reject any open pending approvals for this session on cancel/shutdown."""
        self.queue.refresh_from_disk()
        for pending in self.queue.list_pending(session_id=self._session.session_id):
            with contextlib.suppress(KeyError, ApprovalNonceExpired):
                self.queue.resolve(
                    pending.id,
                    ApprovalDecision.REJECT,
                    principal=_CANCEL_PRINCIPAL,
                    reason=reason,
                    source="acp-cancel",
                )

    async def stop(self) -> None:
        """Stop background watcher cleanly, interrupting any in-flight permission RPC."""
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            self.queue.refresh_from_disk()
            for pending in self.queue.list_pending(session_id=self._session.session_id):
                if pending.id in self._handled_ids:
                    continue
                self._handled_ids.add(pending.id)
                await self._forward_one(pending)

            self._wake_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout=0.05)

    async def _forward_one(self, pending: PendingApproval) -> None:
        operand = str(pending.tool_args.get("path") or pending.tool_args.get("file_path") or "")
        cmd = str(pending.tool_args.get("command") or "")
        detail = f"{pending.tool_name}: {operand or cmd}".strip(": ")
        locations = [{"path": operand}] if operand else None
        decision_str, selected_kind = await self._registry.ask_permission_outcome(
            self._session.session_id,
            pending.tool_name,
            detail,
            tool_call_id=f"call-{pending.id[:12]}",
            kind=infer_tool_kind(pending.tool_name),
            locations=locations,
        )
        if selected_kind == "allow_always":
            mapped = ApprovalDecision.ALWAYS
        elif decision_str == "approved":
            mapped = ApprovalDecision.ALLOW
        else:
            mapped = ApprovalDecision.REJECT

        principal = ApprovalPrincipal(
            identifier=f"acp-user:{self._registry.client_name_for_peer(self._session.peer)}",
            auth_method="acp-v1",
            kind=PrincipalKind.HUMAN,
            grant=self._session.session_id,
        )
        with contextlib.suppress(KeyError, ApprovalNonceExpired):
            self.queue.resolve(
                pending.id,
                mapped,
                principal=principal,
                reason=f"ACP permission outcome: {selected_kind}",
                nonce=pending.nonce,
                source="acp",
                channel="acp-stdio",
            )


def resolve_embedded_adapter_name(explicit: str | None = None) -> str:
    """Resolve CLI adapter name for embedded execution."""
    candidate = (explicit or os.environ.get("BERNSTEIN_DEFAULT_CLI") or "").strip().lower()
    if candidate and candidate != "auto":
        return candidate
    installed = find_installed_cli_adapters()
    if installed:
        return installed[0]
    return "mock"


def _guess_target_file(workdir: Path, prompt_text: str) -> Path | None:
    for match in _FILE_HINT_RE.finditer(prompt_text):
        rel = match.group(1)
        candidate = (workdir / rel).resolve()
        try:
            candidate.relative_to(workdir.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    app_py = workdir / "app.py"
    return app_py if app_py.is_file() else None


@dataclass
class EmbeddedOrchestratorRunner:
    """In-process workspace runner backing ``bernstein acp serve --stdio``."""

    registry: ACPHandlerRegistry
    adapter_name: str | None = None
    worker_executor: Callable[[ACPSession, str, Path, asyncio.Event], Awaitable[str]] | None = None
    _active_workers: dict[str, _ActiveWorker] = field(
        default_factory=dict[str, _ActiveWorker],
        init=False,
        repr=False,
    )
    _workers_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _active_bridges: dict[str, ACPApprovalGateBridge] = field(
        default_factory=dict[str, ACPApprovalGateBridge],
        init=False,
        repr=False,
    )

    def _pop_active_worker(self, session_id: str) -> _ActiveWorker | None:
        with self._workers_lock:
            return self._active_workers.pop(session_id, None)

    def _set_active_worker(self, session_id: str, worker: _ActiveWorker) -> None:
        with self._workers_lock:
            self._active_workers[session_id] = worker

    async def create_legacy_task(self, prompt: str, cwd: str, role: str) -> PromptResult:
        """Persist a task directly into ``<cwd>/.sdd/runtime/tasks.jsonl`` for legacy ``prompt``."""
        workdir = Path(cwd).resolve()
        if not workdir.is_dir():
            return PromptResult(session_id="", accepted=False, message=f"workspace {cwd!r} does not exist")
        tasks_path = workdir / ".sdd" / "runtime" / "tasks.jsonl"
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        store = TaskStore(tasks_path)
        store.replay_jsonl()
        task = await store.create(
            _ACPTaskRequest(title=prompt[:100] or "ACP task", description=prompt, role=role)  # type: ignore[arg-type]
        )
        await store.flush_buffer()
        return PromptResult(session_id=task.id, accepted=True)

    async def cancel_session(self, session_id: str, reason: str) -> bool:
        """Drain pending approvals and reap the entire active worker process group."""
        bridge = self._active_bridges.get(session_id)
        if bridge is not None:
            bridge.reject_all_pending(reason)
        worker = self._pop_active_worker(session_id)
        if worker is not None:
            await asyncio.to_thread(_reap_worker, worker)
        return True

    async def run_prompt(
        self,
        session: ACPSession,
        blocks: list[dict[str, Any]],
        cancel_event: asyncio.Event,
    ) -> PromptRunOutcome:
        """Execute one governed turn in ``session.cwd`` and stream ``EventJournal`` updates."""
        if cancel_event.is_set():
            return PromptRunOutcome(stop_reason="cancelled")

        workdir = Path(session.cwd).resolve()
        if not workdir.is_dir():
            raise ACPSchemaError(INTERNAL_ERROR, f"workspace directory does not exist: {session.cwd}")

        session.set_active_task_id(session.session_id)
        prompt_text = extract_prompt_text(blocks).strip() or "(empty prompt)"
        title = prompt_text.splitlines()[0][:120]

        sdd_dir = workdir / ".sdd"
        (sdd_dir / "runtime").mkdir(parents=True, exist_ok=True)
        store = TaskStore(sdd_dir / "runtime" / "tasks.jsonl")
        store.replay_jsonl()
        journal = EventJournal.resume(session.run_id, sdd_dir)

        projector = JournalACPProjector()
        pending_updates: list[dict[str, Any]] = []

        def _on_journal_entry(entry: dict[str, Any]) -> None:
            pending_updates.extend(projector.feed_raw_entry(entry, include_user_messages=False))

        async def _flush_updates() -> None:
            while pending_updates:
                batch = list(pending_updates)
                pending_updates.clear()
                for upd in batch:
                    await self.registry.emit_session_update(session.session_id, upd)

        def _cancel_reason() -> str:
            return session.cancel_reason or "cancelled by client"

        journal.set_observer(_on_journal_entry)
        bridge = ACPApprovalGateBridge(workdir=workdir, session=session, registry=self.registry)
        self._active_bridges[session.session_id] = bridge
        bridge.start()
        created_task_id: str | None = None
        settled = False

        async def _settle_cancelled(task_id: str, reason: str) -> PromptRunOutcome:
            nonlocal settled
            bridge.reject_all_pending(reason)
            worker = self._pop_active_worker(session.session_id)
            if worker is not None:
                await asyncio.to_thread(_reap_worker, worker)
            with contextlib.suppress(KeyError, ValueError):
                await store.cancel(task_id, reason)
            await store.flush_buffer()
            settled = True
            journal.record("task_cancelled", task_id=task_id, reason=reason)
            await _flush_updates()
            return PromptRunOutcome(stop_reason="cancelled", usage=projector.usage_summary())

        try:
            journal.record("user_prompt", session_id=session.session_id, text=prompt_text)

            task = await store.create(
                _ACPTaskRequest(title=title, description=prompt_text, role=session.role)  # type: ignore[arg-type]
            )
            created_task_id = task.id
            await store.claim_by_id(task.id, claimed_by_session=session.session_id)
            await store.flush_buffer()

            journal.record(
                "plan.graph.full",
                goal=prompt_text,
                tasks=[{"id": task.id, "title": title, "role": session.role, "priority": 1, "depends_on": []}],
            )
            journal.record("agent_thought", text=f"Preparing governed execution for: {title}")
            await _flush_updates()

            target_file = _guess_target_file(workdir, prompt_text)
            rel_target = target_file.relative_to(workdir).as_posix() if target_file is not None else "."
            before_text = _read_utf8_text_if_valid(target_file)

            journal.record(
                "task_claimed",
                task_id=task.id,
                title=title,
                tool="edit_file",
                args={"path": rel_target},
            )
            await _flush_updates()

            if cancel_event.is_set():
                return await _settle_cancelled(task.id, _cancel_reason())

            # Gate workspace write/execution through core.approval.gate + ApprovalQueue,
            # racing against cancel_event so mid-permission cancel or EOF wakes immediately.
            ws_cfg = load_approval_config(workdir)
            effective_cfg = (
                ApprovalConfig(interactive=False, timeout_seconds=ws_cfg.timeout_seconds, smart_auto_approve=True)
                if session.mode == "auto"
                else ApprovalConfig(
                    interactive=True,
                    timeout_seconds=max(1, min(ws_cfg.timeout_seconds, int(self.registry.permission_timeout_s))),
                    smart_auto_approve=ws_cfg.smart_auto_approve,
                )
            )
            gate_task = asyncio.create_task(
                await_tool_call(
                    session_id=session.session_id,
                    agent_role=session.role,
                    tool_name="edit_file",
                    tool_args={"path": rel_target, "goal": title},
                    workdir=workdir,
                    queue=bridge.queue,
                    config=effective_cfg,
                )
            )
            cancel_wait = asyncio.create_task(cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {gate_task, cancel_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_wait in done or cancel_event.is_set():
                    reason_str = _cancel_reason()
                    bridge.reject_all_pending(reason_str)
                    if not gate_task.done():
                        gate_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, ApprovalTimeoutError):
                        await gate_task
                    return await _settle_cancelled(task.id, reason_str)
                resolution = await gate_task
            except ApprovalTimeoutError as exc:
                bridge.reject_all_pending("approval timeout")
                await bridge.stop()
                await store.fail(task.id, str(exc))
                await store.flush_buffer()
                settled = True
                journal.record("task_failed", task_id=task.id, reason=str(exc))
                journal.record("agent_message", text=f"Approval timed out: {exc}")
                await _flush_updates()
                return PromptRunOutcome(stop_reason="end_turn", usage=projector.usage_summary())
            finally:
                if not cancel_wait.done():
                    cancel_wait.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_wait

            if cancel_event.is_set():
                return await _settle_cancelled(task.id, _cancel_reason())

            if resolution is not None and resolution.decision is ApprovalDecision.REJECT:
                reason_msg = resolution.reason or "Tool execution rejected by client"
                await store.fail(task.id, reason_msg)
                await store.flush_buffer()
                settled = True
                journal.record("task_failed", task_id=task.id, reason=reason_msg)
                journal.record("agent_message", text=f"Action skipped: {reason_msg}")
                journal.record(
                    "usage_update",
                    input_tokens=max(1, len(prompt_text) // 4),
                    output_tokens=12,
                    size=DEFAULT_CONTEXT_WINDOW_TOKENS,
                )
                await _flush_updates()
                return PromptRunOutcome(stop_reason="end_turn", usage=projector.usage_summary())

            # Execute worker turn (custom executor or registered CLI adapter).
            summary_text, exit_code, was_cancelled = await self._execute_worker(
                session=session,
                task_id=task.id,
                task_title=title,
                prompt_text=prompt_text,
                workdir=workdir,
                journal=journal,
                cancel_event=cancel_event,
            )
            if was_cancelled:
                return await _settle_cancelled(task.id, _cancel_reason())

            in_tokens = max(1, len(prompt_text) // 4)
            out_tokens = max(1, len(summary_text) // 4)

            if exit_code != 0:
                fail_reason = f"Worker exited with code {exit_code}: {summary_text}"
                await store.fail(task.id, fail_reason)
                await store.flush_buffer()
                settled = True
                journal.record("task_failed", task_id=task.id, reason=fail_reason)
                journal.record(
                    "usage_update",
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    size=DEFAULT_CONTEXT_WINDOW_TOKENS,
                )
                journal.record("agent_message", text=fail_reason)
                await _flush_updates()
                return PromptRunOutcome(stop_reason="end_turn", usage=projector.usage_summary())

            after_text = _read_utf8_text_if_valid(target_file)
            if before_text is not None and after_text is not None and after_text != before_text:
                journal.record(
                    "task_diff_captured",
                    task_id=task.id,
                    path=rel_target,
                    old_text=before_text,
                    new_text=after_text,
                )

            await store.complete(task.id, summary_text)
            await store.flush_buffer()
            settled = True

            journal.record("task_completed", task_id=task.id, summary=summary_text)
            journal.record(
                "usage_update",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                size=DEFAULT_CONTEXT_WINDOW_TOKENS,
            )
            journal.record("agent_message", text=summary_text)
            await _flush_updates()
            return PromptRunOutcome(stop_reason="end_turn", usage=projector.usage_summary())
        except asyncio.CancelledError:
            if created_task_id is not None and not settled:
                bridge.reject_all_pending("transport closed")
                worker = self._pop_active_worker(session.session_id)
                if worker is not None:
                    _reap_worker(worker)
                with contextlib.suppress(KeyError, ValueError):
                    await store.cancel(created_task_id, "transport closed")
                await store.flush_buffer()
                settled = True
                journal.record("task_cancelled", task_id=created_task_id, reason="transport closed")
            raise
        except Exception as exc:
            if created_task_id is not None and not settled:
                bridge.reject_all_pending(str(exc))
                worker = self._pop_active_worker(session.session_id)
                if worker is not None:
                    await asyncio.to_thread(_reap_worker, worker)
                with contextlib.suppress(KeyError, ValueError):
                    await store.fail(created_task_id, str(exc))
                await store.flush_buffer()
                settled = True
                journal.record("task_failed", task_id=created_task_id, reason=str(exc))
                with contextlib.suppress(Exception):
                    await _flush_updates()
            raise
        finally:
            self._active_bridges.pop(session.session_id, None)
            if not settled:
                with contextlib.suppress(Exception):
                    await store.flush_buffer()
            journal.set_observer(None)
            await bridge.stop()

    async def _execute_worker(
        self,
        *,
        session: ACPSession,
        task_id: str,
        task_title: str,
        prompt_text: str,
        workdir: Path,
        journal: EventJournal,
        cancel_event: asyncio.Event,
    ) -> tuple[str, int, bool]:
        """Run worker step and return ``(summary_text, exit_code, was_cancelled)``."""
        if self.worker_executor is not None:
            out = await self.worker_executor(session, prompt_text, workdir, cancel_event)
            return out, 0, cancel_event.is_set()

        chosen_adapter = resolve_embedded_adapter_name(self.adapter_name)
        from bernstein.adapters.mock import MockAgentAdapter
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter(chosen_adapter)
        model_name = str(getattr(adapter, "default_model", None) or "mock")
        model_cfg = ModelConfig(model=model_name, effort="medium")
        run_sess_id = f"{session.run_id}-{session.turn_count}"
        adapter_any: Any = adapter

        def _spawn_sync() -> SpawnResult:
            orig_probe = getattr(adapter, "_probe_fast_exit", None)
            had_instance_probe = "_probe_fast_exit" in getattr(adapter, "__dict__", {})
            if callable(orig_probe):

                def _wrapped_probe(proc_obj: Any, *p_args: Any, **p_kwargs: Any) -> Any:
                    pid_val = int(getattr(proc_obj, "pid", 0) or 0)
                    raw_p = (
                        cast("subprocess.Popen[bytes]", proc_obj) if isinstance(proc_obj, subprocess.Popen) else None
                    )
                    worker_handle = _ActiveWorker(adapter=adapter, proc=raw_p, pid=pid_val)
                    self._set_active_worker(session.session_id, worker_handle)
                    if cancel_event.is_set():
                        _reap_worker(worker_handle)
                    return orig_probe(proc_obj, *p_args, **p_kwargs)

                adapter_any._probe_fast_exit = _wrapped_probe

            try:
                if isinstance(adapter, MockAgentAdapter):
                    return adapter_any.spawn(
                        prompt=prompt_text,
                        workdir=workdir,
                        model_config=model_cfg,
                        session_id=run_sess_id,
                        timeout_seconds=120,
                        task_id=task_id,
                        task_title=task_title,
                    )
                return adapter_any.spawn(
                    prompt=prompt_text,
                    workdir=workdir,
                    model_config=model_cfg,
                    session_id=run_sess_id,
                    timeout_seconds=120,
                )
            finally:
                if callable(orig_probe):
                    if had_instance_probe:
                        adapter_any._probe_fast_exit = orig_probe
                    else:
                        with contextlib.suppress(AttributeError):
                            delattr(adapter_any, "_probe_fast_exit")

        spawn_res: SpawnResult
        try:
            spawn_res = await asyncio.to_thread(_spawn_sync)
        except Exception:
            if cancel_event.is_set():
                worker = self._pop_active_worker(session.session_id)
                if worker is not None:
                    await asyncio.to_thread(_reap_worker, worker)
                return "Cancelled", 0, True
            raise

        raw_proc = spawn_res.proc
        proc: subprocess.Popen[bytes] | None = (
            cast("subprocess.Popen[bytes]", raw_proc) if isinstance(raw_proc, subprocess.Popen) else None
        )
        worker_pid = spawn_res.pid or (proc.pid if proc is not None else 0)
        worker_handle = _ActiveWorker(adapter=adapter, proc=proc, pid=worker_pid)
        self._set_active_worker(session.session_id, worker_handle)
        if worker_pid > 0:
            journal.record("worker_spawned", task_id=task_id, pid=worker_pid, adapter=chosen_adapter)

        try:
            while proc is not None and proc.poll() is None:
                if cancel_event.is_set():
                    self._pop_active_worker(session.session_id)
                    await asyncio.to_thread(_reap_worker, worker_handle)
                    return "Cancelled", 0, True
                await asyncio.sleep(0.03)
        except asyncio.CancelledError:
            self._pop_active_worker(session.session_id)
            _reap_worker(worker_handle)
            raise
        finally:
            self._pop_active_worker(session.session_id)
            if spawn_res.timeout_timer is not None:
                spawn_res.timeout_timer.cancel()
            if cancel_event.is_set() and proc is not None and proc.poll() is None:
                _reap_worker(worker_handle)

        if cancel_event.is_set():
            await asyncio.to_thread(_reap_worker, worker_handle)
            return "Cancelled", 0, True

        exit_code = int(proc.returncode) if proc is not None and proc.returncode is not None else 0
        log_tail = ""
        if spawn_res.log_path.is_file():
            with contextlib.suppress(OSError):
                text = spawn_res.log_path.read_text(encoding="utf-8", errors="replace")
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if lines:
                    log_tail = lines[-1]
        summary = log_tail or f"Completed task ({chosen_adapter}): {task_title}"
        return summary, exit_code, False


__all__ = [
    "ACPApprovalGateBridge",
    "EmbeddedOrchestratorRunner",
    "adapter_binary_name",
    "find_installed_cli_adapters",
    "is_adapter_usable",
    "resolve_embedded_adapter_name",
]
