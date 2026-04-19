"""Unit tests for the sandbox backend registry (oai-002)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.sandbox import (
    SandboxCapability,
)
from bernstein.core.sandbox import registry as sandbox_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.sandbox.backend import SandboxSession
    from bernstein.core.sandbox.manifest import WorkspaceManifest


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    """Each test gets a pristine default registry."""
    sandbox_registry._reset_for_tests()
    yield
    sandbox_registry._reset_for_tests()


def test_default_registry_discovers_builtins() -> None:
    """The default registry lists ``worktree`` and ``docker``."""
    names = sandbox_registry.list_backend_names()
    assert "worktree" in names
    assert "docker" in names


def test_get_backend_returns_instance() -> None:
    """``get_backend`` instantiates the class lazily."""
    backend = sandbox_registry.get_backend("worktree")
    assert backend.name == "worktree"
    assert SandboxCapability.FILE_RW in backend.capabilities


def test_get_backend_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError) as excinfo:
        sandbox_registry.get_backend("nonexistent")
    # Error message lists available names.
    assert "Available" in str(excinfo.value)


def test_duplicate_registration_rejected() -> None:
    class _Stub:
        name = "worktree"
        capabilities = frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC})

        async def create(self, manifest: WorkspaceManifest, options: dict[str, Any] | None = None) -> SandboxSession:
            raise NotImplementedError

        async def resume(self, snapshot_id: str) -> SandboxSession:
            raise NotImplementedError

        async def destroy(self, session: SandboxSession) -> None:  # pragma: no cover
            raise NotImplementedError

    # Force builtin load so the name is already taken.
    sandbox_registry.list_backend_names()
    with pytest.raises(ValueError, match="Duplicate"):
        sandbox_registry.register_backend("worktree", _Stub())


def test_empty_name_rejected() -> None:
    class _Stub:
        name = "x"
        capabilities = frozenset({SandboxCapability.FILE_RW})

        async def create(self, manifest: WorkspaceManifest, options: dict[str, Any] | None = None) -> SandboxSession:
            raise NotImplementedError

        async def resume(self, snapshot_id: str) -> SandboxSession:
            raise NotImplementedError

        async def destroy(self, session: SandboxSession) -> None:
            raise NotImplementedError

    with pytest.raises(ValueError, match="non-empty"):
        sandbox_registry.register_backend("  ", _Stub())


def test_custom_backend_registration_round_trip() -> None:
    class _Custom:
        name = "custom"
        capabilities = frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC})

        async def create(
            self, manifest: WorkspaceManifest, options: dict[str, Any] | None = None
        ) -> SandboxSession:  # pragma: no cover - not invoked here
            raise NotImplementedError

        async def resume(self, snapshot_id: str) -> SandboxSession:  # pragma: no cover
            raise NotImplementedError

        async def destroy(self, session: SandboxSession) -> None:  # pragma: no cover
            raise NotImplementedError

    instance = _Custom()
    sandbox_registry.register_backend("custom", instance)
    assert sandbox_registry.get_backend("custom") is instance
    assert "custom" in sandbox_registry.list_backend_names()


def test_list_backends_instantiates_factories() -> None:
    """``list_backends`` returns instances for every registered factory."""
    backends = sandbox_registry.list_backends()
    names = [b.name for b in backends]
    assert "worktree" in names
    assert "docker" in names


def test_entry_point_failure_does_not_break_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving entry-point must not block built-in backends."""

    class _FakeEntryPoint:
        name = "broken"

        def load(self) -> object:
            raise RuntimeError("boom")

    def _fake_entry_points(group: str = "") -> list[_FakeEntryPoint]:
        if group == "bernstein.sandbox_backends":
            return [_FakeEntryPoint()]
        return []

    monkeypatch.setattr(sandbox_registry, "entry_points", _fake_entry_points)
    # No exception — the registry skips the broken entry-point.
    names = sandbox_registry.list_backend_names()
    assert "worktree" in names
    assert "broken" not in names


def test_entry_point_class_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entry-points can load classes, not just instances."""

    class _ExtBackend:
        name = "ext"
        capabilities = frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC})

        async def create(
            self, manifest: WorkspaceManifest, options: dict[str, Any] | None = None
        ) -> SandboxSession:  # pragma: no cover
            raise NotImplementedError

        async def resume(self, snapshot_id: str) -> SandboxSession:  # pragma: no cover
            raise NotImplementedError

        async def destroy(self, session: SandboxSession) -> None:  # pragma: no cover
            raise NotImplementedError

    class _FakeEntryPoint:
        name = "ext"

        def load(self) -> type[_ExtBackend]:
            return _ExtBackend

    def _fake_entry_points(group: str = "") -> list[_FakeEntryPoint]:
        if group == "bernstein.sandbox_backends":
            return [_FakeEntryPoint()]
        return []

    monkeypatch.setattr(sandbox_registry, "entry_points", _fake_entry_points)
    assert "ext" in sandbox_registry.list_backend_names()
    ext = sandbox_registry.get_backend("ext")
    assert isinstance(ext, _ExtBackend)


def test_unregister_removes_name() -> None:
    reg = sandbox_registry.default_registry()
    reg.list_names()  # ensure builtins loaded
    reg.unregister("docker")
    assert "docker" not in reg.list_names()
