"""Tests for provider batch execution with provider APIs fully mocked."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.batch_api import (
    AnthropicBatchClient,
    BatchJobRecord,
    BatchJobStatus,
    BatchPollResult,
    BatchProviderRequest,
    BatchProviderSubmission,
    ProviderBatchManager,
)
from bernstein.core.git_basic import GitResult
from bernstein.core.models import BatchConfig, ModelConfig


class _CollectorStub:
    """Minimal collector stub used by batch-api tests."""

    def __init__(self) -> None:
        self.task_metrics: dict[str, Any] = {}
        self.agent_metrics: dict[str, Any] = {}

    def start_agent(
        self,
        agent_id: str,
        role: str,
        model: str,
        provider: str,
        agent_source: str = "built-in",
        tenant_id: str | None = None,
    ) -> None:
        del role, model, provider, agent_source, tenant_id
        self.agent_metrics[agent_id] = SimpleNamespace(tasks_completed=0)

    def start_task(self, task_id: str, role: str, model: str, provider: str, tenant_id: str | None = None) -> None:
        del tenant_id
        self.task_metrics[task_id] = SimpleNamespace(
            task_id=task_id,
            role=role,
            model=model,
            provider=provider,
            start_time=0.0,
            end_time=None,
            cost_usd=0.0,
            tokens_prompt=0,
            tokens_completion=0,
            files_modified=0,
        )

    def complete_task(
        self,
        task_id: str,
        success: bool,
        tokens_used: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> None:
        metrics = self.task_metrics.setdefault(task_id, SimpleNamespace())
        metrics.success = success
        metrics.tokens_used = tokens_used
        metrics.cost_usd = cost_usd
        metrics.error = error

    def complete_agent_task(self, agent_id: str, success: bool, tokens_used: int = 0, cost_usd: float = 0.0) -> None:
        metrics = self.agent_metrics.setdefault(agent_id, SimpleNamespace(tasks_completed=0))
        metrics.success = success
        metrics.tokens_used = tokens_used
        metrics.cost_usd = cost_usd

    def end_agent(self, agent_id: str) -> None:
        metrics = self.agent_metrics.setdefault(agent_id, SimpleNamespace(tasks_completed=0))
        metrics.ended = True


class _ProviderClientStub:
    """Provider client stub with programmable submit/poll behavior."""

    def __init__(self, poll_result: BatchPollResult | None = None) -> None:
        self.submissions: list[BatchProviderRequest] = []
        self.polled_job_ids: list[str] = []
        self._poll_result = poll_result or BatchPollResult(done=False)

    def submit(self, request: BatchProviderRequest) -> BatchProviderSubmission:
        self.submissions.append(request)
        request.input_path.parent.mkdir(parents=True, exist_ok=True)
        request.input_path.write_text('{"submitted": true}\n', encoding="utf-8")
        return BatchProviderSubmission(external_id="batch-ext-1")

    def poll(self, job: Any) -> BatchPollResult:
        self.polled_job_ids.append(job.job_id)
        return self._poll_result


class _RouterStub:
    """Router stub that captures the base model config used for batch routing."""

    def __init__(self, provider: str = "openai", cost_per_1k_tokens: float = 0.02) -> None:
        self._provider = provider
        self.base_config: ModelConfig | None = None
        self.record_provider_cost = MagicMock()
        self.state = SimpleNamespace(
            providers={provider: SimpleNamespace(cost_per_1k_tokens=cost_per_1k_tokens)},
        )

    def select_provider_for_task(
        self,
        task: Any,
        base_config: ModelConfig | None = None,
        preferred_provider: str | None = None,
    ) -> Any:
        del task, preferred_provider
        self.base_config = base_config
        model_config = base_config or ModelConfig(model="gpt-5.4-mini-mini", effort="high", is_batch=True)
        return SimpleNamespace(provider=self._provider, model_config=model_config)


class _WorktreeManagerStub:
    """Worktree manager stub that returns a deterministic path."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def create(self, session_id: str) -> Path:
        del session_id
        self._path.mkdir(parents=True, exist_ok=True)
        return self._path

    def cleanup(self, session_id: str) -> None:
        del session_id


def _make_orch(tmp_path: Path, router: _RouterStub) -> Any:
    """Build the minimal orchestrator stub required by ProviderBatchManager."""
    client = MagicMock()
    client.post.return_value = SimpleNamespace(raise_for_status=MagicMock())
    return SimpleNamespace(
        _router=router,
        _spawner=SimpleNamespace(
            _worktree_mgr=_WorktreeManagerStub(tmp_path / "batch-worktree"),
            _worktree_paths={},
            reap_completed_agent=MagicMock(return_value=None),
        ),
        _batch_sessions={},
        _task_to_session={},
        _file_ownership={},
        _lock_manager=None,
        _client=client,
        _config=SimpleNamespace(server_url="http://server"),
        _recorder=MagicMock(),
        _release_task_to_session=MagicMock(),
        _release_file_ownership=MagicMock(),
        _quality_gate_config=None,
        _workdir=tmp_path,
    )


def test_try_submit_persists_job_for_auto_detected_batch_task(tmp_path: Path, make_task: Any) -> None:
    """Keyword-eligible tasks should submit through provider batch even without explicit batch_eligible=True."""
    router = _RouterStub()
    provider_client = _ProviderClientStub()
    manager = ProviderBatchManager(
        tmp_path,
        BatchConfig(enabled=True, eligible=["docs"]),
        provider_clients={"openai": provider_client},
    )
    manager._trace_store = MagicMock()
    collector = _CollectorStub()
    orch = _make_orch(tmp_path, router)
    task = make_task(id="T-docs", title="Update docs", description="Refresh the API docs.")

    with patch("bernstein.core.tasks.batch_api.get_collector", return_value=collector):
        result = manager.try_submit(orch, task)

    assert result.handled is True
    assert result.submitted is True
    assert router.base_config is not None
    assert router.base_config.is_batch is True
    assert provider_client.submissions[0].task_id == "T-docs"
    assert provider_client.submissions[0].input_path.exists()
    job_path = tmp_path / ".sdd" / "runtime" / "batch_jobs" / "batch-T-docs.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["task_id"] == "T-docs"
    assert job["status"] == "submitted"
    assert "T-docs" in orch._task_to_session


def test_poll_applies_diff_and_records_discounted_cost(tmp_path: Path, make_task: Any) -> None:
    """Completed provider batch jobs should apply the diff, finish the task, and record discounted cost."""
    router = _RouterStub(cost_per_1k_tokens=0.02)
    provider_client = _ProviderClientStub(
        BatchPollResult(
            done=True,
            output_text="diff --git a/README.md b/README.md\n+updated\n",
            input_tokens=500,
            output_tokens=500,
        )
    )
    manager = ProviderBatchManager(
        tmp_path,
        BatchConfig(enabled=True, eligible=["docs"]),
        provider_clients={"openai": provider_client},
    )
    manager._trace_store = MagicMock()
    collector = _CollectorStub()
    orch = _make_orch(tmp_path, router)
    task = make_task(id="T-batch-ok", title="Update docs", description="Refresh API docs.")

    with patch("bernstein.core.tasks.batch_api.get_collector", return_value=collector):
        submit_result = manager.try_submit(orch, task)

    assert submit_result.submitted is True

    with (
        patch("bernstein.core.tasks.batch_api.get_collector", return_value=collector),
        patch("bernstein.core.tasks.batch_api.apply_diff", return_value=GitResult(0, "", "")),
        patch("bernstein.core.tasks.batch_api._changed_files", return_value=["README.md"]),
        patch("bernstein.core.tasks.batch_api.stage_task_files", return_value=["README.md"]),
        patch("bernstein.core.tasks.batch_api.commit", return_value=GitResult(0, "", "")),
        patch("bernstein.core.tasks.batch_api.verify_task", return_value=(True, [])),
        patch("bernstein.core.tasks.batch_api.complete_task") as mock_complete_task,
    ):
        manager.poll(orch)

    mock_complete_task.assert_called_once()
    router.record_provider_cost.assert_called_once_with("openai", 1000, 0.01)
    job_path = tmp_path / ".sdd" / "runtime" / "batch_jobs" / "batch-T-batch-ok.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "applied"
    assert job["cost_usd"] == pytest.approx(0.01)
    metrics_path = tmp_path / ".sdd" / "metrics" / "tasks.jsonl"
    record = json.loads(metrics_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["task_id"] == "T-batch-ok"
    assert record["batch_state"] == "applied"
    assert record["cost_usd"] == pytest.approx(0.01)
    assert record["estimated_savings_usd"] == pytest.approx(0.01)


def test_poll_failure_marks_task_for_realtime_fallback(tmp_path: Path, make_task: Any) -> None:
    """A failed provider batch should requeue the task and skip batch submission on later ticks."""
    router = _RouterStub()
    provider_client = _ProviderClientStub(
        BatchPollResult(
            done=True,
            output_text="this is not a valid patch",
            input_tokens=300,
            output_tokens=200,
        )
    )
    manager = ProviderBatchManager(
        tmp_path,
        BatchConfig(enabled=True, eligible=["docs"]),
        provider_clients={"openai": provider_client},
    )
    manager._trace_store = MagicMock()
    collector = _CollectorStub()
    orch = _make_orch(tmp_path, router)
    task = make_task(id="T-batch-fallback", title="Update docs", description="Refresh API docs.")

    with patch("bernstein.core.tasks.batch_api.get_collector", return_value=collector):
        submit_result = manager.try_submit(orch, task)

    assert submit_result.submitted is True

    with (
        patch("bernstein.core.tasks.batch_api.get_collector", return_value=collector),
        patch("bernstein.core.tasks.batch_api.apply_diff", return_value=GitResult(1, "", "bad patch")),
    ):
        manager.poll(orch)

    orch._client.post.assert_called_with("http://server/tasks/T-batch-fallback/force-claim")
    job_path = tmp_path / ".sdd" / "runtime" / "batch_jobs" / "batch-T-batch-fallback.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["error"] == "bad patch"
    fallback_marker = tmp_path / ".sdd" / "runtime" / "batch_fallback" / "T-batch-fallback.flag"
    assert fallback_marker.exists()

    retry = manager.try_submit(orch, task)
    assert retry.handled is False
    assert retry.submitted is False


def test_try_submit_uses_anthropic_provider_for_claude_batch_task(tmp_path: Path, make_task: Any) -> None:
    """Claude-model non-urgent work should submit through the Anthropic batch path."""
    router = _RouterStub(provider="anthropic")
    provider_client = _ProviderClientStub()
    manager = ProviderBatchManager(
        tmp_path,
        BatchConfig(enabled=True, eligible=["docs"]),
        provider_clients={"anthropic": provider_client},
    )
    manager._trace_store = MagicMock()
    collector = _CollectorStub()
    orch = _make_orch(tmp_path, router)
    task = make_task(id="T-claude-batch", title="Update docs", description="Refresh Claude docs.")

    with patch("bernstein.core.tasks.batch_api.get_collector", return_value=collector):
        result = manager.try_submit(orch, task)

    assert result.handled is True
    assert result.submitted is True
    assert provider_client.submissions[0].task_id == "T-claude-batch"
    assert "T-claude-batch" in orch._task_to_session


def test_anthropic_batch_client_submit_posts_message_batch_request(tmp_path: Path) -> None:
    """Anthropic client should submit a message-batch payload and return the external id."""
    response = MagicMock()
    response.json.return_value = {"id": "msgbatch_123"}
    response.raise_for_status.return_value = None
    http_client = MagicMock()
    http_client.post.return_value = response

    client = AnthropicBatchClient(api_key="test-key", client=http_client)
    request = BatchProviderRequest(
        job_id="batch-T-123",
        task_id="T-123",
        model="claude-3-7-sonnet-20250219",
        system_prompt="system",
        user_prompt="user",
        max_tokens=2048,
        input_path=tmp_path / "payload.jsonl",
    )

    submission = client.submit(request)

    assert submission.external_id == "msgbatch_123"
    http_client.post.assert_called_once()
    called_json = http_client.post.call_args.kwargs["json"]
    assert called_json["requests"][0]["custom_id"] == "batch-T-123"
    assert called_json["requests"][0]["params"]["model"] == "claude-3-7-sonnet-20250219"


def test_anthropic_batch_client_poll_fetches_results_url() -> None:
    """Anthropic client should resolve results_url once the batch has ended."""
    status_response = MagicMock()
    status_response.json.return_value = {
        "processing_status": "ended",
        "results_url": "https://example.test/results",
    }
    status_response.raise_for_status.return_value = None

    results_response = MagicMock()
    results_response.text = (
        '{"custom_id":"batch-T-123","result":{"type":"succeeded","message":{"content":[{"type":"text","text":"'
        'diff --git a/README.md b/README.md\\n+hello\\n"}],"usage":{"input_tokens":50,"output_tokens":25}}}}\n'
    )
    results_response.raise_for_status.return_value = None

    http_client = MagicMock()
    http_client.get.side_effect = [status_response, results_response]
    client = AnthropicBatchClient(api_key="test-key", client=http_client)
    job = BatchJobRecord(
        job_id="batch-T-123",
        task_id="T-123",
        session_id="session-1",
        provider_name="anthropic",
        provider_kind="anthropic",
        model="claude-3-7-sonnet-20250219",
        effort="high",
        external_id="msgbatch_123",
        worktree_path="/var/lib/bernstein/worktree",
        status=BatchJobStatus.SUBMITTED,
        task_payload={"id": "T-123"},
    )

    result = client.poll(job)

    assert result.done is True
    assert result.failed is False
    assert "diff --git" in result.output_text
    assert result.input_tokens == 50
    assert result.output_tokens == 25
