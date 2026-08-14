"""Spawn-path wiring for operator attachments (issue #1797, #3555).

``docs/operations/run.md`` promises that an attached image is stored in
CAS, recorded as a ``multimodal.attach`` audit-chain event, pinned to the
attaching worktree, and named by the produced artefact's signed lineage
entry.

The primitives for all of that live in
:mod:`bernstein.core.agents.multimodal_attestation`, but nothing called
them: ``--attach`` set an env var that no reader consumed and
``Task.attachments`` was parsed and then dropped, so a run produced no
bytes, no event, and no lineage record. These tests drive the spawner
itself rather than the primitives, so the wiring -- not just the
building blocks -- is what is under test.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.agents.attachment_dispatch import AttachmentDispatchError
from bernstein.core.agents.multimodal_attestation import WorktreeAccessDenied
from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.security.audit_chain import (
    EVENT_MULTIMODAL_ATTACH,
    AuditChainStore,
)

if TYPE_CHECKING:
    from bernstein.core.tasks.models import ModelConfig

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"attachment-under-test"


class RecordingAdapter(CLIAdapter):
    """Stub adapter with a real ``spawn`` signature.

    ``MagicMock(spec=CLIAdapter)`` erases the signature to ``(*args,
    **kwargs)``, which is exactly what the spawner introspects to decide
    whether an adapter can carry attachments. A real subclass is
    therefore required to exercise the capable-adapter path.
    """

    default_model = "sonnet"

    def __init__(self) -> None:
        super().__init__()
        self.captured_multimodal: Any = None
        self.captured_prompt: str = ""
        self.spawn_calls: int = 0

    def name(self) -> str:
        return "claude"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 3600,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        self.spawn_calls += 1
        self.captured_prompt = prompt
        self.captured_multimodal = multimodal_context
        return SpawnResult(pid=4242, log_path=workdir / "agent.log")


class IncapableAdapter(RecordingAdapter):
    """Adapter whose ``spawn`` cannot carry attachments at all."""

    def name(self) -> str:
        return "aider"

    def spawn(  # type: ignore[override]
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 3600,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        self.spawn_calls += 1
        return SpawnResult(pid=4243, log_path=workdir / "agent.log")


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    """Minimal role template tree the spawner can render from."""
    roles = tmp_path / "templates" / "roles"
    (roles / "backend").mkdir(parents=True)
    (roles / "backend" / "config.yaml").write_text("default_model: sonnet\ndefault_effort: medium\n")
    (roles / "backend" / "prompt.md").write_text("You are a backend agent.\n")
    return roles


@pytest.fixture
def attachment(tmp_path: Path) -> Path:
    """An on-disk PNG the operator would pass to ``--attach``."""
    path = tmp_path / "screenshot.png"
    path.write_bytes(PNG_BYTES)
    return path


def _digest() -> str:
    return hashlib.sha256(PNG_BYTES).hexdigest()


class TestSpawnPathConsumesAttachments:
    """The documented provenance must be produced by an actual spawn."""

    def test_task_attachments_reach_the_adapter(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """A task declaring attachments hands the adapter a multimodal context."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]

        spawner.spawn_for_tasks([task])

        assert adapter.spawn_calls == 1
        ctx = adapter.captured_multimodal
        assert ctx is not None, "spawner dropped the declared attachments"
        assert [Path(str(i.content_path)).name for i in ctx.inputs] == ["screenshot.png"]

    def test_spawn_stores_bytes_in_cas(self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task) -> None:
        """Attachment bytes land in ``.sdd/cas/`` addressed by SHA-256."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]
        spawner.spawn_for_tasks([task])

        cas = CASStore(tmp_path / ".sdd" / "cas")
        assert cas.get(_digest()) == PNG_BYTES

    def test_spawn_records_multimodal_attach_event(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """A ``multimodal.attach`` event is appended to the audit chain."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]
        spawner.spawn_for_tasks([task])

        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        events = chain.query(event_type=EVENT_MULTIMODAL_ATTACH)
        assert len(events) == 1
        assert events[0].details["sha256"] == _digest()
        assert events[0].details["worktree_id"], "attach event carries no worktree pin"

    def test_run_level_attach_env_var_is_consumed(
        self,
        tmp_path: Path,
        templates_dir: Path,
        attachment: Path,
        make_task,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``bernstein run --attach`` reaches the spawn path via its env var."""
        monkeypatch.setenv("BERNSTEIN_RUN_ATTACHMENTS", str(attachment))
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        spawner.spawn_for_tasks([make_task(role="backend")])

        ctx = adapter.captured_multimodal
        assert ctx is not None, "--attach env var was never read by the spawn path"
        assert len(ctx.inputs) == 1

    def test_no_attachments_leaves_spawn_untouched(self, tmp_path: Path, templates_dir: Path, make_task) -> None:
        """A run without attachments passes no context and writes no CAS/chain."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        spawner.spawn_for_tasks([make_task(role="backend")])

        assert adapter.captured_multimodal is None
        assert not (tmp_path / ".sdd" / "cas").exists()


class TestLineagePinning:
    """Digests are stamped so the artefact receipt can carry them."""

    def test_spawn_stamps_digests_for_lineage(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """The resolved digests are recorded on the task for the lineage receipt."""
        from bernstein.core.agents.attachment_dispatch import attachment_digests_for_tasks

        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]
        spawner.spawn_for_tasks([task])

        assert attachment_digests_for_tasks([task]) == [_digest()]


class TestWorktreePinSurvivesTheWiring:
    """The pin the docs promise must hold against a real recorded event."""

    def test_cross_worktree_resolve_is_denied(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """A worker in another worktree cannot resolve the attached bytes."""
        from bernstein.core.agents.multimodal_attestation import resolve_attachment_for_worker

        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]
        spawner.spawn_for_tasks([task])

        with pytest.raises(WorktreeAccessDenied):
            resolve_attachment_for_worker(
                sha256=_digest(),
                requesting_worktree_id="some-other-worktree",
                cas=CASStore(tmp_path / ".sdd" / "cas"),
                audit_chain=AuditChainStore(tmp_path / ".sdd" / "audit"),
            )


class TestResumeResolvesThroughThePin:
    """A resumed worker rebuilds its attachments from attested bytes."""

    def test_resume_rebuilds_context_from_cas(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """Resume re-sends the attached bytes without re-reading the file."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]
        spawner.spawn_for_tasks([task])

        # The operator's file is gone by the time the crashed worker resumes;
        # CAS is the only remaining source of the bytes the chain attests.
        attachment.unlink()

        spawner.spawn_for_resume([task], worktree_path=tmp_path, changed_files=[])

        ctx = adapter.captured_multimodal
        assert ctx is not None, "resume dropped the attachments"
        replayed = base64.b64decode(str(ctx.inputs[0].content_base64))
        assert replayed == PNG_BYTES

    def test_resume_in_another_worktree_is_refused(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """The worktree pin is enforced on the resume read, not bypassed."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]
        spawner.spawn_for_tasks([task])

        foreign = tmp_path / "other-worktree"
        foreign.mkdir()
        with pytest.raises(AttachmentDispatchError, match="could not resolve attachment"):
            spawner.spawn_for_resume([task], worktree_path=foreign, changed_files=[])


class TestRefusalsInsteadOfSilentDrops:
    """Every path that cannot carry attachments says so."""

    def test_missing_declared_attachment_refuses(self, tmp_path: Path, templates_dir: Path, make_task) -> None:
        """A plan-declared path that does not exist aborts the spawn."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(tmp_path / "nope.png")]

        with pytest.raises(AttachmentDispatchError, match="not found"):
            spawner.spawn_for_tasks([task])
        assert adapter.spawn_calls == 0

    def test_adapter_without_the_parameter_refuses(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """An adapter whose spawn() cannot take a context never silently drops it."""
        adapter = IncapableAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]

        with pytest.raises(RuntimeError, match="cannot carry attachments"):
            spawner.spawn_for_tasks([task])

    def test_container_isolation_refuses_before_writing_anything(
        self, tmp_path: Path, templates_dir: Path, attachment: Path, make_task
    ) -> None:
        """Container mode refuses, and leaves no orphan blob or attach event."""
        adapter = RecordingAdapter()
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)  # type: ignore[arg-type]
        spawner._container_mgr = object()  # type: ignore[assignment]

        task = make_task(role="backend")
        task.attachments = [str(attachment)]

        with pytest.raises(AttachmentDispatchError, match="not supported when agents run via"):
            spawner.spawn_for_tasks([task])
        assert not (tmp_path / ".sdd" / "cas").exists()
        assert adapter.spawn_calls == 0
