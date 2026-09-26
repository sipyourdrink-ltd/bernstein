"""Sandbox0 sessions backed by stock gVisor and durable RootFS snapshots.

The synchronous SDK runs in worker threads: AgentSpawner uses a different
asyncio loop for provisioning, execution, and teardown. No loop-bound HTTP
client is retained. Provider credentials stay on the orchestrator.
"""

from __future__ import annotations

# Session and backend jointly own resources; private access stays in this module.
# pyright: reportPrivateUsage=false
import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from bernstein.core.sandbox.backend import ExecResult, SandboxCapability, SandboxSession
from bernstein.core.sandbox.backends._remote_helpers import guard_exec_preconditions, resolve_posix_path
from bernstein.core.sandbox.manifest import WorkspaceManifest

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.sandbox.manifest import GitRepoEntry

# Capture byte streams in files instead of the context API's bounded text log.
# os.execvpe preserves argv literally and replaces the context's process, so
# deleting the context terminates the actual command on timeout/cancellation.
_EXEC = """import json, os, sys
p = sys.argv[1]
with open(p + '/request.json') as f:
    r = json.load(f)
os.chdir(r['cwd'])
for fd, name, flags in [(0, 'stdin', os.O_RDONLY),
                        (1, 'stdout', os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
                        (2, 'stderr', os.O_WRONLY | os.O_CREAT | os.O_TRUNC)]:
    source = os.open(p + '/' + name, flags, 0o600)
    os.dup2(source, fd)
    if source != fd:
        os.close(source)
os.execvpe(r['cmd'][0], r['cmd'], dict(os.environ, **r['env']))
"""
_METADATA = "/var/lib/bernstein/sandbox0-session.json"


def _sdk() -> Any:
    try:
        import sandbox0
    except ImportError as exc:
        raise RuntimeError("Install Sandbox0 support with `pip install 'bernstein[sandbox0]'`") from exc
    return sandbox0


async def _owned_call(function: Any, *args: Any, cleanup: Any, **kwargs: Any) -> Any:
    """Finish an in-flight allocation before cleaning up a cancelled caller.

    Cancelling to_thread alone leaves the HTTP operation running and loses its
    returned resource id. Shield allocation, then reap it if cancellation wins.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        value = await task
        await asyncio.to_thread(cleanup, value)
        raise


def _bundle_repository(repo: GitRepoEntry) -> tuple[bytes, str]:
    """Export a branch, or detached HEAD, without changing any source refs.

    Git bundles need a named ref, and sync-back fetches refs/heads/*. Stage a
    detached commit in a temporary bare repository so agent commits remain
    reachable through a branch without creating one in the operator's repo.
    """
    with tempfile.TemporaryDirectory(prefix="bernstein-bundle-") as directory:
        bundle = Path(directory) / "repo.bundle"
        source = Path(repo.src_path).resolve()
        branch = repo.branch
        if branch == "HEAD":
            try:
                commit = (
                    subprocess.run(
                        ["git", "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
                        cwd=source,
                        check=True,
                        capture_output=True,
                        timeout=10,
                    )
                    .stdout.decode("ascii")
                    .strip()
                )
            except subprocess.CalledProcessError as exc:
                raise ValueError("Workspace Git HEAD must resolve to an existing commit") from exc
            staging = Path(directory) / "source.git"
            branch = "bernstein-detached"
            subprocess.run(["git", "init", "--bare", str(staging)], check=True, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "fetch", "--no-tags", "--", str(source), f"{commit}:refs/heads/{branch}"],
                cwd=staging,
                check=True,
                capture_output=True,
                timeout=120,
            )
            source = staging
        else:
            subprocess.run(
                ["git", "check-ref-format", "--branch", branch],
                cwd=source,
                check=True,
                capture_output=True,
                timeout=10,
            )
        subprocess.run(
            ["git", "bundle", "create", str(bundle), f"refs/heads/{branch}"],
            cwd=source,
            check=True,
            capture_output=True,
            timeout=120,
        )
        return bundle.read_bytes(), branch


class Sandbox0SandboxSession(SandboxSession):
    backend_name = "sandbox0"

    def __init__(
        self, backend: Sandbox0SandboxBackend, sandbox: Any, manifest: WorkspaceManifest, template: str
    ) -> None:
        self.session_id = sandbox.id
        self.workdir = manifest.root
        self._backend = backend
        self._sandbox = sandbox
        self._manifest = manifest
        self._template = template
        self._closed = False
        self._shutdown_lock = threading.Lock()

    async def read(self, path: str) -> bytes:
        resolved = resolve_posix_path(self.workdir, path)
        try:
            return await asyncio.to_thread(self._sandbox.read_file, resolved)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                raise FileNotFoundError(resolved) from exc
            raise

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        resolved = resolve_posix_path(self.workdir, path)
        await asyncio.to_thread(self._sandbox.mkdir, str(PurePosixPath(resolved).parent), recursive=True)
        await asyncio.to_thread(self._sandbox.write_file, resolved, data)
        result = await self.exec(["chmod", format(mode, "o"), "--", resolved], cwd="/")
        if result.exit_code:
            raise OSError("Could not set sandbox file permissions")

    async def ls(self, path: str) -> list[str]:
        entries = await asyncio.to_thread(self._sandbox.list_files, resolve_posix_path(self.workdir, path))
        return sorted(entry.name for entry in entries)

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        guard_exec_preconditions(self._closed, self.session_id, cmd)
        from sandbox0.apispec.models.create_cmd_context_request import CreateCMDContextRequest
        from sandbox0.apispec.models.create_context_request import CreateContextRequest
        from sandbox0.apispec.models.process_type import ProcessType
        from sandbox0.apispec.types import Unset

        limit = self._manifest.timeout_seconds if timeout is None else timeout
        if limit <= 0:
            raise ValueError("timeout must be positive")
        started = time.monotonic()
        io_dir = f"/tmp/bernstein-exec-{uuid.uuid4().hex}"
        context = None
        try:
            await asyncio.to_thread(self._sandbox.mkdir, io_dir, recursive=True)
            request = json.dumps(
                {
                    "cmd": cmd,
                    "cwd": resolve_posix_path(self.workdir, cwd or self.workdir),
                    "env": dict(self._manifest.env) | dict(env or {}),
                }
            ).encode()
            await asyncio.to_thread(self._sandbox.write_file, io_dir + "/request.json", request)
            await asyncio.to_thread(self._sandbox.write_file, io_dir + "/stdin", stdin or b"")

            def reap_context(value: Any) -> None:
                self._sandbox.delete_context(value.id)

            context = await _owned_call(
                self._sandbox.create_context,
                CreateContextRequest(
                    type_=ProcessType.CMD,
                    cmd=CreateCMDContextRequest(command=["python3", "-c", _EXEC, io_dir]),
                    cwd="/",
                    wait_until_done=False,
                ),
                cleanup=reap_context,
            )
            while True:
                state = await asyncio.to_thread(self._sandbox.get_context, context.id)
                if not state.running:
                    if isinstance(state.exit_code, Unset) or state.exit_code is None:
                        raise RuntimeError("Sandbox0 command ended without an exit code")
                    stdout = await asyncio.to_thread(self._sandbox.read_file, io_dir + "/stdout")
                    stderr = await asyncio.to_thread(self._sandbox.read_file, io_dir + "/stderr")
                    return ExecResult(state.exit_code, stdout, stderr, time.monotonic() - started)
                if time.monotonic() - started >= limit:
                    raise TimeoutError(f"Sandbox0 command exceeded {limit}s")
                await asyncio.sleep(0.1)
        finally:
            if context is not None:
                await asyncio.to_thread(self._sandbox.delete_context, context.id)
            # These files include stdin and the command environment. Never
            # retain them in a subsequent RootFS snapshot.
            await asyncio.to_thread(self._sandbox.delete_file, io_dir)

    async def snapshot(self) -> str:
        # Metadata lives inside the encrypted snapshot, never in the id/logs.
        await self.write(_METADATA, json.dumps({"env": dict(self._manifest.env)}).encode(), mode=0o600)

        def reap_snapshot(value: Any) -> None:
            self._backend.client.sandboxes.delete_rootfs_snapshot(value.id)

        snapshot = await _owned_call(
            self._backend.client.sandboxes.create_rootfs_snapshot,
            self.session_id,
            cleanup=reap_snapshot,
        )
        return json.dumps(
            {
                "version": 1,
                "snapshot_id": snapshot.id,
                "template": self._template,
                "root": self.workdir,
                "timeout_seconds": self._manifest.timeout_seconds,
            },
            sort_keys=True,
        )

    async def shutdown(self) -> None:
        def close() -> None:
            with self._shutdown_lock:
                if self._closed:
                    return
                self._backend._delete_sandbox(self.session_id)
                self._closed = True
                self._backend._forget(self.session_id)

        await asyncio.to_thread(close)


class Sandbox0SandboxBackend:
    """Optional SDK backend. Snapshots survive destroy until explicitly deleted."""

    name = "sandbox0"
    capabilities = frozenset(
        {SandboxCapability.FILE_RW, SandboxCapability.EXEC, SandboxCapability.NETWORK, SandboxCapability.SNAPSHOT}
    )

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client
        self._sessions: dict[str, Sandbox0SandboxSession] = {}
        self._lock = threading.Lock()

    @property
    def client(self) -> Any:
        with self._lock:
            if self._client is None:
                token = os.environ.get("SANDBOX0_API_KEY") or os.environ.get("SANDBOX0_TOKEN")
                if not token:
                    raise ValueError("SANDBOX0_API_KEY or SANDBOX0_TOKEN is required")
                kwargs: dict[str, Any] = {"token": token, "timeout": 60.0}
                if base_url := os.environ.get("SANDBOX0_BASE_URL"):
                    kwargs["base_url"] = base_url
                self._client = _sdk().Client(**kwargs)
            return self._client

    def ensure_available(self) -> None:
        """Check installation and credentials before any agent can spawn."""
        _ = self.client

    def _delete_sandbox(self, sandbox_id: str) -> None:
        """Wait for committed deletion, including SDKs predating HTTP 202.

        Older generated SDKs raise APIError for the current asynchronous
        acceptance response. Acceptance is not proof of cleanup: GET must
        return 404 before the session is forgotten.
        """
        try:
            self.client.delete_sandbox(sandbox_id)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 404:
                return
            if status != 202:
                raise
        deadline = time.monotonic() + 120
        while True:
            try:
                self.client.sandboxes.get(sandbox_id)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    return
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("Sandbox0 deletion is still pending; retry shutdown")
            time.sleep(0.25)

    def _forget(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    async def _claim(
        self, manifest: WorkspaceManifest, template: str, snapshot_id: str | None = None
    ) -> Sandbox0SandboxSession:
        def reap_sandbox(value: Any) -> None:
            self._delete_sandbox(value.id)

        sandbox = await _owned_call(
            self.client.sandboxes.claim,
            template,
            snapshot_id=snapshot_id,
            cleanup=reap_sandbox,
        )
        session = Sandbox0SandboxSession(self, sandbox, manifest, template)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    async def create(self, manifest: WorkspaceManifest, options: dict[str, Any] | None = None) -> SandboxSession:
        if manifest.artifact_mounts:
            raise NotImplementedError("Sandbox0 artifact mounts are not implemented; use file transfer")
        if not PurePosixPath(manifest.root).is_absolute() or manifest.timeout_seconds <= 0:
            raise ValueError("Workspace root must be absolute and timeout_seconds positive")
        opts = options or {}
        template = str(opts.get("template") or os.environ.get("SANDBOX0_TEMPLATE") or "default")
        session = await self._claim(manifest, template)
        try:
            if manifest.repo is not None:
                await self._seed_repo(session, manifest)
            else:
                await asyncio.to_thread(session._sandbox.mkdir, manifest.root, recursive=True)
            for entry in manifest.files:
                await session.write(entry.path, entry.content, mode=entry.mode)
            return session
        except BaseException:
            await session.shutdown()
            raise

    async def _seed_repo(self, session: Sandbox0SandboxSession, manifest: WorkspaceManifest) -> None:
        """Transfer committed local history without exposing host credentials/config."""
        assert manifest.repo is not None
        repo = manifest.repo
        payload, checkout_branch = await asyncio.to_thread(_bundle_repository, repo)
        remote = f"/tmp/bernstein-repo-{uuid.uuid4().hex}.bundle"
        await asyncio.to_thread(session._sandbox.write_file, remote, payload)
        try:
            result = await session.exec(
                ["git", "clone", "--branch", checkout_branch, "--", remote, manifest.root], cwd="/"
            )
            if result.exit_code:
                raise RuntimeError(
                    f"Could not clone the workspace Git bundle: {result.stderr[:500].decode(errors='replace')}"
                )
            if repo.sparse_paths:
                result = await session.exec(["git", "sparse-checkout", "set", "--", *repo.sparse_paths])
                if result.exit_code:
                    raise RuntimeError("Could not configure sparse checkout")
            for key, value in (("user.name", "Bernstein"), ("user.email", "bernstein@localhost")):
                result = await session.exec(["git", "config", key, value])
                if result.exit_code:
                    raise RuntimeError("Could not configure workspace Git identity")
        finally:
            await asyncio.to_thread(session._sandbox.delete_file, remote)

    async def resume(self, snapshot_id: str) -> SandboxSession:
        reference = json.loads(snapshot_id)
        if reference.get("version") != 1:
            raise ValueError("Unsupported Sandbox0 snapshot reference")
        manifest = WorkspaceManifest(root=reference["root"], timeout_seconds=reference["timeout_seconds"])
        session = await self._claim(manifest, reference["template"], reference["snapshot_id"])
        try:
            metadata = json.loads(await session.read(_METADATA))
            session._manifest = WorkspaceManifest(
                root=manifest.root, timeout_seconds=manifest.timeout_seconds, env=metadata["env"]
            )
            return session
        except BaseException:
            await session.shutdown()
            raise

    async def destroy(self, session: SandboxSession) -> None:
        await session.shutdown()

    async def destroy_all(self) -> None:
        """Reap sessions retained after a spawn/setup failure, preserving snapshots."""
        with self._lock:
            sessions = list(self._sessions.values())
        results = await asyncio.gather(*(s.shutdown() for s in sessions), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
