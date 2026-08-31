"""Tests for endpoint identity stamping on AgentSession (issue #4908).

Verifies that ``spawn_for_tasks`` and ``spawn_for_resume`` stamp the four
endpoint identity fields (``endpoint_adapter_name``, ``endpoint_model``,
``endpoint_base_url``, ``endpoint_profile_name``) on every created
``AgentSession`` by resolving them from the role policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import SpawnResult
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    AdapterPluginInfo,
    PluginAdapter,
)
from bernstein.core.agents.spawner_core import AgentSpawner

if TYPE_CHECKING:
    from pathlib import Path


class _SamplingCapableAdapter(PluginAdapter):
    """Adapter that declares SUPPORTS_SAMPLING_PARAMS so base_url overrides pass the gate."""

    def __init__(self, name: str = "TestAdapter") -> None:
        super().__init__()
        self._name = name

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name=self._name,
            version="1.0.0",
            capabilities=(AdapterCapability.SUPPORTS_SAMPLING_PARAMS,),
        )

    def health_check(self) -> bool:
        return True

    def supported_models(self) -> list[str]:
        return []

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: object,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        return SpawnResult(pid=4242, log_path=workdir / "stub.log")

    def name(self) -> str:
        return self._name


def _build_spawner(
    tmp_path: Path,
    adapter: PluginAdapter,
    *,
    role_model_policy: dict[str, dict[str, Any]] | None = None,
) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        role_model_policy=role_model_policy,
    )


class TestFreshSpawnEndpointIdentity:
    def test_spawn_records_adapter_model_base_url_profile_name(self, tmp_path: Path, make_task: Any) -> None:
        """A fresh spawn stamps adapter name, model, base_url, and profile_name."""
        adapter = _SamplingCapableAdapter("MockCLI")
        spawner = _build_spawner(
            tmp_path,
            adapter,
            role_model_policy={
                "backend": {
                    "model": "my-model",
                    "base_url": "https://custom.example.com/v1",
                    "endpoint": "my-endpoint-profile",
                }
            },
        )
        task = make_task(role="backend")
        session = spawner.spawn_for_tasks([task])

        assert session.endpoint_adapter_name == "MockCLI"
        assert session.endpoint_model == "my-model"
        assert session.endpoint_base_url == "https://custom.example.com/v1"
        assert session.endpoint_profile_name == "my-endpoint-profile"


class TestResumeEndpointIdentity:
    def test_resume_records_endpoint_identity(self, tmp_path: Path, make_task: Any) -> None:
        """spawn_for_resume stamps the same four fields as a fresh spawn."""
        adapter = _SamplingCapableAdapter("MockCLI")
        spawner = _build_spawner(
            tmp_path,
            adapter,
            role_model_policy={
                "backend": {
                    "model": "resume-model",
                    "base_url": "https://resume.example.com/v1",
                    "endpoint": "resume-endpoint",
                }
            },
        )
        task = make_task(role="backend")
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        session = spawner.spawn_for_resume([task], worktree_path=worktree, changed_files=["a.py"])

        assert session.endpoint_adapter_name == "MockCLI"
        assert session.endpoint_model == "resume-model"
        assert session.endpoint_base_url == "https://resume.example.com/v1"
        assert session.endpoint_profile_name == "resume-endpoint"


class TestPerRoleOverride:
    def test_per_role_override(self, tmp_path: Path, make_task: Any) -> None:
        """A per-role base_url in role_policy is reflected in endpoint_base_url."""
        adapter = _SamplingCapableAdapter("MockCLI")
        spawner = _build_spawner(
            tmp_path,
            adapter,
            role_model_policy={
                "qa": {
                    "model": "qa-model",
                    "base_url": "https://qa.example.com/v1",
                }
            },
        )
        task = make_task(role="qa")
        session = spawner.spawn_for_tasks([task])

        assert session.endpoint_adapter_name == "MockCLI"
        assert session.endpoint_model == "qa-model"
        assert session.endpoint_base_url == "https://qa.example.com/v1"
        assert session.endpoint_profile_name == ""


class TestDefaultNoOverride:
    def test_profile_default_no_override(self, tmp_path: Path, make_task: Any) -> None:
        """When no per-role override is configured, endpoint_base_url is empty."""
        adapter = _SamplingCapableAdapter("MockCLI")
        spawner = _build_spawner(tmp_path, adapter, role_model_policy={})
        task = make_task(role="backend")
        session = spawner.spawn_for_tasks([task])

        assert session.endpoint_adapter_name == "MockCLI"
        assert session.endpoint_model == "mock-model"
        assert session.endpoint_base_url == ""
        assert session.endpoint_profile_name == ""

    def test_resume_default_no_override(self, tmp_path: Path, make_task: Any) -> None:
        """spawn_for_resume with no role override leaves endpoint_base_url empty."""
        adapter = _SamplingCapableAdapter("MockCLI")
        spawner = _build_spawner(tmp_path, adapter, role_model_policy={})
        task = make_task(role="backend")
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        session = spawner.spawn_for_resume([task], worktree_path=worktree, changed_files=[])

        assert session.endpoint_adapter_name == "MockCLI"
        assert session.endpoint_model == "mock-model"
        assert session.endpoint_base_url == ""
        assert session.endpoint_profile_name == ""


class TestWorkflowAgentNodePath:
    def test_workflow_agent_node_records_endpoint_identity(self, tmp_path: Path, make_task: Any) -> None:
        """The agent-typed workflow node path (via spawn_for_tasks) records identity.

        Workflow agent nodes build a Task from the node's role/prompt and call
        ``spawn_for_tasks`` directly, so the same endpoint identity stamping
        applies - verified here with a role that has per-role overrides.
        """
        adapter = _SamplingCapableAdapter("MockCLI")
        spawner = _build_spawner(
            tmp_path,
            adapter,
            role_model_policy={
                "analyst": {
                    "model": "workflow-model",
                    "base_url": "https://workflow.example.com/v1",
                    "endpoint": "workflow-endpoint",
                }
            },
        )
        task = make_task(role="analyst")
        session = spawner.spawn_for_tasks([task])

        assert session.endpoint_adapter_name == "MockCLI"
        assert session.endpoint_model == "workflow-model"
        assert session.endpoint_base_url == "https://workflow.example.com/v1"
        assert session.endpoint_profile_name == "workflow-endpoint"
