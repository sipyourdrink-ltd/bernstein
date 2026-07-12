"""Execute a detached run's tasks on the ssh sandbox backend (#2352, AC4).

The detached run service owns *execution* while the durable work ledger owns
*state*. This module supplies the missing off-host execution path: a task
runner that, for each task of a goal, provisions its own isolated remote
worktree over ssh, injects the credentials the task needs (resolved from the
credential vault -- never the ledger, never the receipts), runs the task's
command in that worktree, and appends a signed ``run.ssh_task`` receipt binding
the worktree and the work-ledger head at execution time.

The receipt is the artefact: strip the audit chain and the work ledger and it
means nothing. An offline verifier holding the chain can prove, per task, that
a goal ran end to end across the ssh boundary in per-task-isolated worktrees --
distinct worktree per task, none lost across a supervisor restart. The runner
plugs into :func:`~bernstein.core.run_service.supervisor.advance_run` as its
``task_runner``, so a killed supervisor resumes on the ssh backend from the
ledger tip exactly as the single-host path does.

Secret handling: credentials flow through
:func:`~bernstein.core.security.vault.resolver.resolve_secret` with the
environment fallback disabled (``environ={}``), so only the vault is consulted.
Resolved values are injected into the remote process environment and are never
written to the ledger, the receipts, or the logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.work_ledger import LedgerReader, run_ledger_dir
from bernstein.core.run_service.paths import run_dir, validate_run_id
from bernstein.core.sandbox.manifest import GitRepoEntry, WorkspaceManifest
from bernstein.core.sandbox.ssh_backend import SSHSandboxBackend
from bernstein.core.security.audit_chain import AuditChainStore, record_run_ssh_task
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.security.vault.resolver import resolve_secret

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from bernstein.core.persistence.work_ledger import LedgerState
    from bernstein.core.sandbox.ssh_backend import SSHTransport
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.vault.protocol import CredentialVault

logger = logging.getLogger(__name__)

#: Sidecar file (in the run's runtime dir) that persists the *non-secret* ssh
#: connection descriptor so the detached supervisor can rebuild the backend on
#: restart. Never carries a credential -- only host, path, and provider ids.
_SIDECAR_NAME = "ssh_backend.json"

#: Worktree-relative path of the per-task isolation marker the runner writes.
_MARKER_PATH = ".bernstein/task-marker"

#: Any character outside this class is replaced by ``-`` when deriving a remote
#: worktree segment or branch component from a task id.
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")


class SSHRunnerError(RuntimeError):
    """Raised when an ssh-backed run task cannot be provisioned or executed."""


def _safe_component(value: str) -> str:
    """Return *value* reduced to a safe ``[A-Za-z0-9._-]`` path/branch segment."""
    cleaned = _UNSAFE_COMPONENT.sub("-", value)
    return cleaned or "task"


@dataclass(frozen=True)
class SSHBackendSpec:
    """Non-secret connection descriptor for running a goal on the ssh backend.

    Every field here is safe to persist and to read back on a supervisor
    restart. Credentials are *not* here: ``secret_env`` names which vault
    provider supplies which remote environment variable, and the values are
    fetched from the vault at execution time.

    Attributes:
        host: The ssh host (hostname or ``~/.ssh/config`` alias).
        remote_root: Absolute POSIX path on the host under which per-task
            worktrees are provisioned.
        user: Optional remote user.
        port: Remote ssh port.
        identity_file: Optional private-key path on the local host.
        repo_src: Optional remote repo path to ``git worktree add`` from. When
            unset, each task gets a plain isolated directory instead.
        base_branch: Base ref each per-task worktree branch is cut from.
        strict_host_key_checking: Passed through to the ssh backend.
        secret_env: Tuple of ``(remote_env_name, vault_provider_id)`` pairs. The
            runner resolves each provider from the vault and injects it into the
            remote environment under ``remote_env_name``.
        timeout_seconds: Per-task wall-clock timeout for the remote command.
    """

    host: str
    remote_root: str
    user: str | None = None
    port: int = 22
    identity_file: str | None = None
    repo_src: str | None = None
    base_branch: str = "main"
    strict_host_key_checking: bool = True
    secret_env: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int = 1800

    def to_public_dict(self) -> dict[str, Any]:
        """Return the JSON-safe, secret-free representation for the sidecar."""
        return {
            "host": self.host,
            "remote_root": self.remote_root,
            "user": self.user,
            "port": self.port,
            "identity_file": self.identity_file,
            "repo_src": self.repo_src,
            "base_branch": self.base_branch,
            "strict_host_key_checking": self.strict_host_key_checking,
            "secret_env": [[name, provider] for name, provider in self.secret_env],
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_public_dict(cls, data: Mapping[str, Any]) -> SSHBackendSpec:
        """Rebuild a spec from :meth:`to_public_dict` output."""
        raw_secret_env = data.get("secret_env") or ()
        secret_env = tuple((str(name), str(provider)) for name, provider in raw_secret_env)
        return cls(
            host=str(data["host"]),
            remote_root=str(data["remote_root"]),
            user=data.get("user"),
            port=int(data.get("port", 22)),
            identity_file=data.get("identity_file"),
            repo_src=data.get("repo_src"),
            base_branch=str(data.get("base_branch", "main")),
            strict_host_key_checking=bool(data.get("strict_host_key_checking", True)),
            secret_env=secret_env,
            timeout_seconds=int(data.get("timeout_seconds", 1800)),
        )


@dataclass(frozen=True)
class SSHTaskReceipt:
    """Result of executing one task on the ssh backend.

    Attributes:
        run_id: The detached run the task belongs to.
        task_id: The task that executed.
        worktree: Absolute POSIX path of the isolated remote worktree.
        exit_code: Exit code of the remote task command.
        worktree_digest: ``sha256:`` digest of the per-task isolation marker.
        ledger_head: Work-ledger head at execution time.
        event: The recorded ``run.ssh_task`` audit event.
    """

    run_id: str
    task_id: str
    worktree: str
    exit_code: int
    worktree_digest: str
    ledger_head: str
    event: AuditEvent


def build_ssh_backend(spec: SSHBackendSpec, *, transport: SSHTransport | None = None) -> SSHSandboxBackend:
    """Construct an :class:`SSHSandboxBackend` from *spec*.

    Module-level so tests (and embedding hosts) can substitute a backend built
    over an in-process transport without a live host. Production callers pass no
    transport and get the default system-``ssh`` transport.
    """
    return SSHSandboxBackend(
        host=spec.host,
        user=spec.user,
        path=spec.remote_root,
        identity_file=spec.identity_file,
        port=spec.port,
        strict_host_key_checking=spec.strict_host_key_checking,
        transport=transport,
    )


def _sidecar_path(root: Path, run_id: str) -> Path:
    return run_dir(Path(root) / ".sdd", validate_run_id(run_id)) / _SIDECAR_NAME


def write_ssh_spec(root: Path, run_id: str, spec: SSHBackendSpec) -> None:
    """Persist the non-secret ssh descriptor for *run_id* to its runtime dir."""
    path = _sidecar_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec.to_public_dict(), sort_keys=True, indent=2), encoding="utf-8")


def read_ssh_spec(root: Path, run_id: str) -> SSHBackendSpec | None:
    """Load the ssh descriptor for *run_id*, or ``None`` when the run is local."""
    path = _sidecar_path(root, run_id)
    if not path.exists():
        return None
    return SSHBackendSpec.from_public_dict(json.loads(path.read_text(encoding="utf-8")))


class SSHTaskRunner:
    """Run each task of a detached run on the ssh backend, one worktree per task.

    Instances are callable so they can be handed straight to
    :func:`~bernstein.core.run_service.supervisor.advance_run` as its
    ``task_runner``.
    """

    def __init__(
        self,
        spec: SSHBackendSpec,
        root: Path,
        run_id: str,
        *,
        backend: SSHSandboxBackend | None = None,
        vault: CredentialVault | None = None,
        command_for: Callable[[str], Sequence[str]] | None = None,
        chain: AuditChainStore | None = None,
    ) -> None:
        """Initialise the runner.

        Args:
            spec: Non-secret connection descriptor.
            root: Project root (the run's ``.sdd`` lives under it).
            run_id: The run whose tasks this runner executes.
            backend: Pre-built backend (tests inject one over an in-process
                transport). When ``None`` a real backend is built lazily.
            vault: Vault used to resolve ``spec.secret_env`` providers.
            command_for: Maps a task id to the argv run in its worktree.
                Defaults to reading the per-task isolation marker back.
            chain: Audit chain store; defaults to the run's ``.sdd/audit``.
        """
        self._spec = spec
        self._root = Path(root)
        self._sdd = self._root / ".sdd"
        self._run_id = run_id
        self._backend = backend
        self._vault = vault
        self._command_for = command_for or self._default_command
        self._chain = chain

    # -- callable seam ------------------------------------------------------

    def __call__(self, task_id: str) -> None:
        """Execute *task_id* (the ``advance_run`` task-runner contract)."""
        self.run_task(task_id)

    def run_task(self, task_id: str) -> SSHTaskReceipt:
        """Provision, execute, and receipt one task on the ssh backend."""
        return asyncio.run(self._run_task_async(task_id))

    # -- secret resolution (vault-only) -------------------------------------

    def resolve_secret_env(self) -> dict[str, str]:
        """Resolve every ``secret_env`` provider from the vault only.

        The legacy environment fallback is disabled, so a provider missing from
        the vault raises rather than silently reading an ambient env var.

        Raises:
            SSHRunnerError: If a provider cannot be resolved from the vault.
        """
        env: dict[str, str] = {}
        for env_name, provider_id in self._spec.secret_env:
            try:
                resolution = resolve_secret(provider_id, vault=self._vault, environ={}, audit=False)
            except Exception as exc:
                # Log the failure *type* only -- never the provider payload.
                logger.warning(
                    "ssh runner: secret resolution failed for provider %s (%s)",
                    sanitize_log(provider_id),
                    type(exc).__name__,
                )
                raise SSHRunnerError(f"secret resolution failed for provider {provider_id!r}") from exc
            if not resolution.found:
                raise SSHRunnerError(f"no vault credential for provider {provider_id!r}")
            env[env_name] = resolution.secret
        return env

    # -- internals ----------------------------------------------------------

    def _get_backend(self) -> SSHSandboxBackend:
        if self._backend is None:
            self._backend = build_ssh_backend(self._spec)
        return self._backend

    def _get_chain(self) -> AuditChainStore:
        if self._chain is None:
            self._chain = AuditChainStore(self._sdd / "audit")
        return self._chain

    def _session_component(self, task_id: str) -> str:
        return f"{self._run_id}--{_safe_component(task_id)}"

    def _branch(self, task_id: str) -> str:
        return f"bernstein/{self._run_id}/{_safe_component(task_id)}"

    def _marker_bytes(self, task_id: str) -> bytes:
        return f"run={self._run_id}\ntask={task_id}\n".encode()

    def _default_command(self, task_id: str) -> Sequence[str]:
        _ = task_id
        return ["sh", "-c", f"cat {_MARKER_PATH}"]

    def _ledger_head(self) -> str:
        reader = LedgerReader(run_ledger_dir(self._sdd, self._run_id))
        return reader.verify().head_hash

    async def _run_task_async(self, task_id: str) -> SSHTaskReceipt:
        env = self.resolve_secret_env()
        backend = self._get_backend()
        repo = (
            GitRepoEntry(src_path=self._spec.repo_src, branch=self._spec.base_branch) if self._spec.repo_src else None
        )
        manifest = WorkspaceManifest(repo=repo, env=env, timeout_seconds=self._spec.timeout_seconds)
        session = await backend.create(
            manifest,
            options={
                "session_id": self._session_component(task_id),
                "worktree_branch": self._branch(task_id),
                # Resume-safe: a task killed mid-execution left a worktree/branch;
                # re-running it must reset rather than collide (zero lost work).
                "worktree_reset": True,
            },
        )
        try:
            marker = self._marker_bytes(task_id)
            await session.write(_MARKER_PATH, marker)
            result = await session.exec(list(self._command_for(task_id)))
            worktree = session.workdir
            digest = "sha256:" + hashlib.sha256(marker).hexdigest()
            ledger_head = self._ledger_head()
            event = record_run_ssh_task(
                chain=self._get_chain(),
                run_id=self._run_id,
                task_id=task_id,
                host=self._spec.host,
                worktree=worktree,
                exit_code=result.exit_code,
                worktree_digest=digest,
                ledger_head=ledger_head,
            )
            return SSHTaskReceipt(
                run_id=self._run_id,
                task_id=task_id,
                worktree=worktree,
                exit_code=result.exit_code,
                worktree_digest=digest,
                ledger_head=ledger_head,
                event=event,
            )
        finally:
            await backend.destroy(session)


def run_goal_on_ssh(
    root: Path,
    goal: str,
    task_ids: Sequence[str],
    spec: SSHBackendSpec,
    *,
    run_id: str | None = None,
    backend: SSHSandboxBackend | None = None,
    vault: CredentialVault | None = None,
    command_for: Callable[[str], Sequence[str]] | None = None,
) -> LedgerState:
    """Submit *goal* and advance it end to end on the ssh backend.

    A convenience over the run service: open the run, then drive its whole
    frontier through :func:`advance_run` with an :class:`SSHTaskRunner`, then
    close it. Returns the final ledger projection.
    """
    from bernstein.core.run_service.service import RunService
    from bernstein.core.run_service.supervisor import advance_run

    svc = RunService(root)
    handle = svc.submit(goal, task_ids, run_id=run_id)
    write_ssh_spec(root, handle.run_id, spec)
    runner = SSHTaskRunner(spec, root, handle.run_id, backend=backend, vault=vault, command_for=command_for)
    advance_run(root, handle.run_id, task_runner=runner)
    svc.complete(handle.run_id)
    return svc.project(handle.run_id)


__all__ = [
    "SSHBackendSpec",
    "SSHRunnerError",
    "SSHTaskReceipt",
    "SSHTaskRunner",
    "build_ssh_backend",
    "read_ssh_spec",
    "run_goal_on_ssh",
    "write_ssh_spec",
]
