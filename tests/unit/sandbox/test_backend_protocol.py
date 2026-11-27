"""Protocol-level tests for SandboxBackend/SandboxSession (oai-002).

The conformance suite itself lives in :mod:`bernstein.core.sandbox.conformance`;
it is parametrised so each backend simply subclasses and supplies a fixture.

This file exercises:

1. The protocol contract (runtime_checkable Protocol matches our concrete
   classes).
2. :class:`SandboxBackendConformance` against the worktree backend. Docker
   /E2B/Modal conformance lives under ``tests/integration/sandbox/`` and
   is gated by environment/daemon availability.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from bernstein.core.sandbox import (
    ExecResult,
    SandboxBackend,
    SandboxCapability,
    SandboxSession,
    WorkspaceManifest,
)
from bernstein.core.sandbox.backends.worktree import WorktreeSandboxBackend
from bernstein.core.sandbox.conformance import SandboxBackendConformance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _init_git_repo(path: Path) -> None:
    """Initialise a throwaway git repo for worktree-backend tests."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=bernstein",
            "-c",
            "user.email=bernstein@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
    )


def test_protocol_runtime_checkable() -> None:
    """WorktreeSandboxBackend satisfies the runtime-checkable protocol."""
    backend = WorktreeSandboxBackend()
    assert isinstance(backend, SandboxBackend)


def test_removal_contract_structure_and_values() -> None:
    """Check the optional protocol signatures and production value objects."""
    from dataclasses import FrozenInstanceError, replace
    from inspect import Parameter, iscoroutinefunction, signature
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from bernstein.core import sandbox
    from bernstein.core.sandbox import backend as contract

    for name in (
        "SandboxRemovalBackend",
        "SandboxReattachStatus",
        "SandboxRemovalStatus",
        "SandboxReattachResult",
        "SandboxRemovalResult",
    ):
        assert hasattr(contract, name), f"missing optional removal contract: {name}"
        assert getattr(sandbox, name) is getattr(contract, name)
        assert name in sandbox.__all__
    attach_status = contract.SandboxReattachStatus
    removal_status = contract.SandboxRemovalStatus
    assert [s.value for s in attach_status] == ["attached", "unsupported", "not_found", "identity_mismatch", "error"]
    assert [s.value for s in removal_status] == ["removed", "not_found", "identity_mismatch", "error"]
    assert SandboxSession.removal_evidence is None
    legacy = WorktreeSandboxBackend()
    assert isinstance(legacy, SandboxBackend)
    assert not isinstance(legacy, contract.SandboxRemovalBackend)

    # Structural detection must require both optional methods, independently
    # of the required create/resume/destroy protocol.
    backend = SimpleNamespace(reattach_for_removal=AsyncMock(), destroy_for_removal=AsyncMock())
    assert isinstance(backend, contract.SandboxRemovalBackend)
    assert not isinstance(backend, SandboxBackend)
    for missing in ("reattach_for_removal", "destroy_for_removal"):
        partial = SimpleNamespace(**{name: value for name, value in vars(backend).items() if name != missing})
        assert not isinstance(partial, contract.SandboxRemovalBackend)

    for method_name, argument, result_type in (
        ("reattach_for_removal", "session_id", "SandboxReattachResult"),
        ("destroy_for_removal", "session", "SandboxRemovalResult"),
    ):
        method = getattr(contract.SandboxRemovalBackend, method_name)
        assert iscoroutinefunction(method)
        sig = signature(method)
        assert list(sig.parameters) == ["self", argument, "evidence"]
        assert sig.parameters["evidence"].kind is Parameter.KEYWORD_ONLY
        assert sig.parameters["evidence"].default is Parameter.empty
        assert sig.return_annotation == result_type

    # These constructors are the enforcement implemented in this slice.
    # Check every status/handle combination, including explicit None.
    session = Mock(spec=SandboxSession)
    attached = contract.SandboxReattachResult(attach_status.ATTACHED, session)
    assert attached.status is attach_status.ATTACHED
    assert attached.session is session
    assert attached.detail == ""
    with pytest.raises(ValueError, match="requires and permits a session"):
        contract.SandboxReattachResult(attach_status.ATTACHED)
    for status in attach_status:
        with pytest.raises(ValueError, match="requires and permits a session"):
            contract.SandboxReattachResult(status, None if status == attach_status.ATTACHED else session)
        if status is attach_status.ATTACHED:
            continue
        result = contract.SandboxReattachResult(status)
        assert result.status is status
        assert result.session is None
        assert result.detail == ""
        with pytest.raises(ValueError, match="requires and permits a session"):
            replace(attached, status=status)
        for field, value in (("status", attach_status.ATTACHED), ("session", session), ("detail", "changed")):
            with pytest.raises(FrozenInstanceError):
                setattr(result, field, value)
    for field, value in (("status", attach_status.ERROR), ("session", None), ("detail", "changed")):
        with pytest.raises(FrozenInstanceError):
            setattr(attached, field, value)
    assert replace(attached, detail="verified").detail == "verified"

    for status in removal_status:
        result = contract.SandboxRemovalResult(status)
        assert result.status is status
        assert result.detail == ""
        for field, value in (("status", removal_status.REMOVED), ("detail", "changed")):
            with pytest.raises(FrozenInstanceError):
                setattr(result, field, value)
        assert replace(result, detail="provider detail").detail == "provider detail"

    # Raw strings (even known values) and the other enum must not bypass the
    # constructors' enum-type validation through StrEnum string equality.
    for invalid in ("unknown", None, *[s.value for s in attach_status], *removal_status):
        with pytest.raises(ValueError, match="status must be a SandboxReattachStatus"):
            contract.SandboxReattachResult(invalid, session)
    for invalid in ("unknown", None, *[s.value for s in removal_status], *attach_status):
        with pytest.raises(ValueError, match="status must be a SandboxRemovalStatus"):
            contract.SandboxRemovalResult(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "fault", "expected"),
    [
        ("reattach", "none", "attached"),
        ("reattach", "unsupported", "unsupported"),
        ("reattach", "missing", "not_found"),
        ("reattach", "wrong_id", "identity_mismatch"),
        ("reattach", "wrong_evidence", "identity_mismatch"),
        ("reattach", "error", "error"),
        ("reattach", "exception", None),
        ("reattach", "invalid_result", None),
        ("destroy", "none", "removed"),
        ("destroy", "missing", "not_found"),
        ("destroy", "wrong_id", "identity_mismatch"),
        ("destroy", "wrong_evidence", "identity_mismatch"),
        ("destroy", "changed_identity", "identity_mismatch"),
        ("destroy", "replaced", "identity_mismatch"),
        ("destroy", "error", "error"),
        ("destroy", "exception", None),
        ("destroy", "invalid_result", None),
        *[
            (op, fault, "error")
            for op in ("reattach", "destroy")
            for fault in ("null_evidence", "empty_evidence", "list_evidence", "numeric_value", "numeric_key")
        ],
    ],
)
async def test_removal_reattach_contract(operation: str, fault: str, expected: str | None) -> None:
    """Exercise optional methods with an identity-aware, in-memory backend.

    The fake models provider state; all returned values and invalid-result
    rejection go through the production contract, without a provider or CLI.
    """
    from collections.abc import Mapping
    from unittest.mock import AsyncMock, Mock

    from bernstein.core.sandbox import backend as contract

    for name in (
        "SandboxRemovalBackend",
        "SandboxReattachStatus",
        "SandboxRemovalStatus",
        "SandboxReattachResult",
        "SandboxRemovalResult",
    ):
        assert hasattr(contract, name), f"missing optional removal contract: {name}"
    attach_status = contract.SandboxReattachStatus
    removal_status = contract.SandboxRemovalStatus
    session = Mock(spec=SandboxSession, session_id="existing", removal_evidence={"identity": "original"})

    class OptionalBackend(WorktreeSandboxBackend):
        def __init__(self):
            super().__init__()
            self.resources = {session.session_id: session}
            self.supported = True
            self.failure = None

        @staticmethod
        def valid_evidence(evidence):
            return (
                isinstance(evidence, Mapping)
                and set(evidence) == {"identity"}
                and isinstance(evidence["identity"], str)
                and bool(evidence["identity"])
            )

        async def reattach_for_removal(self, session_id, *, evidence):
            if self.failure == "exception":
                raise RuntimeError("provider lookup failed")
            if self.failure == "invalid_result":
                return contract.SandboxReattachResult(attach_status.ERROR, session)
            if not self.supported:
                return contract.SandboxReattachResult(attach_status.UNSUPPORTED)
            if self.failure == "error" or not self.valid_evidence(evidence):
                return contract.SandboxReattachResult(attach_status.ERROR)
            resource = self.resources.get(session_id)
            if resource is None:
                return contract.SandboxReattachResult(attach_status.NOT_FOUND)
            if resource.session_id != session_id or resource.removal_evidence != evidence:
                return contract.SandboxReattachResult(attach_status.IDENTITY_MISMATCH)
            return contract.SandboxReattachResult(attach_status.ATTACHED, resource)

        async def destroy_for_removal(self, handle, *, evidence):
            if self.failure == "exception":
                raise RuntimeError("provider removal failed")
            if self.failure == "invalid_result":
                return contract.SandboxRemovalResult("removed")
            if self.failure == "error" or not self.valid_evidence(evidence):
                return contract.SandboxRemovalResult(removal_status.ERROR)
            resource = self.resources.get(handle.session_id)
            if resource is None:
                return contract.SandboxRemovalResult(removal_status.NOT_FOUND)
            if (
                resource is not handle
                or resource.session_id != handle.session_id
                or resource.removal_evidence != evidence
            ):
                return contract.SandboxRemovalResult(removal_status.IDENTITY_MISMATCH)
            del self.resources[handle.session_id]
            return contract.SandboxRemovalResult(removal_status.REMOVED)

    backend = OptionalBackend()
    backend.create = AsyncMock(side_effect=AssertionError("must not create"))
    backend.resume = AsyncMock(side_effect=AssertionError("must not resume"))
    backend.destroy = AsyncMock(side_effect=AssertionError("must use explicit removal outcome"))
    assert isinstance(backend, SandboxBackend)
    assert isinstance(backend, contract.SandboxRemovalBackend)
    evidence = {"identity": "original"}
    handle = None
    if operation == "destroy":
        attached = await backend.reattach_for_removal("existing", evidence=evidence)
        assert attached.status is attach_status.ATTACHED
        assert attached.session is session
        assert attached.session.session_id == "existing"
        assert attached.session.removal_evidence == evidence
        handle = attached.session

    # Change provider state *after* attachment to exercise the removal recheck.
    if fault == "missing":
        backend.resources.clear()
    elif fault == "wrong_id":
        if operation == "reattach":
            session.session_id = "different"
        else:
            backend.resources["existing"] = Mock(
                spec=SandboxSession, session_id="different", removal_evidence={"identity": "original"}
            )
    elif fault == "wrong_evidence":
        evidence = {"identity": "different"}
    elif fault == "changed_identity":
        session.removal_evidence = {"identity": "replacement"}
    elif fault == "replaced":
        backend.resources["existing"] = Mock(
            spec=SandboxSession, session_id="existing", removal_evidence={"identity": "replacement"}
        )
    elif fault == "unsupported":
        backend.supported = False
    elif fault in ("error", "exception", "invalid_result"):
        backend.failure = fault
    else:
        evidence = {
            "none": evidence,
            "null_evidence": None,
            "empty_evidence": {},
            "list_evidence": ["original"],
            "numeric_value": {"identity": 1},
            "numeric_key": {1: "original"},
        }[fault]

    before = dict(backend.resources)
    result = None
    try:
        call = (
            backend.reattach_for_removal("existing", evidence=evidence)
            if operation == "reattach"
            else backend.destroy_for_removal(handle, evidence=evidence)
        )
        if fault in ("exception", "invalid_result"):
            exception = RuntimeError if fault == "exception" else ValueError
            message = "provider" if fault == "exception" else "requires and permits a session|status must be"
            with pytest.raises(exception, match=message):
                result = await call
            assert result is None
        else:
            result = await call
            assert result.status.value == expected
            if operation == "reattach":
                assert result.session is (session if expected == "attached" else None)
            else:
                assert (result.status is removal_status.REMOVED) is (expected == "removed")
        assert backend.resources == ({} if expected == "removed" else before)
    finally:
        backend.create.assert_not_called()
        backend.resume.assert_not_called()
        backend.destroy.assert_not_called()


def test_capabilities_worktree_shape() -> None:
    """Worktree backend exposes FILE_RW + EXEC + NETWORK + SNAPSHOT.

    Snapshot support landed for the worktree backend in issue #2295
    (git-commit snapshot primitive), so SNAPSHOT is now declared. Cloud
    backends still omit it.
    """
    backend = WorktreeSandboxBackend()
    assert SandboxCapability.FILE_RW in backend.capabilities
    assert SandboxCapability.EXEC in backend.capabilities
    assert SandboxCapability.NETWORK in backend.capabilities
    assert SandboxCapability.SNAPSHOT in backend.capabilities


def test_exec_result_dataclass_is_frozen() -> None:
    """ExecResult is a frozen dataclass (value object semantics)."""
    from dataclasses import FrozenInstanceError

    result = ExecResult(exit_code=0, stdout=b"hi", stderr=b"", duration_seconds=0.1)
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]


def test_sandbox_session_is_abstract() -> None:
    """SandboxSession cannot be instantiated directly (abstract base)."""
    with pytest.raises(TypeError):
        SandboxSession()  # type: ignore[abstract]


class TestWorktreeConformance(SandboxBackendConformance):
    """Run the full conformance suite against the worktree backend."""

    @pytest_asyncio.fixture
    async def backend(
        self,
        tmp_path: Path,
    ) -> AsyncIterator[SandboxBackend]:
        _init_git_repo(tmp_path)
        backend = WorktreeSandboxBackend()
        yield backend

    @pytest.fixture
    def manifest(self, tmp_path: Path) -> WorkspaceManifest:
        return WorkspaceManifest(
            root=str(tmp_path),
            env={"LC_ALL": "C"},
            timeout_seconds=60,
        )

    @pytest.mark.asyncio
    async def test_read_write_roundtrip_utf8(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            payload = "hello world - ✓".encode()
            await session.write("hello.txt", payload)
            got = await session.read("hello.txt")
            assert got == payload
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_read_write_roundtrip_binary(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            payload = bytes(range(256)) * 4
            await session.write("blob.bin", payload)
            got = await session.read("blob.bin")
            assert got == payload
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_read_write_large_file(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            payload = b"x" * (1024 * 1024)
            await session.write("large.bin", payload)
            got = await session.read("large.bin")
            assert got == payload
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_exec_returns_exit_code(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            good = await session.exec(["sh", "-c", "echo hi; echo boom 1>&2; exit 0"])
            assert good.exit_code == 0
            assert b"hi" in good.stdout
            assert b"boom" in good.stderr
            bad = await session.exec(["sh", "-c", "exit 7"])
            assert bad.exit_code == 7
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_exec_respects_env(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            result = await session.exec(
                ["sh", "-c", "echo $BERNSTEIN_CONFORMANCE"],
                env={"BERNSTEIN_CONFORMANCE": "ok"},
            )
            assert result.exit_code == 0
            assert b"ok" in result.stdout
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_exec_respects_timeout(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            with pytest.raises(TimeoutError):
                await session.exec(["sleep", "5"], timeout=1)
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_ls_lists_entries(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            await session.write("dir/a.txt", b"a")
            await session.write("dir/b.txt", b"b")
            names = await session.ls("dir")
            assert "a.txt" in names
            assert "b.txt" in names
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        await session.shutdown()
        await session.shutdown()  # must not raise

    @pytest.mark.asyncio
    async def test_snapshot_resume_roundtrip(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        # Backends without SNAPSHOT skip; the worktree backend declares it
        # (issue #2295) and must round-trip byte-identical contents.
        if SandboxCapability.SNAPSHOT not in backend.capabilities:
            pytest.skip("Backend does not declare SNAPSHOT capability")
        session = await backend.create(
            manifest,
            options={"repo_root": manifest.root, "run_id": "conf-run", "step_index": 0},
        )
        payload = bytes(range(256)) * 2
        try:
            await session.write("snap/data.bin", payload)
            snapshot_id = await session.snapshot()
            assert snapshot_id
        finally:
            await backend.destroy(session)

        resumed = await backend.resume(snapshot_id)
        try:
            assert await resumed.read("snap/data.bin") == payload
        finally:
            await backend.destroy(resumed)
