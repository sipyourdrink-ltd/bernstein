"""Provider batch execution for low-risk single-task work."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from bernstein.core.batch_router import BATCH_DISCOUNT_FACTOR, BatchMode, classify_batch_mode
from bernstein.core.fast_path import TaskLevel, classify_task
from bernstein.core.git_basic import commit, run_git, stage_files, stage_task_files
from bernstein.core.git_pr import apply_diff
from bernstein.core.janitor import verify_task
from bernstein.core.lifecycle import transition_agent
from bernstein.core.metrics import get_collector
from bernstein.core.models import AgentSession, ModelConfig, Task
from bernstein.core.quality_gates import run_quality_gates
from bernstein.core.router import route_task
from bernstein.core.tick_pipeline import complete_task
from bernstein.core.traces import AgentTrace, TraceStep, TraceStore

if TYPE_CHECKING:
    from bernstein.core.models import BatchConfig

logger = logging.getLogger(__name__)


class BatchJobStatus(StrEnum):
    """Persisted state for a provider batch job."""

    SUBMITTED = "submitted"
    PROCESSING = "processing"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(frozen=True)
class BatchSubmissionResult:
    """Outcome of a batch-submission attempt."""

    handled: bool
    submitted: bool
    session_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class BatchProviderRequest:
    """Provider-neutral payload sent to a batch API client."""

    job_id: str
    task_id: str
    model: str
    system_prompt: str
    user_prompt: str
    max_tokens: int
    input_path: Path


@dataclass(frozen=True)
class BatchProviderSubmission:
    """Provider submission metadata."""

    external_id: str


@dataclass(frozen=True)
class BatchPollResult:
    """Normalized provider polling result."""

    done: bool
    failed: bool = False
    output_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


class BatchProviderClient(Protocol):
    """Provider-neutral interface for batch submission and polling."""

    def submit(self, request: BatchProviderRequest) -> BatchProviderSubmission:
        """Submit a new provider batch job."""
        ...

    def poll(self, job: BatchJobRecord) -> BatchPollResult:
        """Poll a provider batch job until it completes or fails."""
        ...


@dataclass
class BatchJobRecord:
    """File-backed provider batch job record."""

    job_id: str
    task_id: str
    session_id: str
    provider_name: str
    provider_kind: str
    model: str
    effort: str
    external_id: str
    worktree_path: str
    status: BatchJobStatus
    task_payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize the batch job to a JSON-safe dict."""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchJobRecord:
        """Deserialize a persisted batch job record."""
        return cls(
            job_id=str(data["job_id"]),
            task_id=str(data["task_id"]),
            session_id=str(data["session_id"]),
            provider_name=str(data["provider_name"]),
            provider_kind=str(data["provider_kind"]),
            model=str(data["model"]),
            effort=str(data.get("effort", "high")),
            external_id=str(data["external_id"]),
            worktree_path=str(data["worktree_path"]),
            status=BatchJobStatus(str(data["status"])),
            task_payload=dict(data["task_payload"]),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            error=str(data.get("error", "")),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
        )


class BatchJobStore:
    """Persist batch jobs as JSON files under `.sdd/runtime/batch_jobs/`."""

    def __init__(self, runtime_dir: Path) -> None:
        self._dir = runtime_dir / "batch_jobs"
        self._payload_dir = runtime_dir / "batch_payloads"
        self._fallback_dir = runtime_dir / "batch_fallback"

    def payload_path(self, job_id: str) -> Path:
        """Return the request payload path for a batch job."""
        self._payload_dir.mkdir(parents=True, exist_ok=True)
        return self._payload_dir / f"{job_id}.jsonl"

    def save(self, job: BatchJobRecord) -> None:
        """Persist a batch job snapshot."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{job.job_id}.json"
        path.write_text(json.dumps(job.to_dict(), sort_keys=True), encoding="utf-8")

    def has_realtime_fallback(self, task_id: str) -> bool:
        """Return True when a task has already failed batch and should use realtime fallback."""
        return (self._fallback_dir / f"{task_id}.flag").exists()

    def mark_realtime_fallback(self, task_id: str, reason: str) -> None:
        """Persist a one-way fallback marker for a task ID.

        Task IDs are unique, so a task that already failed provider batch should
        fall through to the normal realtime spawn path on later ticks.
        """
        self._fallback_dir.mkdir(parents=True, exist_ok=True)
        marker = self._fallback_dir / f"{task_id}.flag"
        marker.write_text(reason + "\n", encoding="utf-8")

    def list_active(self) -> list[BatchJobRecord]:
        """Return active batch jobs that still need polling."""
        if not self._dir.exists():
            return []
        jobs: list[BatchJobRecord] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                job = BatchJobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed batch job record: %s", path)
                continue
            if job.status in {BatchJobStatus.SUBMITTED, BatchJobStatus.PROCESSING}:
                jobs.append(job)
        return jobs


class OpenAIBatchClient:
    """OpenAI Batch API client."""

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        from openai import OpenAI

        self._client = OpenAI(api_key=self._api_key)
        return self._client

    def submit(self, request: BatchProviderRequest) -> BatchProviderSubmission:
        client = self._get_client()
        request.input_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "custom_id": request.job_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                "max_tokens": request.max_tokens,
            },
        }
        request.input_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with request.input_path.open("rb") as fh:
            uploaded = client.files.create(file=fh, purpose="batch")
        batch = client.batches.create(
            completion_window="24h",
            endpoint="/v1/chat/completions",
            input_file_id=uploaded.id,
            metadata={"task_id": request.task_id},
        )
        return BatchProviderSubmission(external_id=str(batch.id))

    def poll(self, job: BatchJobRecord) -> BatchPollResult:
        client = self._get_client()
        batch = client.batches.retrieve(job.external_id)
        status = str(getattr(batch, "status", ""))
        if status in {"validating", "in_progress", "finalizing", "cancelling"}:
            return BatchPollResult(done=False)
        if status != "completed":
            return BatchPollResult(done=True, failed=True, error=f"OpenAI batch ended with status={status}")
        output_file_id = getattr(batch, "output_file_id", None)
        if not output_file_id:
            return BatchPollResult(done=True, failed=True, error="OpenAI batch missing output_file_id")
        output = client.files.content(output_file_id).text
        return _parse_openai_output(job, output)


class AnthropicBatchClient:
    """Anthropic Message Batches API client."""

    _BASE_URL = "https://api.anthropic.com/v1/messages/batches"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = client or httpx.Client(timeout=30.0)

    @property
    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def submit(self, request: BatchProviderRequest) -> BatchProviderSubmission:
        payload = {
            "requests": [
                {
                    "custom_id": request.job_id,
                    "params": {
                        "model": request.model,
                        "max_tokens": request.max_tokens,
                        "system": request.system_prompt,
                        "messages": [{"role": "user", "content": request.user_prompt}],
                    },
                }
            ]
        }
        response = self._client.post(self._BASE_URL, headers=self._headers, json=payload)
        response.raise_for_status()
        data = response.json()
        return BatchProviderSubmission(external_id=str(data["id"]))

    def poll(self, job: BatchJobRecord) -> BatchPollResult:
        response = self._client.get(f"{self._BASE_URL}/{job.external_id}", headers=self._headers)
        response.raise_for_status()
        data = response.json()
        processing_status = str(data.get("processing_status", ""))
        if processing_status != "ended":
            return BatchPollResult(done=False)
        results_url = data.get("results_url")
        if not isinstance(results_url, str) or not results_url:
            return BatchPollResult(done=True, failed=True, error="Anthropic batch missing results_url")
        results = self._client.get(results_url, headers=self._headers)
        results.raise_for_status()
        return _parse_anthropic_output(job, results.text)


class ProviderBatchManager:
    """Submit and poll provider batch jobs."""

    def __init__(
        self,
        workdir: Path,
        config: BatchConfig,
        provider_clients: dict[str, BatchProviderClient] | None = None,
    ) -> None:
        self._workdir = workdir
        self._config = config
        self._runtime_dir = workdir / ".sdd" / "runtime"
        self._metrics_dir = workdir / ".sdd" / "metrics"
        self._store = BatchJobStore(self._runtime_dir)
        self._trace_store = TraceStore(workdir / ".sdd" / "traces")
        self._provider_clients = provider_clients or {
            "openai": OpenAIBatchClient(),
            "anthropic": AnthropicBatchClient(),
        }

    def try_submit(self, orch: Any, task: Task) -> BatchSubmissionResult:
        """Submit a single task to a provider batch API when eligible."""
        if not self._config.enabled:
            return BatchSubmissionResult(handled=False, submitted=False)
        if self._store.has_realtime_fallback(task.id):
            return BatchSubmissionResult(handled=False, submitted=False)
        if classify_batch_mode(task).mode != BatchMode.BATCH:
            return BatchSubmissionResult(handled=False, submitted=False)
        if classify_task(task).level == TaskLevel.L0:
            return BatchSubmissionResult(handled=False, submitted=False)
        if not self._is_config_eligible(task):
            return BatchSubmissionResult(handled=False, submitted=False)
        router = getattr(orch, "_router", None)
        if router is None:
            return BatchSubmissionResult(handled=False, submitted=False)

        base_config = route_task(task)
        if not base_config.is_batch:
            base_config = ModelConfig(
                model=base_config.model,
                effort=base_config.effort,
                max_tokens=base_config.max_tokens,
                is_batch=True,
            )
        decision = router.select_provider_for_task(task, base_config=base_config)
        provider_kind = _provider_kind(decision.provider, decision.model_config.model)
        provider_client = self._provider_clients.get(provider_kind) if provider_kind else None
        if provider_client is None:
            return BatchSubmissionResult(handled=False, submitted=False)
        assert provider_kind is not None

        worktree_mgr = getattr(getattr(orch, "_spawner", None), "_worktree_mgr", None)
        if worktree_mgr is None:
            return BatchSubmissionResult(handled=False, submitted=False)

        session_id = f"batch-{task.id}-{uuid.uuid4().hex[:8]}"
        session = AgentSession(
            id=session_id,
            role=task.role,
            task_ids=[task.id],
            model_config=decision.model_config,
            heartbeat_ts=time.time(),
            spawn_ts=time.time(),
            status="working",
            provider=decision.provider,
        )

        collector = get_collector(self._metrics_dir)
        collector.start_agent(
            agent_id=session.id,
            role=session.role,
            model=session.model_config.model,
            provider=session.provider or decision.provider,
            agent_source="provider-batch",
            tenant_id=task.tenant_id,
        )
        collector.start_task(
            task_id=task.id,
            role=task.role,
            model=decision.model_config.model,
            provider=decision.provider,
            tenant_id=task.tenant_id,
        )

        worktree_path: Path | None = None
        try:
            worktree_path = worktree_mgr.create(session.id)
            orch._spawner._worktree_paths[session.id] = worktree_path  # pyright: ignore[reportAttributeAccessIssue]
            if not hasattr(orch, "_batch_sessions"):
                orch._batch_sessions = {}
            request = BatchProviderRequest(
                job_id=f"batch-{task.id}",
                task_id=task.id,
                model=decision.model_config.model,
                system_prompt=_system_prompt(),
                user_prompt=_user_prompt(task),
                max_tokens=max(1024, min(decision.model_config.max_tokens, 8192)),
                input_path=self._store.payload_path(f"batch-{task.id}"),
            )
            submission = provider_client.submit(request)
            record = BatchJobRecord(
                job_id=request.job_id,
                task_id=task.id,
                session_id=session.id,
                provider_name=decision.provider,
                provider_kind=provider_kind,
                model=decision.model_config.model,
                effort=decision.model_config.effort,
                external_id=submission.external_id,
                worktree_path=str(worktree_path),
                status=BatchJobStatus.SUBMITTED,
                task_payload=_task_snapshot(task),
            )
            self._store.save(record)
            batch_sessions = _get_batch_sessions(orch)
            batch_sessions[session.id] = session
            orch._task_to_session[task.id] = session.id
            _claim_file_ownership(orch, session.id, [task])
            orch._recorder.record(
                "batch_job_submitted",
                task_id=task.id,
                batch_id=record.job_id,
                session_id=session.id,
                provider=decision.provider,
                model=decision.model_config.model,
            )
            return BatchSubmissionResult(handled=True, submitted=True, session_id=session.id)
        except Exception as exc:
            reason = f"submission failed: {exc}"
            logger.warning("Provider batch submission failed for %s: %s", task.id, exc)
            self._fail_to_realtime(
                orch,
                task,
                session=session,
                reason=reason,
                cost_usd=0.0,
                input_tokens=0,
                output_tokens=0,
            )
            return BatchSubmissionResult(handled=True, submitted=False, reason=reason)

    def poll(self, orch: Any) -> None:
        """Poll all active provider batch jobs."""
        for job in self._store.list_active():
            session = self._restore_session(orch, job)
            task = Task.from_dict(job.task_payload)
            provider_client = self._provider_clients.get(job.provider_kind)
            if provider_client is None:
                self._fail_to_realtime(
                    orch,
                    task,
                    session=session,
                    reason=f"unsupported provider kind: {job.provider_kind}",
                    cost_usd=job.cost_usd,
                    input_tokens=job.input_tokens,
                    output_tokens=job.output_tokens,
                )
                job.status = BatchJobStatus.FAILED
                job.error = f"unsupported provider kind: {job.provider_kind}"
                job.updated_at = time.time()
                self._store.save(job)
                continue

            try:
                polled = provider_client.poll(job)
            except Exception as exc:
                job.status = BatchJobStatus.FAILED
                job.error = f"poll failed: {exc}"
                job.updated_at = time.time()
                self._store.save(job)
                self._fail_to_realtime(
                    orch,
                    task,
                    session=session,
                    reason=job.error,
                    cost_usd=job.cost_usd,
                    input_tokens=job.input_tokens,
                    output_tokens=job.output_tokens,
                )
                continue

            if not polled.done:
                if job.status != BatchJobStatus.PROCESSING:
                    job.status = BatchJobStatus.PROCESSING
                    job.updated_at = time.time()
                    self._store.save(job)
                continue

            if polled.failed:
                job.status = BatchJobStatus.FAILED
                job.error = polled.error
                job.input_tokens = polled.input_tokens
                job.output_tokens = polled.output_tokens
                job.cost_usd = _discounted_cost(orch, job.provider_name, polled.input_tokens + polled.output_tokens)
                job.updated_at = time.time()
                self._store.save(job)
                self._fail_to_realtime(
                    orch,
                    task,
                    session=session,
                    reason=polled.error or "provider batch failed",
                    cost_usd=job.cost_usd,
                    input_tokens=polled.input_tokens,
                    output_tokens=polled.output_tokens,
                )
                continue

            job.input_tokens = polled.input_tokens
            job.output_tokens = polled.output_tokens
            job.cost_usd = _discounted_cost(orch, job.provider_name, polled.input_tokens + polled.output_tokens)
            if not self._apply_and_complete(orch, job, session, task, polled.output_text):
                continue
            job.status = BatchJobStatus.APPLIED
            job.updated_at = time.time()
            self._store.save(job)

    def _apply_and_complete(
        self,
        orch: Any,
        job: BatchJobRecord,
        session: AgentSession,
        task: Task,
        output: str,
    ) -> bool:
        worktree_path = self._ensure_worktree(orch, job, session)
        diff_text = _extract_diff_text(output)
        if not diff_text.strip():
            self._fail_to_realtime(
                orch,
                task,
                session=session,
                reason="provider batch returned no diff",
                cost_usd=job.cost_usd,
                input_tokens=job.input_tokens,
                output_tokens=job.output_tokens,
            )
            job.status = BatchJobStatus.FAILED
            job.error = "provider batch returned no diff"
            job.updated_at = time.time()
            self._store.save(job)
            return False

        apply_result = apply_diff(worktree_path, diff_text)
        if not apply_result.ok:
            reason = apply_result.stderr.strip() or "git apply failed"
            self._fail_to_realtime(
                orch,
                task,
                session=session,
                reason=reason,
                cost_usd=job.cost_usd,
                input_tokens=job.input_tokens,
                output_tokens=job.output_tokens,
            )
            job.status = BatchJobStatus.FAILED
            job.error = reason
            job.updated_at = time.time()
            self._store.save(job)
            return False

        changed_files = _changed_files(worktree_path)
        staged = stage_task_files(worktree_path, task.owned_files or changed_files)
        if not staged and changed_files:
            stage_files(worktree_path, changed_files)
            staged = changed_files
        if not staged:
            self._fail_to_realtime(
                orch,
                task,
                session=session,
                reason="provider batch produced no staged changes",
                cost_usd=job.cost_usd,
                input_tokens=job.input_tokens,
                output_tokens=job.output_tokens,
            )
            job.status = BatchJobStatus.FAILED
            job.error = "provider batch produced no staged changes"
            job.updated_at = time.time()
            self._store.save(job)
            return False

        commit_result = commit(worktree_path, f"feat(batch): {task.title[:72]}", enforce_conventional=True)
        if not commit_result.ok:
            reason = commit_result.stderr.strip() or "git commit failed"
            self._fail_to_realtime(
                orch,
                task,
                session=session,
                reason=reason,
                cost_usd=job.cost_usd,
                input_tokens=job.input_tokens,
                output_tokens=job.output_tokens,
            )
            job.status = BatchJobStatus.FAILED
            job.error = reason
            job.updated_at = time.time()
            self._store.save(job)
            return False

        passed, failed_signals = verify_task(task, worktree_path)
        if not passed:
            reason = "; ".join(failed_signals) or "janitor verification failed"
            self._fail_to_realtime(
                orch,
                task,
                session=session,
                reason=reason,
                cost_usd=job.cost_usd,
                input_tokens=job.input_tokens,
                output_tokens=job.output_tokens,
            )
            job.status = BatchJobStatus.FAILED
            job.error = reason
            job.updated_at = time.time()
            self._store.save(job)
            return False

        qg_config = getattr(orch, "_quality_gate_config", None)
        if qg_config is not None:
            qg_result = run_quality_gates(task, worktree_path, orch._workdir, qg_config)
            if not qg_result.passed:
                failed_gates = [r.gate for r in qg_result.gate_results if r.blocked and not r.passed]
                reason = ", ".join(failed_gates) or "quality gates failed"
                self._fail_to_realtime(
                    orch,
                    task,
                    session=session,
                    reason=reason,
                    cost_usd=job.cost_usd,
                    input_tokens=job.input_tokens,
                    output_tokens=job.output_tokens,
                )
                job.status = BatchJobStatus.FAILED
                job.error = reason
                job.updated_at = time.time()
                self._store.save(job)
                return False

        metrics = get_collector(self._metrics_dir).task_metrics.get(task.id)
        if metrics is not None:
            metrics.tokens_prompt = job.input_tokens
            metrics.tokens_completion = job.output_tokens
            metrics.cost_usd = job.cost_usd
            metrics.files_modified = len(changed_files)
        if getattr(orch, "_router", None) is not None:
            orch._router.record_provider_cost(job.provider_name, job.input_tokens + job.output_tokens, job.cost_usd)
        orch._recorder.record(
            "batch_job_applied",
            task_id=task.id,
            batch_id=job.job_id,
            provider=job.provider_name,
            model=job.model,
            cost_usd=round(job.cost_usd, 6),
        )
        self._trace_store.write(
            AgentTrace(
                trace_id=uuid.uuid4().hex[:16],
                session_id=session.id,
                task_ids=[task.id],
                agent_role=task.role,
                model=job.model,
                effort=job.effort,
                spawn_ts=session.spawn_ts,
                end_ts=time.time(),
                outcome="success",
                log_path="",
                task_snapshots=[job.task_payload],
                steps=[
                    TraceStep(
                        type="spawn",
                        timestamp=session.spawn_ts,
                        detail=f"Submitted provider batch via {job.provider_name}",
                    ),
                    TraceStep(
                        type="edit",
                        timestamp=time.time(),
                        detail="Applied provider batch diff",
                        files=changed_files,
                    ),
                    TraceStep(type="verify", timestamp=time.time(), detail="Batch diff passed janitor/quality gates"),
                    TraceStep(
                        type="complete",
                        timestamp=time.time(),
                        detail="Marked task complete after provider batch",
                    ),
                ],
            )
        )
        self._append_tasks_metric(
            task=task,
            provider=job.provider_name,
            model=job.model,
            cost_usd=job.cost_usd,
            input_tokens=job.input_tokens,
            output_tokens=job.output_tokens,
            batch_state="applied",
            success=True,
        )
        try:
            complete_task(
                orch._client,
                orch._config.server_url,
                task.id,
                f"Applied provider batch diff via {job.provider_name} ({job.model}).",
            )
        except Exception as exc:
            reason = f"complete_task failed: {exc}"
            self._fail_to_realtime(
                orch,
                task,
                session=session,
                reason=reason,
                cost_usd=job.cost_usd,
                input_tokens=job.input_tokens,
                output_tokens=job.output_tokens,
            )
            job.status = BatchJobStatus.FAILED
            job.error = reason
            job.updated_at = time.time()
            self._store.save(job)
            return False
        return True

    def _restore_session(self, orch: Any, job: BatchJobRecord) -> AgentSession:
        batch_sessions = _get_batch_sessions(orch)
        if job.session_id in batch_sessions:
            return batch_sessions[job.session_id]
        session = AgentSession(
            id=job.session_id,
            role=str(job.task_payload.get("role", "backend")),
            task_ids=[job.task_id],
            model_config=ModelConfig(job.model, job.effort),
            heartbeat_ts=job.updated_at,
            spawn_ts=job.created_at,
            status="working",
            provider=job.provider_name,
        )
        batch_sessions[session.id] = session
        orch._task_to_session[job.task_id] = session.id
        orch._spawner._worktree_paths[session.id] = Path(job.worktree_path)  # pyright: ignore[reportAttributeAccessIssue]
        _claim_file_ownership(orch, session.id, [Task.from_dict(job.task_payload)])
        return session

    def _ensure_worktree(self, orch: Any, job: BatchJobRecord, session: AgentSession) -> Path:
        worktree_path = Path(job.worktree_path)
        if worktree_path.exists():
            orch._spawner._worktree_paths[session.id] = worktree_path  # pyright: ignore[reportAttributeAccessIssue]
            return worktree_path
        worktree_mgr = getattr(getattr(orch, "_spawner", None), "_worktree_mgr", None)
        if worktree_mgr is None:
            raise RuntimeError("worktree manager is unavailable for provider batch job")
        recreated = Path(worktree_mgr.create(session.id))
        orch._spawner._worktree_paths[session.id] = recreated  # pyright: ignore[reportAttributeAccessIssue]
        job.worktree_path = str(recreated)
        job.updated_at = time.time()
        self._store.save(job)
        return recreated

    def _is_config_eligible(self, task: Task) -> bool:
        if task.batch_eligible is True:
            return True
        if not self._config.eligible:
            return True
        haystack = f"{task.title}\n{task.description}".lower()
        return any(token.lower() in haystack for token in self._config.eligible)

    def _fail_to_realtime(
        self,
        orch: Any,
        task: Task,
        *,
        session: AgentSession,
        reason: str,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        logger.warning("Falling back to realtime spawn for %s: %s", task.id, reason)
        with contextlib.suppress(OSError):
            self._store.mark_realtime_fallback(task.id, reason)
        collector = get_collector(self._metrics_dir)
        collector.complete_task(
            task.id,
            success=False,
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost_usd,
            error=reason,
        )
        collector.complete_agent_task(
            session.id,
            success=False,
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost_usd,
        )
        collector.end_agent(session.id)
        self._append_tasks_metric(
            task=task,
            provider=session.provider or "unknown",
            model=session.model_config.model,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            batch_state="fallback",
            success=False,
        )
        self._trace_store.write(
            AgentTrace(
                trace_id=uuid.uuid4().hex[:16],
                session_id=session.id,
                task_ids=[task.id],
                agent_role=task.role,
                model=session.model_config.model,
                effort=session.model_config.effort,
                spawn_ts=session.spawn_ts,
                end_ts=time.time(),
                outcome="failed",
                log_path="",
                task_snapshots=[_task_snapshot(task)],
                steps=[
                    TraceStep(
                        type="spawn",
                        timestamp=session.spawn_ts,
                        detail=f"Submitted provider batch via {session.provider or 'unknown'}",
                    ),
                    TraceStep(type="fail", timestamp=time.time(), detail=reason),
                ],
            )
        )
        orch._recorder.record(
            "batch_job_failed",
            task_id=task.id,
            session_id=session.id,
            provider=session.provider,
            reason=reason,
        )
        try:
            orch._client.post(f"{orch._config.server_url}/tasks/{task.id}/force-claim").raise_for_status()
        except Exception as exc:
            logger.error("Failed to requeue batch task %s: %s", task.id, exc)
        with contextlib.suppress(Exception):
            if session.status != "dead":
                transition_agent(session, "dead", actor="batch_api", reason=reason)
            orch._spawner.reap_completed_agent(session, skip_merge=True)
        batch_sessions = _get_batch_sessions(orch)
        batch_sessions.pop(session.id, None)
        release_tasks = getattr(orch, "_release_task_to_session", None)
        if callable(release_tasks):
            release_tasks([task.id])
        release_files = getattr(orch, "_release_file_ownership", None)
        if callable(release_files):
            release_files(session.id)

    def _append_tasks_metric(
        self,
        *,
        task: Task,
        provider: str,
        model: str,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
        batch_state: str,
        success: bool,
    ) -> None:
        record = {
            "timestamp": time.time(),
            "task_id": task.id,
            "role": task.role,
            "model": model,
            "provider": provider,
            "duration_seconds": 0.0,
            "tokens_prompt": input_tokens,
            "tokens_completion": output_tokens,
            "cost_usd": cost_usd,
            "estimated_savings_usd": round(cost_usd, 6),
            "janitor_passed": success,
            "files_modified": 0,
            "lines_added": 0,
            "lines_deleted": 0,
            "batch_state": batch_state,
        }
        tasks_jsonl = self._metrics_dir / "tasks.jsonl"
        try:
            self._metrics_dir.mkdir(parents=True, exist_ok=True)
            with tasks_jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.warning("Could not persist batch task record to tasks.jsonl: %s", exc)


def _provider_kind(provider_name: str, model: str) -> str | None:
    """Map a router provider/model pair to a supported batch backend."""
    text = f"{provider_name} {model}".lower()
    if "openai" in text or "codex" in text or text.startswith("gpt-") or " gpt-" in text:
        return "openai"
    if "anthropic" in text or "claude" in text:
        return "anthropic"
    return None


def _system_prompt() -> str:
    """Return the system instruction for diff-only batch jobs."""
    return (
        "You are a coding agent operating in provider batch mode. "
        "Return ONLY a unified git diff that can be applied with `git apply`. "
        "Do not include markdown fences, explanations, or any prose."
    )


def _user_prompt(task: Task) -> str:
    """Build the user prompt for a diff-only batch job."""
    owned = "\n".join(f"- {path}" for path in task.owned_files) or "- (no owned files provided)"
    return (
        f"Task ID: {task.id}\n"
        f"Title: {task.title}\n\n"
        f"Description:\n{task.description}\n\n"
        "Preferred files:\n"
        f"{owned}\n\n"
        "Produce the smallest safe patch that satisfies the task. "
        "Return a unified diff only."
    )


def _task_snapshot(task: Task) -> dict[str, Any]:
    """Serialize a task to the server-style dict used by Task.from_dict()."""
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": task.estimated_minutes,
        "status": task.status.value,
        "depends_on": list(task.depends_on),
        "owned_files": list(task.owned_files),
        "assigned_agent": task.assigned_agent,
        "result_summary": task.result_summary,
        "task_type": task.task_type.value,
        "model": task.model,
        "effort": task.effort,
        "batch_eligible": task.batch_eligible,
        "completion_signals": [{"type": signal.type, "value": signal.value} for signal in task.completion_signals],
    }


def _changed_files(worktree_path: Path) -> list[str]:
    """Return changed files after applying a provider diff."""
    result = run_git(["diff", "--name-only", "HEAD"], worktree_path, timeout=10)
    if not result.ok:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _extract_diff_text(output: str) -> str:
    """Strip optional markdown fences from model output."""
    text = output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _discounted_cost(orch: Any, provider_name: str, total_tokens: int) -> float:
    """Estimate discounted provider batch cost from router pricing data."""
    router = getattr(orch, "_router", None)
    if router is None:
        return 0.0
    provider = router.state.providers.get(provider_name)
    if provider is None:
        return 0.0
    standard_cost = (total_tokens / 1000.0) * provider.cost_per_1k_tokens
    return round(standard_cost * BATCH_DISCOUNT_FACTOR, 6)


def _parse_openai_output(job: BatchJobRecord, raw_output: str) -> BatchPollResult:
    """Parse the first completed OpenAI batch line for this job."""
    for line in raw_output.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("custom_id") != job.job_id:
            continue
        if payload.get("error"):
            return BatchPollResult(done=True, failed=True, error=str(payload["error"]))
        body = payload.get("response", {}).get("body", {})
        choices = body.get("choices", [])
        if not choices:
            return BatchPollResult(done=True, failed=True, error="OpenAI batch returned no choices")
        message = choices[0].get("message", {})
        content = _extract_content_text(message.get("content", ""))
        usage = body.get("usage", {})
        return BatchPollResult(
            done=True,
            output_text=str(content),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
        )
    return BatchPollResult(done=True, failed=True, error="OpenAI batch output did not include the request")


def _parse_anthropic_output(job: BatchJobRecord, raw_output: str) -> BatchPollResult:
    """Parse the first completed Anthropic batch line for this job."""
    for line in raw_output.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("custom_id") != job.job_id:
            continue
        result = payload.get("result", {})
        if result.get("type") != "succeeded":
            return BatchPollResult(done=True, failed=True, error=f"Anthropic result={result.get('type')}")
        message = result.get("message", {})
        text = _extract_content_text(message.get("content", []))
        usage = message.get("usage", {})
        return BatchPollResult(
            done=True,
            output_text=text,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )
    return BatchPollResult(done=True, failed=True, error="Anthropic batch output did not include the request")


def _claim_file_ownership(orch: Any, agent_id: str, tasks: list[Task]) -> None:
    """Mirror task_lifecycle file-ownership behavior for batch sessions."""
    lock_manager = getattr(orch, "_lock_manager", None)
    for task in tasks:
        if task.owned_files and lock_manager is not None:
            lock_manager.acquire(task.owned_files, agent_id=agent_id, task_id=task.id, task_title=task.title)
        for file_path in task.owned_files:
            orch._file_ownership[file_path] = agent_id


def _get_batch_sessions(orch: Any) -> dict[str, AgentSession]:
    """Return the orchestrator's batch-session map, creating it when absent."""
    raw = getattr(orch, "_batch_sessions", None)
    if isinstance(raw, dict):
        return cast("dict[str, AgentSession]", raw)
    batch_sessions: dict[str, AgentSession] = {}
    orch._batch_sessions = batch_sessions
    return batch_sessions


def _extract_content_text(content: object) -> str:
    """Normalize provider message content to plain text."""
    if isinstance(content, list):
        parts: list[str] = []
        for item in cast("list[object]", content):
            if not isinstance(item, dict):
                continue
            part = cast("dict[str, object]", item)
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return str(content)
