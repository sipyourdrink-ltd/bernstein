"""The spawner hands the host-isolation declaration to adapters that ask (#5341).

Only an adapter that owns a vendor sandbox consumes the declaration, and the
hand-off is anchored in the HMAC audit chain: dropping a vendor sandbox is a
posture change, so it has to be a signed record rather than a silent
constructor argument. Adapters without the marker are left untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.capability_profile import SandboxTier
from bernstein.adapters.codex import CodexAdapter
from bernstein.core.security.audit_chain import EVENT_HOST_ISOLATION_DECLARED, AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _declared_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_TIER", "container")
    monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_EVIDENCE", "read-only rootfs, cap-drop ALL")


def _spawner(tmp_path: Path) -> AgentSpawner:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.name.return_value = "MockCLI"
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    return AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False, default_model="mock-model")


def _events(tmp_path: Path) -> list:
    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    return chain.query(event_type=EVENT_HOST_ISOLATION_DECLARED)


def test_consuming_adapter_receives_the_declaration(tmp_path: Path) -> None:
    spawner = _spawner(tmp_path)

    with patch("bernstein.core.agents.spawner_core.get_adapter", return_value=CodexAdapter()):
        adapter = spawner._get_adapter_by_name("codex")  # type: ignore[reportPrivateUsage]

    assert adapter.host_isolation is SandboxTier.CONTAINER
    assert adapter.host_isolation_evidence == "read-only rootfs, cap-drop ALL"


def test_declaration_is_anchored_in_the_audit_chain(tmp_path: Path) -> None:
    spawner = _spawner(tmp_path)
    spawner.set_run_id("run-5341")

    with patch("bernstein.core.agents.spawner_core.get_adapter", return_value=CodexAdapter()):
        spawner._get_adapter_by_name("codex")  # type: ignore[reportPrivateUsage]

    events = _events(tmp_path)
    assert len(events) == 1
    details = events[0].details
    assert details["adapter"] == "codex"
    assert details["tier"] == "container"
    assert details["evidence"] == "read-only rootfs, cap-drop ALL"
    assert details["vendor_sandbox_dropped"] is True
    assert details["source"] == "session"
    assert details["run_id"] == "run-5341"


def test_declaration_is_recorded_once_per_adapter(tmp_path: Path) -> None:
    """The adapter cache makes the second lookup a cache hit, not a second record."""
    spawner = _spawner(tmp_path)

    with patch("bernstein.core.agents.spawner_core.get_adapter", return_value=CodexAdapter()):
        first = spawner._get_adapter_by_name("codex")  # type: ignore[reportPrivateUsage]
        second = spawner._get_adapter_by_name("codex")  # type: ignore[reportPrivateUsage]

    assert first is second
    assert len(_events(tmp_path)) == 1


def test_weak_tier_records_that_the_vendor_sandbox_stayed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_TIER", "process")
    spawner = _spawner(tmp_path)

    with patch("bernstein.core.agents.spawner_core.get_adapter", return_value=CodexAdapter()):
        adapter = spawner._get_adapter_by_name("codex")  # type: ignore[reportPrivateUsage]

    assert adapter.host_isolation is SandboxTier.PROCESS
    events = _events(tmp_path)
    assert len(events) == 1
    assert events[0].details["vendor_sandbox_dropped"] is False


def test_adapter_without_the_marker_is_untouched(tmp_path: Path) -> None:
    """qwen has no vendor sandbox to drop, so nothing is injected and nothing recorded."""
    plain = MagicMock(spec=CLIAdapter)
    plain.name.return_value = "Qwen"
    spawner = _spawner(tmp_path)

    with patch("bernstein.core.agents.spawner_core.get_adapter", return_value=plain):
        adapter = spawner._get_adapter_by_name("qwen")  # type: ignore[reportPrivateUsage]

    assert not hasattr(adapter, "host_isolation")
    assert _events(tmp_path) == []


def test_a_broken_declaration_does_not_wedge_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A misdeclared tier is loud, but the adapter still spawns sandboxed."""
    monkeypatch.setenv("BERNSTEIN_HOST_ISOLATION_TIER", "kubernetes")
    spawner = _spawner(tmp_path)

    with (
        patch("bernstein.core.agents.spawner_core.get_adapter", return_value=CodexAdapter()),
        caplog.at_level("WARNING", logger="bernstein.core.agents.spawner_core"),
    ):
        adapter = spawner._get_adapter_by_name("codex")  # type: ignore[reportPrivateUsage]

    assert adapter.host_isolation == SandboxTier.NONE
    assert any("kubernetes" in record.getMessage() for record in caplog.records)
    assert _events(tmp_path) == []
