"""Task identity must survive the spawn path to adapters that accept it.

An adapter can brand its output per task — select behaviour from
``task_title``, stamp ``task_id`` into its log — but only if the spawner
forwards the identity. Forwarding is gated on the adapter's ``spawn()``
signature, and production always wraps the adapter in ``CachingAdapter``,
so the wrapper must relay the identity to the inner adapter: otherwise the
gate inspects the wrapper's signature and the identity silently never
arrives (the same shape as the heartbeat_dir regression in
``test_spawner.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import SpawnResult
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    AdapterPluginInfo,
    PluginAdapter,
)

if TYPE_CHECKING:
    from pathlib import Path


class _IdentityAwareAdapter(PluginAdapter):
    """Adapter whose spawn() accepts task identity and records it."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_task_id: str | None = None
        self.seen_task_title: str | None = None

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name="identity-aware",
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
        task_id: str = "",
        task_title: str = "",
    ) -> SpawnResult:
        self.seen_task_id = task_id
        self.seen_task_title = task_title
        return SpawnResult(pid=4242, log_path=workdir / "stub.log")

    def name(self) -> str:
        return "identity-aware"


class _IdentityBlindAdapter(PluginAdapter):
    """Adapter whose spawn() does not know about task identity."""

    def __init__(self) -> None:
        super().__init__()
        self.spawn_calls = 0

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name="identity-blind",
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
        self.spawn_calls += 1
        return SpawnResult(pid=4242, log_path=workdir / "stub.log")

    def name(self) -> str:
        return "identity-blind"


def _make_spawner(adapter: PluginAdapter, tmp_path: Path, **kwargs: Any) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        **kwargs,
    )


class TestTaskIdentitySpawnPath:
    def test_task_identity_reaches_capable_adapter(self, tmp_path: Path, make_task) -> None:
        """spawn() must receive the task's id and title, not defaults.

        Fails before the spawner forwards identity: the adapter's recorded
        values stay empty and every downstream per-task branch degrades to
        its unknown-task fallback.
        """
        adapter = _IdentityAwareAdapter()
        spawner = _make_spawner(adapter, tmp_path)

        spawner.spawn_for_tasks([make_task(id="T-042", title="Fix off-by-one in get_item route")])

        assert adapter.seen_task_id == "T-042"
        assert adapter.seen_task_title == "Fix off-by-one in get_item route"

    def test_task_identity_survives_caching_wrapper(self, tmp_path: Path, make_task) -> None:
        """Production wraps every adapter in CachingAdapter; the identity
        must be relayed through the wrapper to the inner adapter."""
        adapter = _IdentityAwareAdapter()
        spawner = _make_spawner(adapter, tmp_path, enable_caching=True)

        spawner.spawn_for_tasks([make_task(id="T-777", title="Fix health endpoint returns 201 instead of 200")])

        assert adapter.seen_task_id == "T-777"
        assert adapter.seen_task_title == "Fix health endpoint returns 201 instead of 200"

    def test_identity_blind_adapter_spawns_clean(self, tmp_path: Path, make_task) -> None:
        """Adapters without the parameters must not receive them at all."""
        adapter = _IdentityBlindAdapter()
        spawner = _make_spawner(adapter, tmp_path)

        spawner.spawn_for_tasks([make_task()])

        assert adapter.spawn_calls == 1
