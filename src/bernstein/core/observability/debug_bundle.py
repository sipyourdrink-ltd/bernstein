"""Debug bundle generator with comprehensive secret redaction.

Creates a zip archive containing diagnostic information for bug reports.
Every piece of collected text passes through aggressive secret redaction
before being written to the archive -- better to over-redact than leak.

Usage::

    from bernstein.core.observability.debug_bundle import (
        create_debug_bundle,
        BundleConfig,
    )

    path, manifest = create_debug_bundle(Path("."))
    print(f"Bundle: {path}  ({manifest.redactions_applied} secrets redacted)")
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import bernstein

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_REDACTED: Final[str] = "***REDACTED***"


@dataclass(frozen=True)
class BundleManifest:
    """What's included in the debug bundle."""

    bernstein_version: str
    platform_info: str
    timestamp: str
    files_included: tuple[str, ...]
    redactions_applied: int


@dataclass(frozen=True)
class BundleConfig:
    """Configuration for bundle generation.

    Attributes:
        output_path: Where to write the zip.  ``None`` auto-generates a
            timestamped name in the current working directory.
        extended: When ``True``, include full (un-truncated) logs.
        max_log_lines: Maximum lines to keep from server/orchestrator logs.
        max_agent_logs: Maximum number of agent log files to include.
        max_agent_log_lines: Maximum lines per agent log.
        max_task_records: Maximum task JSONL records to include.
        max_archive_records: Maximum archive JSONL records to include.
    """

    output_path: Path | None = field(default=None)
    extended: bool = field(default=False)
    max_log_lines: int = field(default=1000)
    max_agent_logs: int = field(default=5)
    max_agent_log_lines: int = field(default=500)
    max_task_records: int = field(default=100)
    max_archive_records: int = field(default=50)


# ---------------------------------------------------------------------------
# Secret redaction patterns
# ---------------------------------------------------------------------------

# Order matters -- more specific patterns first to avoid partial matches.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # SSH private keys (multiline block)
    (
        "ssh_key",
        re.compile(
            r"-----BEGIN [A-Z ]*KEY-----[\s\S]*?-----END [A-Z ]*KEY-----",
            re.MULTILINE,
        ),
    ),
    # JWT tokens (three base64 segments separated by dots)
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    # Bearer tokens in headers
    (
        "bearer",
        re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE),
    ),
    # URLs with embedded credentials  https://user:pass@host
    (
        "url_cred",
        re.compile(r"(https?://)[^\s:]+:[^\s@]+@"),
    ),
    # Environment variable assignments for sensitive keys
    (
        "env_export",
        re.compile(
            r"((?:export\s+)?[A-Z_]*(?:SECRET|TOKEN|PASSWORD|KEY|CREDENTIAL)[A-Z_]*\s*=\s*)"
            r"[^\s]+",
            re.IGNORECASE,
        ),
    ),
    # Explicit KEY=value pairs (e.g. ANTHROPIC_API_KEY=sk-ant-...)
    (
        "api_key_assign",
        re.compile(
            r"([A-Z_]*(?:API_KEY|_TOKEN|_SECRET)\s*=\s*)[^\s]+",
            re.IGNORECASE,
        ),
    ),
    # YAML sensitive values: lines like ``token: <value>``
    (
        "yaml_sensitive",
        re.compile(
            r"((?:token|key|secret|password|credential|auth)[a-z_]*\s*:\s*)"
            r"[^\s#].*",
            re.IGNORECASE,
        ),
    ),
    # Email addresses
    (
        "email",
        re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
    ),
]


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace secrets in *text* with ``***REDACTED***``.

    Applies an aggressive set of patterns covering API keys, tokens,
    passwords, SSH keys, JWTs, emails, and YAML sensitive values.

    Args:
        text: Arbitrary string that may contain secrets.

    Returns:
        Tuple of (redacted text, number of redactions applied).
    """
    count = 0
    result = text
    for label, pattern in _SECRET_PATTERNS:

        def _replacer(m: re.Match[str], _label: str = label) -> str:
            nonlocal count
            count += 1
            # For patterns with a captured prefix group, keep the prefix.
            if _label in ("bearer", "url_cred", "env_export", "api_key_assign", "yaml_sensitive"):
                return m.group(1) + _REDACTED
            return _REDACTED

        result = pattern.sub(_replacer, result)
    return result, count


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------


def collect_version_info() -> str:
    """Collect bernstein version, Python version, OS info.

    Returns:
        Human-readable multi-line version string.
    """
    lines = [
        f"bernstein: {bernstein.__version__}",
        f"python: {platform.python_version()}",
        f"os: {platform.platform()}",
    ]
    return "\n".join(lines)


def collect_platform_info() -> str:
    """Collect OS, architecture, Python, shell, terminal, and disk space.

    Returns:
        Human-readable multi-line platform summary.
    """
    import os

    lines = [
        f"system: {platform.system()}",
        f"release: {platform.release()}",
        f"machine: {platform.machine()}",
        f"python: {platform.python_version()} ({platform.python_implementation()})",
        f"shell: {os.environ.get('SHELL', 'unknown')}",
        f"terminal: {os.environ.get('TERM', 'unknown')}",
    ]

    # Disk space for cwd
    try:
        usage = shutil.disk_usage(".")
        gib = 1024**3
        lines.append(f"disk: {usage.free / gib:.1f} GiB free / {usage.total / gib:.1f} GiB total")
    except OSError:
        lines.append("disk: unavailable")

    return "\n".join(lines)


def collect_config(workdir: Path) -> tuple[str, int]:
    """Read ``bernstein.yaml`` from *workdir*, redact secrets.

    Args:
        workdir: Project root containing ``bernstein.yaml``.

    Returns:
        Tuple of (redacted config text, redaction count).
        Returns a placeholder message if the file does not exist.
    """
    config_path = workdir / "bernstein.yaml"
    if not config_path.is_file():
        return "# bernstein.yaml not found\n", 0
    try:
        raw = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "# bernstein.yaml could not be read\n", 0
    return redact_secrets(raw)


def _tail_lines(path: Path, max_lines: int) -> str:
    """Read the last *max_lines* from *path*."""
    try:
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if len(all_lines) <= max_lines:
        return "\n".join(all_lines)
    return "\n".join(all_lines[-max_lines:])


def collect_logs(workdir: Path, config: BundleConfig) -> dict[str, str]:
    """Collect server, orchestrator, and agent logs.

    Logs are truncated to the configured maximums (unless ``extended``
    mode is enabled) and always redacted.

    Args:
        workdir: Project root containing ``.sdd/logs/``.
        config: Bundle configuration with truncation limits.

    Returns:
        Mapping of filename to redacted log content.
    """
    log_dir = workdir / ".sdd" / "logs"
    result: dict[str, str] = {}

    max_lines = 0 if config.extended else config.max_log_lines

    # Server and orchestrator logs
    for name in ("server.log", "orchestrator.log"):
        log_path = log_dir / name
        if log_path.is_file():
            raw = _tail_lines(log_path, max_lines) if max_lines > 0 else _read_safe(log_path)
            redacted, _ = redact_secrets(raw)
            result[name] = redacted

    # Agent logs — pick the most recent ones
    agent_max = 0 if config.extended else config.max_agent_log_lines
    agent_logs = sorted(log_dir.glob("agent_*.log"), key=_mtime_safe, reverse=True)
    for agent_log in agent_logs[: config.max_agent_logs]:
        raw = _tail_lines(agent_log, agent_max) if agent_max > 0 else _read_safe(agent_log)
        redacted, _ = redact_secrets(raw)
        result[agent_log.name] = redacted

    return result


def collect_state(workdir: Path, config: BundleConfig) -> dict[str, str]:
    """Collect task records, archive tail, and runtime summary.

    Args:
        workdir: Project root containing ``.sdd/``.
        config: Bundle configuration with record limits.

    Returns:
        Mapping of filename to redacted state content.
    """
    sdd = workdir / ".sdd"
    result: dict[str, str] = {}

    # Task records (backlog/tasks.jsonl or similar)
    tasks_path = sdd / "backlog" / "tasks.jsonl"
    if tasks_path.is_file():
        raw = _tail_lines(tasks_path, config.max_task_records)
        redacted, _ = redact_secrets(raw)
        result["tasks.jsonl"] = redacted

    # Archive tail
    archive_path = sdd / "archive" / "completed.jsonl"
    if archive_path.is_file():
        raw = _tail_lines(archive_path, config.max_archive_records)
        redacted, _ = redact_secrets(raw)
        result["archive_tail.jsonl"] = redacted

    # Runtime summary
    runtime_path = sdd / "runtime"
    if runtime_path.is_dir():
        summary: dict[str, str] = {}
        for p in sorted(runtime_path.iterdir()):
            if p.is_file() and p.stat().st_size < 64 * 1024:
                try:
                    raw_content = p.read_text(encoding="utf-8", errors="replace")
                    redacted_content, _ = redact_secrets(raw_content)
                    summary[p.name] = redacted_content
                except OSError:
                    continue
        if summary:
            result["runtime_summary.json"] = json.dumps(summary, indent=2)

    return result


def collect_diagnostics(workdir: Path) -> dict[str, str]:
    """Collect disk space, git status, and worktree list.

    Args:
        workdir: Project root directory.

    Returns:
        Mapping of filename to diagnostic content.
    """
    result: dict[str, str] = {}

    # Disk space
    try:
        usage = shutil.disk_usage(str(workdir))
        gib = 1024**3
        result["disk_space.txt"] = (
            f"path: {workdir}\n"
            f"total: {usage.total / gib:.1f} GiB\n"
            f"used:  {usage.used / gib:.1f} GiB\n"
            f"free:  {usage.free / gib:.1f} GiB\n"
        )
    except OSError:
        result["disk_space.txt"] = "unavailable\n"

    # Git status
    result["git_status.txt"] = _run_git(workdir, ["git", "status", "--short"])

    # Worktree list
    result["worktree_list.txt"] = _run_git(workdir, ["git", "worktree", "list"])

    return result


def generate_readme(manifest: BundleManifest) -> str:
    """Generate a README for the debug bundle.

    Args:
        manifest: Bundle metadata.

    Returns:
        Markdown-formatted README text.
    """
    return (
        "# Bernstein Debug Bundle\n"
        "\n"
        f"Generated: {manifest.timestamp}\n"
        f"Version:   {manifest.bernstein_version}\n"
        f"Platform:  {manifest.platform_info}\n"
        f"Files:     {len(manifest.files_included)}\n"
        f"Redactions: {manifest.redactions_applied}\n"
        "\n"
        "## How to use\n"
        "\n"
        "Attach this zip file to your GitHub issue:\n"
        "  https://github.com/chernistry/bernstein/issues/new\n"
        "\n"
        "All secrets, tokens, and email addresses have been redacted.\n"
        "Review the contents before sharing if you have concerns.\n"
        "\n"
        "## Contents\n"
        "\n" + "\n".join(f"- {f}" for f in manifest.files_included) + "\n"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def create_debug_bundle(
    workdir: Path,
    config: BundleConfig | None = None,
) -> tuple[Path, BundleManifest]:
    """Create a zip debug bundle with all diagnostics.

    Collects version info, platform details, configuration, logs,
    state, and diagnostics.  Every text artifact is redacted before
    being written to the archive.

    Args:
        workdir: Project root directory.
        config: Optional configuration; defaults are used when ``None``.

    Returns:
        Tuple of (path to zip file, manifest describing the bundle).
    """
    if config is None:
        config = BundleConfig()

    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"bernstein-debug-{ts}"

    output_path = config.output_path or Path.cwd() / f"{prefix}.zip"
    output_path = output_path.resolve()

    total_redactions = 0
    files_included: list[str] = []

    # Mapping of archive-path -> content to write
    entries: dict[str, str] = {}

    # Version info
    entries[f"{prefix}/bernstein_version.txt"] = collect_version_info()

    # Platform info
    entries[f"{prefix}/platform.txt"] = collect_platform_info()

    # Config
    config_text, cfg_redactions = collect_config(workdir)
    total_redactions += cfg_redactions
    entries[f"{prefix}/config/bernstein.yaml"] = config_text

    # Logs
    logs = collect_logs(workdir, config)
    for name, content in logs.items():
        entries[f"{prefix}/logs/{name}"] = content

    # State
    state = collect_state(workdir, config)
    for name, content in state.items():
        entries[f"{prefix}/state/{name}"] = content

    # Diagnostics
    diags = collect_diagnostics(workdir)
    for name, content in diags.items():
        entries[f"{prefix}/diagnostics/{name}"] = content

    # Build file list for manifest (before adding README)
    files_included = sorted(entries.keys())

    # Final redaction pass over all entries (catches anything collectors missed)
    for key in entries:
        redacted, n = redact_secrets(entries[key])
        total_redactions += n
        entries[key] = redacted

    plat_info = f"{platform.system()} {platform.machine()}"
    manifest = BundleManifest(
        bernstein_version=bernstein.__version__,
        platform_info=plat_info,
        timestamp=ts,
        files_included=tuple(files_included),
        redactions_applied=total_redactions,
    )

    # README (generated from manifest, so added last)
    readme = generate_readme(manifest)
    entries[f"{prefix}/README.md"] = readme
    files_included.append(f"{prefix}/README.md")

    # Update manifest with final file list
    manifest = BundleManifest(
        bernstein_version=manifest.bernstein_version,
        platform_info=manifest.platform_info,
        timestamp=manifest.timestamp,
        files_included=tuple(sorted(files_included)),
        redactions_applied=manifest.redactions_applied,
    )

    # Write zip
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc_name, content in sorted(entries.items()):
            zf.writestr(arc_name, content)

    return output_path, manifest


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _read_safe(path: Path) -> str:
    """Read a file, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _mtime_safe(path: Path) -> float:
    """Return mtime of *path*, or 0.0 on error."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _run_git(workdir: Path, cmd: list[str]) -> str:
    """Run a git command in *workdir*, return stdout or error message."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout or proc.stderr or "(empty)\n"
    except (OSError, subprocess.TimeoutExpired):
        return "(git command failed)\n"
