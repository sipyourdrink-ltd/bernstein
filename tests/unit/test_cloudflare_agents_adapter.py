"""Unit tests for CloudflareAgentsAdapter.

The adapter is experimental and non-functional (issue #2782): it has no
worker-trigger path, so ``spawn()`` refuses with a clear error instead of
launching a ``npx wrangler dev`` server that can never complete a task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.cloudflare_agents import CloudflareAgentsAdapter

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# CloudflareAgentsAdapter.name()
# ---------------------------------------------------------------------------


class TestCloudflareAdapterName:
    def test_name(self) -> None:
        assert CloudflareAgentsAdapter().name() == "Cloudflare Agents"


# ---------------------------------------------------------------------------
# spawn() refuses: the adapter has no worker-trigger path (issue #2782)
# ---------------------------------------------------------------------------


class TestCloudflareSpawnRefuses:
    def test_spawn_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        with pytest.raises(RuntimeError, match="experimental"):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-refuse",
            )

    def test_refusal_message_is_actionable(self, tmp_path: Path) -> None:
        adapter = CloudflareAgentsAdapter()
        with pytest.raises(RuntimeError) as exc:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-refuse-2",
            )
        message = str(exc.value)
        # Names the tracking issue and at least one working alternative.
        assert "#2782" in message
        assert "CloudflareBridge" in message

    def test_spawn_does_not_create_runtime_dirs(self, tmp_path: Path) -> None:
        """Refusal happens before any filesystem side effects."""
        adapter = CloudflareAgentsAdapter()
        with pytest.raises(RuntimeError):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-refuse-3",
            )
        assert not (tmp_path / ".sdd" / "runtime").exists()


# ---------------------------------------------------------------------------
# Multimodal refusal takes precedence over the unavailable error
# ---------------------------------------------------------------------------


class TestCloudflareMultimodalRefusal:
    def test_multimodal_context_refused_first(self, tmp_path: Path) -> None:
        from bernstein.core.agents.multimodal import (
            ModalityType,
            MultiModalContext,
            MultiModalInput,
        )
        from bernstein.core.agents.multimodal_attestation import CapabilityRefusal

        adapter = CloudflareAgentsAdapter()
        attachment = tmp_path / "shot.png"
        attachment.write_bytes(b"fake image")
        context = MultiModalContext(
            inputs=(
                MultiModalInput(
                    modality=ModalityType.IMAGE,
                    content_path=attachment,
                    mime_type="image/png",
                    description="screenshot",
                ),
            ),
            primary_modality=ModalityType.IMAGE,
        )
        with pytest.raises(CapabilityRefusal):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cf-mm",
                multimodal_context=context,
            )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestCloudflareRegistry:
    def test_registered_in_adapter_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter("cloudflare")
        assert adapter.name() == "Cloudflare Agents"


# ---------------------------------------------------------------------------
# is_alive() and kill() - inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestCloudflareIsAlive:
    def test_true_when_process_exists(self) -> None:
        from unittest.mock import patch

        adapter = CloudflareAgentsAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)


class TestCloudflareKill:
    def test_calls_killpg(self) -> None:
        from unittest.mock import patch

        adapter = CloudflareAgentsAdapter()
        with patch("bernstein.adapters.base.reap_process_group") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555)
