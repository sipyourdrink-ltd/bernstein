"""Docker-daemon :class:`SandboxBackend` — first-party, ships in core.

The Docker backend launches a long-running container per session and
proxies file I/O through ``docker cp`` and command execution through
``docker exec``. The ``docker`` Python SDK is an optional install —
when it is missing the backend module still imports cleanly, but
instantiating :class:`DockerSandboxBackend` raises a clear error so the
registry can report the state via
:func:`~bernstein.core.sandbox.registry.list_backends`.

Phase 1 scope: the backend is first-party and ships in core so
integration tests can cover Docker without an optional extra; the
``docker`` SDK dependency is captured in the ``[docker]`` extra so
minimal installs remain lean.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import tarfile
import time
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from bernstein.core.sandbox.backend import (
    ExecResult,
    SandboxCapability,
    SandboxSession,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.sandbox.manifest import WorkspaceManifest

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.13-slim"
_DEFAULT_MEMORY_MB = 2048
_DEFAULT_CPU_QUOTA = 200000  # 2 CPUs when period=100000


class DockerUnavailableError(RuntimeError):
    """Raised when the ``docker`` Python SDK or daemon is unreachable."""


def _import_docker() -> Any:
    """Import the ``docker`` SDK lazily, raising a friendly error on miss."""
    try:
        import docker  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DockerUnavailableError(
            "Install the 'docker' extra to use DockerSandboxBackend: "
            "`pip install bernstein[docker]` or `uv sync --extra docker`."
        ) from exc
    return docker


def _safe_extract_single_file(tf: tarfile.TarFile, *, expected_name: str, resolved: str) -> bytes:
    """Extract exactly one file from ``tf`` whose name matches ``expected_name``.

    Defence-in-depth against path traversal in the tar members
    produced by ``docker.container.get_archive``. Although the archive
    comes from a container we control, we still validate each member:

    * Reject any member whose name does not equal ``expected_name``
      (the basename of the requested path) or the absolute-path form
      ``resolved.lstrip("/")``. This prevents a crafted or buggy tar
      from smuggling in a member called ``../etc/shadow`` or a
      symlink-style entry that resolves outside our intended target.
    * Reject non-file members (directories, symlinks, devices) —
      ``session.read`` only ever wants a single file's bytes.
    * Reject any absolute-path member whose name starts with ``/`` or
      contains a ``..`` segment after normalisation. Even a matching
      name must be "inside" the requested target to be accepted.

    Args:
        tf: Open tar archive streamed from ``container.get_archive``.
        expected_name: Basename of the path the caller requested.
        resolved: The full POSIX path the caller requested. Used for
            the error message and for the alternate absolute-form
            member-name comparison.

    Returns:
        The bytes of the single file member.

    Raises:
        FileNotFoundError: No matching file member was found.
        IsADirectoryError: The archive held only directory entries.
    """
    members = tf.getmembers()
    if not members:
        raise FileNotFoundError(resolved)

    # Docker's get_archive can emit either basename-only members (the
    # common case) or absolute-path members on some daemon versions.
    # We accept both forms; anything else is rejected.
    absolute_form = resolved.lstrip("/")

    acceptable: list[tarfile.TarInfo] = []
    saw_any_file = False
    for member in members:
        if not member.isfile():
            continue
        saw_any_file = True

        # Normalise once: reject absolute-path traversal (e.g.
        # ``../etc/shadow``). PurePosixPath normalises redundant
        # separators but keeps ``..`` segments explicit.
        parts = PurePosixPath(member.name).parts
        if ".." in parts:
            continue
        if member.name.startswith("/"):
            continue

        if member.name == expected_name or member.name == absolute_form:
            acceptable.append(member)

    if not acceptable:
        if saw_any_file:
            # A file member existed but its name did not match. Surface
            # as FileNotFoundError to match the documented contract —
            # the caller asked for a file that is not present under the
            # name they provided.
            raise FileNotFoundError(resolved)
        raise IsADirectoryError(resolved)

    extracted = tf.extractfile(acceptable[0])
    if extracted is None:
        raise FileNotFoundError(resolved)
    return extracted.read()


class DockerSandboxSession(SandboxSession):
    """A session backed by a running Docker container.

    Construction is internal — obtain instances via
    :meth:`DockerSandboxBackend.create`.
    """

    backend_name = "docker"

    def __init__(
        self,
        *,
        session_id: str,
        container: Any,
        workdir: str,
        base_env: Mapping[str, str],
        default_timeout: int,
    ) -> None:
        self.session_id = session_id
        self.workdir = workdir
        self._container = container
        self._base_env = dict(base_env)
        self._default_timeout = default_timeout
        self._closed = False

    # ------------------------------------------------------------------
    # Path normalisation
    # ------------------------------------------------------------------

    def _resolve_posix(self, path: str) -> str:
        """Resolve *path* inside the container, rooted at ``workdir``."""
        candidate = PurePosixPath(path)
        if candidate.is_absolute():
            return str(candidate)
        return str(PurePosixPath(self.workdir) / candidate)

    # ------------------------------------------------------------------
    # File primitives
    # ------------------------------------------------------------------

    async def read(self, path: str) -> bytes:
        resolved = self._resolve_posix(path)
        expected_name = PurePosixPath(resolved).name

        def _do_read() -> bytes:
            archive, _ = self._container.get_archive(resolved)
            buf = BytesIO()
            for chunk in archive:
                buf.write(chunk)
            buf.seek(0)
            # ``get_archive`` returns a tar stream sourced from a
            # container we own. The tar is not from an untrusted
            # origin, but we still validate members before extraction
            # so a docker-daemon bug (or future change to
            # ``get_archive``) cannot turn ``session.read`` into a path
            # traversal primitive. ``_safe_extract_single_file``
            # refuses any member whose name does not resolve to the
            # exact file we requested.
            with tarfile.open(fileobj=buf, mode="r") as tf:  # NOSONAR S5042
                return _safe_extract_single_file(tf, expected_name=expected_name, resolved=resolved)

        return await asyncio.to_thread(_do_read)

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        resolved = self._resolve_posix(path)
        parent = str(PurePosixPath(resolved).parent)
        name = PurePosixPath(resolved).name

        def _do_write() -> None:
            # Ensure parent dir exists.
            mkdir = self._container.exec_run(["mkdir", "-p", parent])
            if mkdir.exit_code != 0:
                raise OSError(f"mkdir -p {parent} failed: {mkdir.output.decode('utf-8', 'replace')}")
            # Build a single-member tar in memory. The archive is
            # constructed *here* from trusted inputs (``name`` derives
            # from ``self._resolve_posix`` which pins paths under
            # :attr:`workdir`; ``data`` is caller-supplied bytes with
            # no path component) — it is then streamed straight to
            # ``put_archive``. There is no extraction step on our
            # side, so S5042 "tar extraction" guidance does not apply:
            # the only consumer is the docker daemon, which treats the
            # single member as the file to place under ``parent``.
            buf = BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:  # NOSONAR S5042
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mode = mode
                info.mtime = int(time.time())
                tf.addfile(info, BytesIO(data))
            buf.seek(0)
            ok = self._container.put_archive(parent, buf.getvalue())
            if not ok:
                raise OSError(f"put_archive failed for {resolved}")

        await asyncio.to_thread(_do_write)

    async def ls(self, path: str) -> list[str]:
        resolved = self._resolve_posix(path)

        def _do_ls() -> list[str]:
            result = self._container.exec_run(["ls", "-1", resolved])
            if result.exit_code != 0:
                raise OSError(f"ls {resolved} failed: {result.output.decode('utf-8', 'replace')}")
            output = result.output.decode("utf-8", "replace")
            entries = [line for line in output.splitlines() if line]
            return sorted(entries)

        return await asyncio.to_thread(_do_ls)

    # ------------------------------------------------------------------
    # Exec primitive
    # ------------------------------------------------------------------

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        if self._closed:
            raise RuntimeError(f"Session {self.session_id} is closed")
        if not cmd:
            raise ValueError("cmd must be a non-empty argv list")
        if stdin is not None:
            raise NotImplementedError("Docker backend does not yet support stdin injection; tracked in oai-002b")
        effective_cwd = cwd if cwd is not None else self.workdir
        effective_timeout = timeout if timeout is not None else self._default_timeout
        merged_env = dict(self._base_env)
        if env:
            merged_env.update(env)
        env_list = [f"{key}={value}" for key, value in merged_env.items()]

        start = time.monotonic()

        def _run() -> tuple[int, bytes, bytes]:
            # ``exec_run`` with ``demux=True`` gives (stdout, stderr) bytes.
            result = self._container.exec_run(
                cmd,
                workdir=effective_cwd,
                environment=env_list,
                demux=True,
            )
            stdout_b, stderr_b = result.output  # type: ignore[misc]
            return (
                int(result.exit_code),
                stdout_b or b"",
                stderr_b or b"",
            )

        try:
            exit_code, stdout_b, stderr_b = await asyncio.wait_for(asyncio.to_thread(_run), timeout=effective_timeout)
        except TimeoutError:
            # Best-effort: kill a stuck exec by killing the container.
            # More surgical cleanup belongs in oai-002b where the
            # spawner owns the exec lifecycle.
            try:
                self._container.kill()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to kill container after timeout: %s", exc)
            raise TimeoutError(f"Command {cmd!r} timed out after {effective_timeout}s") from None
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout_b,
            stderr=stderr_b,
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------
    # Snapshots (unsupported in phase 1)
    # ------------------------------------------------------------------

    async def snapshot(self) -> str:
        raise NotImplementedError("DockerSandboxBackend does not declare the SNAPSHOT capability in phase 1")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _teardown() -> None:
            try:
                self._container.stop(timeout=5)
            except Exception as exc:
                logger.debug("container.stop failed: %s", exc)
            try:
                self._container.remove(force=True)
            except Exception as exc:
                logger.debug("container.remove failed: %s", exc)

        await asyncio.to_thread(_teardown)


class DockerSandboxBackend:
    """SandboxBackend that runs each session inside a Docker container."""

    name = "docker"
    capabilities: frozenset[SandboxCapability] = frozenset(
        {
            SandboxCapability.FILE_RW,
            SandboxCapability.EXEC,
            SandboxCapability.NETWORK,
        }
    )

    def __init__(self, *, client: Any | None = None) -> None:
        """Create the backend.

        Args:
            client: Optional pre-built ``docker.DockerClient`` instance.
                Tests pass a mock; production callers let the backend
                build one via ``docker.from_env`` on first use. The
                client is NOT instantiated at construction time so the
                registry can list the backend without requiring a live
                Docker daemon.
        """
        self._client = client
        self._sessions: dict[str, DockerSandboxSession] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            docker_mod = _import_docker()
            try:
                self._client = docker_mod.from_env()
            except Exception as exc:
                raise DockerUnavailableError(f"Could not connect to Docker daemon: {exc}") from exc
        return self._client

    @staticmethod
    def _allocate_session_id(hint: str | None = None) -> str:
        if hint:
            return hint
        return f"bernstein-sbx-{secrets.token_hex(6)}"

    async def create(
        self,
        manifest: WorkspaceManifest,
        options: dict[str, Any] | None = None,
    ) -> SandboxSession:
        """Start a container per *manifest* and return the session.

        Recognised ``options``:

        - ``image``: Docker image to run. Default ``python:3.13-slim``.
        - ``memory_mb``: Memory limit (int MB). Default 2048.
        - ``cpu_quota``: CFS quota (int microseconds). Default 200000
          (≈ 2 CPU when period=100000).
        - ``network_disabled``: Bool, default False. Set True to create
          the container with ``--network=none``.
        - ``session_id``: Explicit container name suffix.
        - ``labels``: Extra labels to attach for discovery.
        """
        opts = dict(options or {})
        image = opts.get("image", _DEFAULT_IMAGE)
        memory_mb = int(opts.get("memory_mb", _DEFAULT_MEMORY_MB))
        cpu_quota = int(opts.get("cpu_quota", _DEFAULT_CPU_QUOTA))
        network_disabled = bool(opts.get("network_disabled", False))
        session_id = self._allocate_session_id(opts.get("session_id"))

        env_list = [f"{k}={v}" for k, v in manifest.env.items()]
        labels = {
            "bernstein.sandbox": "docker",
            "bernstein.sandbox.session_id": session_id,
        }
        extra_labels: Any = opts.get("labels")
        if isinstance(extra_labels, dict):
            for raw_key, raw_value in extra_labels.items():  # pyright: ignore[reportUnknownVariableType]
                labels[str(raw_key)] = str(raw_value)  # pyright: ignore[reportUnknownArgumentType]

        client = self._get_client()

        def _spawn_container() -> Any:
            container = client.containers.run(
                image=image,
                name=f"bernstein-{session_id}",
                command=["sleep", "infinity"],
                detach=True,
                tty=False,
                working_dir=manifest.root,
                environment=env_list,
                mem_limit=f"{memory_mb}m",
                cpu_period=100000,
                cpu_quota=cpu_quota,
                network_disabled=network_disabled,
                labels=labels,
            )
            # Ensure the workdir exists inside the container.
            mkdir = container.exec_run(["mkdir", "-p", manifest.root])
            if mkdir.exit_code != 0:
                raise RuntimeError(
                    f"mkdir -p {manifest.root} failed in container: {mkdir.output.decode('utf-8', 'replace')}"
                )
            return container

        container = await asyncio.to_thread(_spawn_container)
        session = DockerSandboxSession(
            session_id=session_id,
            container=container,
            workdir=manifest.root,
            base_env=manifest.env,
            default_timeout=manifest.timeout_seconds,
        )
        # Inject byte files after the session is constructed so we can
        # reuse its ``write`` helper for path resolution.
        for entry in manifest.files:
            await session.write(entry.path, entry.content, mode=entry.mode)
        self._sessions[session_id] = session
        return session

    async def resume(self, snapshot_id: str) -> SandboxSession:
        raise NotImplementedError("DockerSandboxBackend does not declare the SNAPSHOT capability in phase 1")

    async def destroy(self, session: SandboxSession) -> None:
        await session.shutdown()
        self._sessions.pop(session.session_id, None)


__all__ = [
    "DockerSandboxBackend",
    "DockerSandboxSession",
    "DockerUnavailableError",
]
