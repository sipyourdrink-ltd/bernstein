"""Container-based agent isolation — kernel-level sandboxing for spawned agents.

Provides configurable container runtimes (Docker, Podman, gVisor, Firecracker)
that wrap the existing CLI adapter pattern.  Each agent session runs inside an
isolated container with enforced resource limits, network policies, and
filesystem restrictions.

Usage::

    from bernstein.core.container import ContainerManager, ContainerConfig

    config = ContainerConfig(runtime="docker", image="bernstein-agent:latest")
    mgr = ContainerManager(config, workdir=Path("."))
    handle = mgr.create("session-abc123", env={"ANTHROPIC_API_KEY": "sk-..."})
    mgr.exec(handle, ["claude", "-p", "Fix the bug"])
    mgr.destroy(handle)
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONTAINER_CMD_TIMEOUT_S = 30  # Timeout for container management commands
_CONTAINER_DESTROY_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


class ContainerRuntime(StrEnum):
    """Supported container runtime backends."""

    DOCKER = "docker"
    PODMAN = "podman"
    GVISOR = "gvisor"  # Docker/Podman with --runtime=runsc
    FIRECRACKER = "firecracker"  # Via ignite or firecracker-containerd


class NetworkMode(StrEnum):
    """Network isolation modes for containers."""

    HOST = "host"  # Share host network (for task server access)
    BRIDGE = "bridge"  # Default Docker bridge (isolated)
    NONE = "none"  # No network at all


@dataclass(frozen=True)
class ResourceLimits:
    """Enforced resource constraints for a container.

    Attributes:
        cpu_cores: Number of CPU cores (float, e.g. 1.5). None = unlimited.
        memory_mb: Memory limit in megabytes. None = unlimited.
        memory_swap_mb: Swap limit in megabytes. -1 = unlimited swap.
        disk_mb: Disk I/O limit (via tmpfs size). None = unlimited.
        pids_limit: Maximum number of processes. None = unlimited.
        read_only_rootfs: Mount root filesystem as read-only.
    """

    cpu_cores: float | None = 2.0
    memory_mb: int | None = 4096
    memory_swap_mb: int = -1
    disk_mb: int | None = None
    pids_limit: int | None = 256
    read_only_rootfs: bool = False


@dataclass(frozen=True)
class SecurityProfile:
    """Security hardening options for the container.

    Attributes:
        drop_capabilities: Linux capabilities to drop (e.g. NET_RAW, SYS_ADMIN).
        no_new_privileges: Prevent privilege escalation via setuid/setgid.
        seccomp_profile: Path to seccomp profile JSON, or "default".
        user: Run as this user inside the container (e.g. "1000:1000").
    """

    drop_capabilities: tuple[str, ...] = (
        "NET_RAW",
        "SYS_ADMIN",
        "SYS_PTRACE",
        "MKNOD",
        "AUDIT_WRITE",
        "SETFCAP",
    )
    no_new_privileges: bool = True
    seccomp_profile: str = "default"
    user: str | None = "1000:1000"


@dataclass(frozen=True)
class MountSpec:
    """A bind mount or volume mount for the container.

    Attributes:
        host_path: Absolute path on the host.
        container_path: Path inside the container.
        read_only: Mount as read-only.
    """

    host_path: str
    container_path: str
    read_only: bool = False


@dataclass(frozen=True)
class TwoPhaseSandboxConfig:
    """Codex-style two-phase sandboxed execution configuration.

    Phase 1 runs with network access to install dependencies (setup phase).
    Phase 2 runs the actual agent with network completely disabled (execution
    phase).  This is an industry-standard security pattern that prevents agents
    from exfiltrating data or making unexpected external calls at runtime.

    Attributes:
        setup_commands: Shell commands to run in Phase 1.  An empty tuple
            triggers auto-detection from the workspace (uv.lock, package.json,
            requirements.txt, etc.).
        phase1_timeout_s: Maximum wall-clock time allowed for Phase 1 setup.
        phase1_network_mode: Network mode for Phase 1 (needs internet access).
        phase2_network_mode: Network mode for Phase 2 (agent execution); should
            be NONE for full isolation.
    """

    setup_commands: tuple[str, ...] = ()
    phase1_timeout_s: int = 300
    phase1_network_mode: NetworkMode = NetworkMode.BRIDGE
    phase2_network_mode: NetworkMode = NetworkMode.NONE


def _detect_setup_commands(workspace: Path) -> list[str]:
    """Auto-detect dependency-install commands from workspace project files.

    Checks for common lock files and manifests in priority order.  Returns
    the first matching set of commands.

    Args:
        workspace: Root directory of the project.

    Returns:
        List of shell commands to run in Phase 1, or empty list if none detected.
    """
    checks: list[tuple[str, str]] = [
        # File to check           # Command to run
        ("uv.lock", "uv sync --frozen"),
        ("requirements.txt", "pip install -r requirements.txt"),
        ("yarn.lock", "yarn install --frozen-lockfile"),
        ("package-lock.json", "npm ci"),
        ("package.json", "npm install"),
        ("Gemfile.lock", "bundle install"),
        ("go.sum", "go mod download"),
        ("Cargo.lock", "cargo fetch"),
    ]
    for filename, cmd in checks:
        if (workspace / filename).exists():
            logger.debug("Auto-detected setup command for %s: %s", filename, cmd)
            return [cmd]
    return []


@dataclass(frozen=True)
class ContainerConfig:
    """Full container isolation configuration.

    Attributes:
        runtime: Which container runtime to use.
        image: Container image for agent execution.
        resource_limits: CPU/memory/PID constraints.
        security: Security hardening profile.
        network_mode: Network isolation mode.
        extra_mounts: Additional bind mounts beyond the workspace.
        labels: Metadata labels applied to the container.
        env_allowlist: Environment variables to pass through to the container.
        extra_hosts: Extra /etc/hosts entries (e.g. for task server access).
        two_phase_sandbox: If set, enables Codex-style two-phase execution:
            Phase 1 installs deps with network; Phase 2 runs agent without it.
    """

    runtime: ContainerRuntime = ContainerRuntime.DOCKER
    image: str = "bernstein-agent:latest"
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    security: SecurityProfile = field(default_factory=SecurityProfile)
    network_mode: NetworkMode = NetworkMode.HOST
    extra_mounts: tuple[MountSpec, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    env_allowlist: tuple[str, ...] = ()
    extra_hosts: tuple[str, ...] = ()
    two_phase_sandbox: TwoPhaseSandboxConfig | None = None


# ---------------------------------------------------------------------------
# Container handle
# ---------------------------------------------------------------------------


@dataclass
class ContainerHandle:
    """Represents a running or stopped container.

    Attributes:
        container_id: Docker/Podman container ID or name.
        session_id: Bernstein agent session ID.
        pid: PID of the container runtime process (for is_alive checks).
        created_at: Unix timestamp when the container was created.
        workspace_mount: Host path mounted as the agent workspace.
        runtime_cmd: The CLI command used (docker/podman).
    """

    container_id: str
    session_id: str
    pid: int | None = None
    created_at: float = field(default_factory=time.time)
    workspace_mount: str = ""
    runtime_cmd: str = "docker"


class ContainerError(Exception):
    """Raised when a container operation fails."""


# ---------------------------------------------------------------------------
# Runtime command builder
# ---------------------------------------------------------------------------


def _resolve_runtime_cmd(runtime: ContainerRuntime) -> str:
    """Resolve the CLI command for the given runtime.

    Args:
        runtime: Container runtime type.

    Returns:
        CLI command name.

    Raises:
        ContainerError: If the runtime CLI is not found on PATH.
    """
    if runtime in (ContainerRuntime.DOCKER, ContainerRuntime.GVISOR):
        cmd = "docker"
    elif runtime == ContainerRuntime.PODMAN:
        cmd = "podman"
    elif runtime == ContainerRuntime.FIRECRACKER:
        # Firecracker uses ignite as the CLI frontend
        cmd = "ignite"
    else:
        cmd = "docker"

    if shutil.which(cmd) is None:
        raise ContainerError(
            f"Container runtime CLI '{cmd}' not found on PATH. Install {runtime.value} or choose a different runtime."
        )
    return cmd


def _build_create_args(
    config: ContainerConfig,
    session_id: str,
    workspace_path: Path,
    env: dict[str, str],
    runtime_cmd: str,
) -> list[str]:
    """Build the `docker/podman create` argument list.

    Args:
        config: Container configuration.
        session_id: Agent session ID (used as container name).
        workspace_path: Host path to mount as /workspace.
        env: Environment variables to set in the container.
        runtime_cmd: The resolved runtime CLI command.

    Returns:
        Full command argument list for subprocess.
    """
    container_name = f"bernstein-{session_id}"
    args: list[str] = [runtime_cmd, "create", "--name", container_name]

    # Resource limits
    limits = config.resource_limits
    if limits.cpu_cores is not None:
        args.extend(["--cpus", str(limits.cpu_cores)])
    if limits.memory_mb is not None:
        args.extend(["--memory", f"{limits.memory_mb}m"])
    if limits.memory_swap_mb != -1:
        args.extend(["--memory-swap", f"{limits.memory_swap_mb}m"])
    if limits.pids_limit is not None:
        args.extend(["--pids-limit", str(limits.pids_limit)])
    if limits.disk_mb is not None:
        # Docker/Podman cannot quota a bind-mounted project directory portably.
        # We still cap the container writable layer and /tmp scratch space.
        args.extend(["--storage-opt", f"size={limits.disk_mb}m"])
        args.extend(["--tmpfs", f"/tmp:rw,size={limits.disk_mb}m"])
    if limits.read_only_rootfs:
        args.append("--read-only")

    # Security profile
    sec = config.security
    if sec.drop_capabilities:
        for cap in sec.drop_capabilities:
            args.extend(["--cap-drop", cap])
    if sec.no_new_privileges:
        args.append("--security-opt=no-new-privileges")
    if sec.seccomp_profile and sec.seccomp_profile != "default":
        args.append(f"--security-opt=seccomp={sec.seccomp_profile}")
    if sec.user:
        args.extend(["--user", sec.user])

    # gVisor runtime
    if config.runtime == ContainerRuntime.GVISOR:
        args.extend(["--runtime", "runsc"])

    # Network
    args.extend(["--network", config.network_mode.value])

    # Extra hosts
    for host_entry in config.extra_hosts:
        args.extend(["--add-host", host_entry])

    # Workspace mount — always bind the workspace directory
    args.extend(
        [
            "--volume",
            f"{workspace_path.resolve()}:/workspace:rw",
        ]
    )

    # .sdd state mount — share state directory for task server communication
    sdd_path = workspace_path / ".sdd"
    if sdd_path.exists():
        args.extend(
            [
                "--volume",
                f"{sdd_path.resolve()}:/workspace/.sdd:rw",
            ]
        )

    # Extra mounts
    for mount in config.extra_mounts:
        ro_flag = ":ro" if mount.read_only else ":rw"
        args.extend(["--volume", f"{mount.host_path}:{mount.container_path}{ro_flag}"])

    # Environment variables
    for key, value in sorted(env.items()):
        args.extend(["--env", f"{key}={value}"])

    # Labels for identification and cleanup
    all_labels = {
        "bernstein.session": session_id,
        "bernstein.managed": "true",
        **config.labels,
    }
    for label_key, label_val in sorted(all_labels.items()):
        args.extend(["--label", f"{label_key}={label_val}"])

    # Working directory inside container
    args.extend(["--workdir", "/workspace"])

    # Image
    args.append(config.image)

    return args


# ---------------------------------------------------------------------------
# Container manager
# ---------------------------------------------------------------------------


class ContainerManager:
    """Manage container lifecycle for agent sessions.

    Wraps Docker/Podman CLI to create, execute, inspect, and destroy
    containers.  Each agent session gets its own container with enforced
    resource limits and security constraints.

    Args:
        config: Container isolation configuration.
        workdir: Project working directory (mounted into containers).
    """

    def __init__(self, config: ContainerConfig, workdir: Path) -> None:
        self._config = config
        self._workdir = workdir.resolve()
        self._runtime_cmd = _resolve_runtime_cmd(config.runtime)
        self._handles: dict[str, ContainerHandle] = {}

    @property
    def config(self) -> ContainerConfig:
        """Return the container configuration."""
        return self._config

    def is_available(self) -> bool:
        """Check if the container runtime is available and responsive.

        Returns:
            True if the runtime daemon is reachable.
        """
        try:
            result = subprocess.run(
                [self._runtime_cmd, "info"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def create(
        self,
        session_id: str,
        *,
        env: dict[str, str] | None = None,
        workspace_override: Path | None = None,
    ) -> ContainerHandle:
        """Create a container for an agent session.

        Does NOT start the container — call :meth:`exec` to run commands.

        Args:
            session_id: Unique agent session identifier.
            env: Environment variables to set in the container.
            workspace_override: Override workspace path (e.g. worktree path).

        Returns:
            ContainerHandle for the created container.

        Raises:
            ContainerError: If container creation fails.
        """
        workspace = workspace_override or self._workdir
        container_env = env or {}

        args = _build_create_args(
            self._config,
            session_id,
            workspace,
            container_env,
            self._runtime_cmd,
        )

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=_CONTAINER_CMD_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContainerError(f"Container creation timed out for {session_id}") from exc
        except OSError as exc:
            raise ContainerError(f"Failed to create container for {session_id}: {exc}") from exc

        if result.returncode != 0:
            raise ContainerError(f"Container creation failed for {session_id}: {result.stderr.strip()}")

        container_id = result.stdout.strip()
        handle = ContainerHandle(
            container_id=container_id,
            session_id=session_id,
            workspace_mount=str(workspace),
            runtime_cmd=self._runtime_cmd,
        )
        self._handles[session_id] = handle
        logger.info(
            "Created container %s for session %s (image=%s, runtime=%s)",
            container_id[:12],
            session_id,
            self._config.image,
            self._config.runtime.value,
        )
        return handle

    def start(self, handle: ContainerHandle) -> None:
        """Start a created container.

        Args:
            handle: Container handle from :meth:`create`.

        Raises:
            ContainerError: If the container fails to start.
        """
        try:
            result = subprocess.run(
                [self._runtime_cmd, "start", handle.container_id],
                capture_output=True,
                text=True,
                timeout=_CONTAINER_CMD_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ContainerError(f"Failed to start container {handle.session_id}: {exc}") from exc

        if result.returncode != 0:
            raise ContainerError(f"Container start failed for {handle.session_id}: {result.stderr.strip()}")

        # Retrieve the container PID for process-level monitoring
        handle.pid = self._get_container_pid(handle)
        logger.info("Started container %s (pid=%s)", handle.container_id[:12], handle.pid)

    def exec(
        self,
        handle: ContainerHandle,
        cmd: list[str],
        *,
        detach: bool = True,
        timeout_s: int | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.Popen[bytes]:
        """Execute a command inside a running container.

        Args:
            handle: Container handle.
            cmd: Command and arguments to run.
            detach: If True, run in background and return a Popen object.
                If False, block until completion and return CompletedProcess.
            timeout_s: Timeout for blocking exec (ignored when detach=True).

        Returns:
            Popen when detached, CompletedProcess when blocking.

        Raises:
            ContainerError: If the exec command fails to launch.
        """
        exec_args = [self._runtime_cmd, "exec"]
        if detach:
            exec_args.append("-d")
        exec_args.extend([handle.container_id, *cmd])

        if detach:
            try:
                proc = subprocess.Popen(
                    exec_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                logger.info(
                    "Exec (detached) in container %s: %s",
                    handle.container_id[:12],
                    " ".join(cmd[:3]),
                )
                return proc
            except OSError as exc:
                raise ContainerError(f"Failed to exec in container {handle.session_id}: {exc}") from exc
        else:
            try:
                result = subprocess.run(
                    exec_args,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s or _CONTAINER_CMD_TIMEOUT_S,
                )
                return result
            except subprocess.TimeoutExpired as exc:
                raise ContainerError(f"Exec timed out in container {handle.session_id}") from exc
            except OSError as exc:
                raise ContainerError(f"Failed to exec in container {handle.session_id}: {exc}") from exc

    def spawn_in_container(
        self,
        session_id: str,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        workspace_override: Path | None = None,
        log_path: Path | None = None,
        network_mode_override: NetworkMode | None = None,
    ) -> ContainerHandle:
        """Create, start, and exec a command in a single call.

        This is the primary entry point for the spawner integration.
        Equivalent to create() + start() + exec(detach=True).

        Args:
            session_id: Agent session ID.
            cmd: Command to execute inside the container.
            env: Environment variables for the container.
            workspace_override: Override workspace path.
            log_path: Path to write container logs to.
            network_mode_override: Override the configured network mode.  Used
                by two-phase sandbox to enforce ``NetworkMode.NONE`` in Phase 2
                regardless of the base config's network_mode.

        Returns:
            ContainerHandle with the running container.

        Raises:
            ContainerError: If any step fails.
        """
        handle = self.create(session_id, env=env, workspace_override=workspace_override)

        # For long-running agents, use `docker run` instead of create+start+exec
        # to get proper signal forwarding and log streaming
        container_name = f"bernstein-{session_id}"
        run_args: list[str] = [self._runtime_cmd, "run", "-d", "--name", container_name]

        # Build a config with the network override applied when requested
        effective_config = self._config
        if network_mode_override is not None and network_mode_override != self._config.network_mode:
            import dataclasses

            effective_config = dataclasses.replace(self._config, network_mode=network_mode_override)

        # Apply same args as create (resource limits, security, mounts)
        create_args = _build_create_args(
            effective_config,
            session_id,
            workspace_override or self._workdir,
            env or {},
            self._runtime_cmd,
        )
        # Extract flags between "create" and the image name
        # Skip: [runtime, "create", "--name", container_name, ...flags..., image]
        flag_args = create_args[4:-1]  # Skip runtime, create, --name, name; drop image
        run_args.extend(flag_args)
        run_args.append(effective_config.image)
        run_args.extend(cmd)

        # Remove the created-but-not-started container first
        self._force_remove(handle)

        try:
            result = subprocess.run(
                run_args,
                capture_output=True,
                text=True,
                timeout=_CONTAINER_CMD_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ContainerError(f"Container run failed for {session_id}: {exc}") from exc

        if result.returncode != 0:
            raise ContainerError(f"Container run failed for {session_id}: {result.stderr.strip()}")

        handle.container_id = result.stdout.strip()
        handle.pid = self._get_container_pid(handle)

        # Stream logs to file if requested
        if log_path is not None:
            self._stream_logs(handle, log_path)

        logger.info(
            "Container %s running for session %s (pid=%s)",
            handle.container_id[:12],
            session_id,
            handle.pid,
        )
        return handle

    def is_alive(self, handle: ContainerHandle) -> bool:
        """Check if the container is still running.

        Args:
            handle: Container handle to check.

        Returns:
            True if the container is running.
        """
        try:
            result = subprocess.run(
                [
                    self._runtime_cmd,
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    handle.container_id,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip().lower() == "true"
        except (subprocess.TimeoutExpired, OSError):
            return False

    def get_exit_code(self, handle: ContainerHandle) -> int | None:
        """Get the exit code of a stopped container.

        Args:
            handle: Container handle.

        Returns:
            Exit code, or None if the container is still running.
        """
        try:
            result = subprocess.run(
                [
                    self._runtime_cmd,
                    "inspect",
                    "--format",
                    "{{.State.ExitCode}}",
                    handle.container_id,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                code = result.stdout.strip()
                return int(code) if code else None
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return None

    def get_resource_usage(self, handle: ContainerHandle) -> dict[str, Any]:
        """Get current resource usage for a running container.

        Args:
            handle: Container handle.

        Returns:
            Dict with cpu_percent, memory_mb, memory_limit_mb, pids.
        """
        try:
            result = subprocess.run(
                [
                    self._runtime_cmd,
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                    handle.container_id,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                stats: dict[str, Any] = json.loads(result.stdout.strip())
                return {
                    "cpu_percent": stats.get("CPUPerc", "0%"),
                    "memory_usage": stats.get("MemUsage", "0B / 0B"),
                    "memory_percent": stats.get("MemPerc", "0%"),
                    "pids": stats.get("PIDs", "0"),
                    "net_io": stats.get("NetIO", "0B / 0B"),
                    "block_io": stats.get("BlockIO", "0B / 0B"),
                }
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
        return {}

    def stop(self, handle: ContainerHandle, timeout_s: int = 10) -> None:
        """Stop a running container gracefully.

        Sends SIGTERM, waits for timeout_s, then SIGKILL if still running.

        Args:
            handle: Container handle.
            timeout_s: Seconds to wait before force-killing.
        """
        try:
            subprocess.run(
                [self._runtime_cmd, "stop", "-t", str(timeout_s), handle.container_id],
                capture_output=True,
                timeout=timeout_s + _CONTAINER_DESTROY_TIMEOUT_S,
            )
            logger.info("Stopped container %s", handle.container_id[:12])
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Failed to stop container %s: %s", handle.session_id, exc)
            self._force_kill(handle)

    def destroy(self, handle: ContainerHandle) -> None:
        """Stop and remove a container, cleaning up all resources.

        Args:
            handle: Container handle to destroy.
        """
        self.stop(handle)
        self._force_remove(handle)
        self._handles.pop(handle.session_id, None)
        logger.info("Destroyed container for session %s", handle.session_id)

    def cleanup_stale(self) -> int:
        """Remove any stale Bernstein containers that are no longer tracked.

        Queries for containers with the ``bernstein.managed=true`` label that
        are not in the current handles dict.

        Returns:
            Number of stale containers removed.
        """
        try:
            result = subprocess.run(
                [
                    self._runtime_cmd,
                    "ps",
                    "-a",
                    "--filter",
                    "label=bernstein.managed=true",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=_CONTAINER_CMD_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError):
            return 0

        if result.returncode != 0:
            return 0

        removed = 0
        tracked_names = {f"bernstein-{sid}" for sid in self._handles}
        for name in result.stdout.strip().splitlines():
            name = name.strip()
            if not name or name in tracked_names:
                continue
            try:
                subprocess.run(
                    [self._runtime_cmd, "rm", "-f", name],
                    capture_output=True,
                    timeout=_CONTAINER_DESTROY_TIMEOUT_S,
                )
                removed += 1
                logger.info("Cleaned up stale container: %s", name)
            except (subprocess.TimeoutExpired, OSError):
                pass

        return removed

    def list_active(self) -> list[str]:
        """Return session IDs of currently tracked containers.

        Returns:
            List of active session IDs.
        """
        return list(self._handles.keys())

    def get_handle(self, session_id: str) -> ContainerHandle | None:
        """Look up a container handle by session ID.

        Args:
            session_id: The session to look up.

        Returns:
            ContainerHandle or None if not tracked.
        """
        return self._handles.get(session_id)

    def run_phase1_setup(
        self,
        session_id: str,
        setup_cmds: list[str],
        *,
        env: dict[str, str] | None = None,
        workspace_override: Path | None = None,
        timeout_s: int = 300,
    ) -> bool:
        """Run Phase 1 setup in a short-lived container with network access.

        Creates a temporary container using ``phase1_network_mode`` (default
        ``bridge``), runs each setup command sequentially, then removes the
        container.  The workspace is bind-mounted so any installed artefacts
        (virtualenvs, node_modules, etc.) persist for Phase 2.

        Args:
            session_id: Agent session ID (used to name the setup container).
            setup_cmds: Shell commands to execute, e.g. ``["uv sync --frozen"]``.
            env: Environment variables to pass into the container.
            workspace_override: Override workspace path.
            timeout_s: Maximum seconds to wait for all setup commands.

        Returns:
            True if all commands succeeded, False if any failed or timed out.
        """
        if not setup_cmds:
            return True

        workspace = workspace_override or self._workdir
        container_env = env or {}

        # Build a temporary config that overrides network mode to allow internet
        # access during setup.
        phase1_network = NetworkMode.BRIDGE
        if self._config.two_phase_sandbox is not None:
            phase1_network = self._config.two_phase_sandbox.phase1_network_mode

        # Build run args for the Phase 1 container
        setup_session_id = f"{session_id}-setup"
        container_name = f"bernstein-{setup_session_id}"

        # Construct a combined shell command that runs all setup steps
        shell_script = " && ".join(setup_cmds)
        run_args: list[str] = [self._runtime_cmd, "run", "--rm", "--name", container_name]

        # Resource limits (reuse from config, but relax for setup)
        limits = self._config.resource_limits
        if limits.cpu_cores is not None:
            run_args.extend(["--cpus", str(limits.cpu_cores)])
        if limits.memory_mb is not None:
            run_args.extend(["--memory", f"{limits.memory_mb}m"])

        # Security profile
        sec = self._config.security
        if sec.drop_capabilities:
            for cap in sec.drop_capabilities:
                run_args.extend(["--cap-drop", cap])
        if sec.no_new_privileges:
            run_args.append("--security-opt=no-new-privileges")
        if sec.user:
            run_args.extend(["--user", sec.user])

        # Network — Phase 1 needs internet access
        run_args.extend(["--network", phase1_network.value])

        # Workspace mount
        run_args.extend(["--volume", f"{workspace.resolve()}:/workspace:rw"])

        # .sdd state mount
        sdd_path = workspace / ".sdd"
        if sdd_path.exists():
            run_args.extend(["--volume", f"{sdd_path.resolve()}:/workspace/.sdd:rw"])

        # Environment variables
        for key, value in sorted(container_env.items()):
            run_args.extend(["--env", f"{key}={value}"])

        # Labels
        run_args.extend(
            [
                "--label",
                f"bernstein.session={setup_session_id}",
                "--label",
                "bernstein.phase=setup",
                "--label",
                "bernstein.managed=true",
            ]
        )

        run_args.extend(["--workdir", "/workspace"])
        run_args.append(self._config.image)
        run_args.extend(["sh", "-c", shell_script])

        logger.info(
            "Phase 1 setup for session %s: running %r (timeout=%ds)",
            session_id,
            shell_script,
            timeout_s,
        )

        try:
            result = subprocess.run(
                run_args,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Phase 1 setup timed out after %ds for session %s",
                timeout_s,
                session_id,
            )
            return False
        except OSError as exc:
            logger.warning("Phase 1 setup failed to launch for session %s: %s", session_id, exc)
            return False

        if result.returncode != 0:
            logger.warning(
                "Phase 1 setup exited with code %d for session %s.\nstdout: %s\nstderr: %s",
                result.returncode,
                session_id,
                result.stdout[-500:] if result.stdout else "",
                result.stderr[-500:] if result.stderr else "",
            )
            return False

        logger.info("Phase 1 setup completed successfully for session %s", session_id)
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_container_pid(self, handle: ContainerHandle) -> int | None:
        """Retrieve the PID of the container's init process."""
        try:
            result = subprocess.run(
                [
                    self._runtime_cmd,
                    "inspect",
                    "--format",
                    "{{.State.Pid}}",
                    handle.container_id,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                pid_str = result.stdout.strip()
                return int(pid_str) if pid_str and pid_str != "0" else None
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return None

    def _force_kill(self, handle: ContainerHandle) -> None:
        """Force-kill a container that didn't respond to stop."""
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            subprocess.run(
                [self._runtime_cmd, "kill", handle.container_id],
                capture_output=True,
                timeout=10,
            )

    def _force_remove(self, handle: ContainerHandle) -> None:
        """Force-remove a container (stopped or running)."""
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            subprocess.run(
                [self._runtime_cmd, "rm", "-f", handle.container_id],
                capture_output=True,
                timeout=_CONTAINER_DESTROY_TIMEOUT_S,
            )

    def _stream_logs(self, handle: ContainerHandle, log_path: Path) -> None:
        """Start background log streaming from container to file."""
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("w")
            subprocess.Popen(
                [self._runtime_cmd, "logs", "-f", handle.container_id],
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            logger.warning("Failed to stream logs for %s: %s", handle.session_id, exc)


# ---------------------------------------------------------------------------
# Image builder
# ---------------------------------------------------------------------------


def ensure_agent_image(
    runtime_cmd: str = "docker",
    image_name: str = "bernstein-agent:latest",
    dockerfile: Path | None = None,
    build_context: Path | None = None,
) -> bool:
    """Ensure the agent container image exists, building it if necessary.

    Args:
        runtime_cmd: Docker/Podman CLI command.
        image_name: Image name:tag to check/build.
        dockerfile: Path to Dockerfile. Defaults to project Dockerfile.
        build_context: Build context directory. Defaults to project root.

    Returns:
        True if the image is ready, False if build failed.
    """
    # Check if image already exists
    try:
        result = subprocess.run(
            [runtime_cmd, "image", "inspect", image_name],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Build the image
    build_args = [runtime_cmd, "build", "-t", image_name]
    if dockerfile is not None:
        build_args.extend(["-f", str(dockerfile)])
    build_args.append(str(build_context or Path(".")))

    logger.info("Building agent image: %s", image_name)
    try:
        result = subprocess.run(
            build_args,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes for image build
        )
        if result.returncode != 0:
            logger.error("Image build failed: %s", result.stderr[:500])
            return False
        logger.info("Built agent image: %s", image_name)
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("Image build failed: %s", exc)
        return False
