"""MCP server sandboxing — optional container isolation for untrusted servers.

Provides configuration dataclasses and command-building helpers to wrap
MCP server subprocesses in Docker/Podman containers with resource limits,
filesystem restrictions, and network isolation.

Usage::

    config = load_sandbox_config(Path("bernstein.yaml"))
    profile = BUILTIN_PROFILES[config.profile]
    cmd = build_sandbox_command(
        server_command=["npx", "-y", "some-mcp-server"],
        config=config,
        profile=profile,
        workdir="/path/to/project",
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

_VALID_RUNTIMES = frozenset({"docker", "podman"})


# ---------------------------------------------------------------------------
# SandboxProfile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxProfile:
    """Security profile controlling container resource limits and access.

    Args:
        name: Human-readable profile identifier.
        read_only_paths: Host paths mounted read-only into the container.
        writable_paths: Host paths mounted read-write into the container.
        network_enabled: Whether outbound network access is allowed.
        memory_limit_mb: Container memory ceiling in MiB.
        cpu_limit: CPU quota (1.0 = one full core).
        timeout_seconds: Maximum wall-clock runtime before the container
            is forcibly stopped.
    """

    name: str
    read_only_paths: list[str] = field(default_factory=lambda: list[str]())
    writable_paths: list[str] = field(default_factory=lambda: list[str]())
    network_enabled: bool = False
    memory_limit_mb: int = 512
    cpu_limit: float = 1.0
    timeout_seconds: int = 300


BUILTIN_PROFILES: dict[str, SandboxProfile] = {
    "strict": SandboxProfile(
        name="strict",
        read_only_paths=["/"],
        writable_paths=[],
        network_enabled=False,
        memory_limit_mb=256,
        cpu_limit=0.5,
        timeout_seconds=120,
    ),
    "standard": SandboxProfile(
        name="standard",
        read_only_paths=["/"],
        writable_paths=["."],
        network_enabled=False,
        memory_limit_mb=512,
        cpu_limit=1.0,
        timeout_seconds=300,
    ),
    "permissive": SandboxProfile(
        name="permissive",
        read_only_paths=[],
        writable_paths=["."],
        network_enabled=True,
        memory_limit_mb=1024,
        cpu_limit=2.0,
        timeout_seconds=600,
    ),
}


# ---------------------------------------------------------------------------
# SandboxConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxConfig:
    """Top-level sandbox configuration for MCP server processes.

    Args:
        enabled: Whether container sandboxing is active.
        profile: Name of the :class:`SandboxProfile` to apply.
        runtime: Container runtime binary (``docker`` or ``podman``).
        image: Container image used for sandboxed execution.
        extra_mounts: Additional ``-v`` mount specifications
            (``host:container[:opts]`` format).
    """

    enabled: bool = False
    profile: str = "standard"
    runtime: str = "docker"
    image: str = "bernstein/mcp-sandbox:latest"
    extra_mounts: list[str] = field(default_factory=lambda: list[str]())


# ---------------------------------------------------------------------------
# build_sandbox_command
# ---------------------------------------------------------------------------


def build_sandbox_command(
    server_command: list[str],
    config: SandboxConfig,
    profile: SandboxProfile,
    workdir: str,
) -> list[str]:
    """Wrap a server command inside a container runtime invocation.

    Args:
        server_command: Original command and arguments for the MCP server.
        config: Sandbox configuration (runtime, image, extra mounts).
        profile: Security profile controlling limits and access.
        workdir: Host working directory to map into the container.

    Returns:
        Full command list starting with the container runtime binary,
        followed by ``run`` flags and the original server command.
    """
    cmd: list[str] = [config.runtime, "run", "--rm"]

    # Resource limits
    cmd.extend(["--memory", f"{profile.memory_limit_mb}m"])
    cmd.extend(["--cpus", str(profile.cpu_limit)])

    # Timeout via stop-timeout
    cmd.extend(["--stop-timeout", str(profile.timeout_seconds)])

    # Network
    if not profile.network_enabled:
        cmd.append("--network=none")

    # Read-only root filesystem when read_only_paths includes "/"
    if "/" in profile.read_only_paths:
        cmd.append("--read-only")

    # Workdir inside container
    cmd.extend(["-w", "/workspace"])

    # Mount workdir
    if profile.writable_paths:
        cmd.extend(["-v", f"{workdir}:/workspace:rw"])
    else:
        cmd.extend(["-v", f"{workdir}:/workspace:ro"])

    # Read-only mounts (excluding "/" which is handled via --read-only)
    for ro_path in profile.read_only_paths:
        if ro_path == "/":
            continue
        cmd.extend(["-v", f"{ro_path}:{ro_path}:ro"])

    # Writable mounts (excluding "." which maps to workdir above)
    for rw_path in profile.writable_paths:
        if rw_path == ".":
            continue
        cmd.extend(["-v", f"{rw_path}:{rw_path}:rw"])

    # Extra mounts from config
    for mount in config.extra_mounts:
        cmd.extend(["-v", mount])

    # Image
    cmd.append(config.image)

    # Original server command
    cmd.extend(server_command)

    return cmd


# ---------------------------------------------------------------------------
# validate_sandbox_config
# ---------------------------------------------------------------------------


def validate_sandbox_config(config: SandboxConfig) -> list[str]:
    """Check a sandbox configuration for common errors.

    Args:
        config: The configuration to validate.

    Returns:
        List of human-readable error strings.  Empty list means valid.
    """
    errors: list[str] = []

    if config.runtime not in _VALID_RUNTIMES:
        errors.append(f"Invalid runtime '{config.runtime}'; must be one of {sorted(_VALID_RUNTIMES)}")

    if config.profile not in BUILTIN_PROFILES:
        errors.append(f"Unknown profile '{config.profile}'; available profiles: {sorted(BUILTIN_PROFILES)}")

    if not config.image:
        errors.append("Sandbox image must not be empty")

    return errors


# ---------------------------------------------------------------------------
# load_sandbox_config
# ---------------------------------------------------------------------------


def load_sandbox_config(yaml_path: Path | None = None) -> SandboxConfig:
    """Load sandbox configuration from a YAML file.

    Reads the ``mcp_sandbox`` top-level key from the given YAML file.
    When *yaml_path* is ``None`` or the file/key is absent, returns a
    default (disabled) :class:`SandboxConfig`.

    Args:
        yaml_path: Path to a ``bernstein.yaml`` configuration file.

    Returns:
        Parsed sandbox configuration.
    """
    if yaml_path is None:
        return SandboxConfig()

    import yaml  # lazy import — only needed when a path is provided

    try:
        with open(yaml_path) as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return SandboxConfig()

    if not isinstance(data, dict):
        return SandboxConfig()

    data_typed = cast("dict[str, Any]", data)
    section_raw: Any = data_typed.get("mcp_sandbox")
    if not isinstance(section_raw, dict):
        return SandboxConfig()

    section = cast("dict[str, Any]", section_raw)

    extra_mounts_raw: Any = section.get("extra_mounts", [])
    extra_mounts: list[str] = (
        [str(m) for m in cast("list[Any]", extra_mounts_raw)] if isinstance(extra_mounts_raw, list) else []
    )

    return SandboxConfig(
        enabled=bool(section.get("enabled", False)),
        profile=str(section.get("profile", "standard")),
        runtime=str(section.get("runtime", "docker")),
        image=str(section.get("image", "bernstein/mcp-sandbox:latest")),
        extra_mounts=extra_mounts,
    )
