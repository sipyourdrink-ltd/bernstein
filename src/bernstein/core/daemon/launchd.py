"""launchd helpers for installing Bernstein as a macOS user agent."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from bernstein.core.daemon._render import render_template
from bernstein.core.daemon.errors import UnitExistsError

__all__ = [
    "DEFAULT_LABEL",
    "DEFAULT_PLIST_NAME",
    "default_plist_dir",
    "install_launchd_plist",
    "load",
    "parse_status_output",
    "render_launchd_plist",
    "start",
    "status",
    "stop",
    "uninstall",
    "unload",
]

DEFAULT_LABEL = "com.bernstein.daemon"
DEFAULT_PLIST_NAME = f"{DEFAULT_LABEL}.plist"


def default_plist_dir() -> Path:
    """Return the default directory for user-scope launchd agents."""
    return Path(os.path.expanduser("~/Library/LaunchAgents"))


def _cmd_lines(command: str) -> str:
    """Render the ``ProgramArguments`` block from a shell-style command."""
    parts = shlex.split(command)
    indented = [f"      <string>{part}</string>" for part in parts]
    return "\n".join(indented)


def _env_lines(env: dict[str, str]) -> str:
    """Render extra ``EnvironmentVariables`` entries."""
    out: list[str] = []
    for key, value in env.items():
        out.append(f"      <key>{key}</key>")
        out.append(f"      <string>{value}</string>")
    return "\n".join(out)


def render_launchd_plist(
    command: str,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
    path_env: str | None = None,
) -> str:
    """Render the launchd plist content for ``command``.

    Args:
        command: Shell-style command line (split via ``shlex``).
        env: Extra environment variables (beyond ``PATH``).
        workdir: Optional working directory; defaults to ``$HOME``.
        path_env: Optional ``PATH`` override; defaults to the current
            process's ``PATH``.

    Returns:
        The rendered plist XML.
    """
    mapping = {
        "CMD": _cmd_lines(command),
        "WORKDIR": workdir if workdir is not None else str(Path.home()),
        "PATH": path_env if path_env is not None else os.environ.get("PATH", ""),
        "ENV_LINES": _env_lines(env or {}),
    }
    return render_template("launchd.plist.template", mapping)


def install_launchd_plist(
    command: str,
    plist_dir: Path | None = None,
    env: dict[str, str] | None = None,
    plist_name: str = DEFAULT_PLIST_NAME,
    workdir: str | None = None,
    path_env: str | None = None,
    force: bool = False,
) -> Path:
    """Install a launchd user agent plist that runs ``command``.

    Args:
        command: Shell-style command line.
        plist_dir: Directory for the plist file; defaults to
            ``~/Library/LaunchAgents``.
        env: Extra environment variables to embed in the plist.
        plist_name: File name for the plist.
        workdir: Working directory set on the agent.
        path_env: ``PATH`` value baked into the plist.
        force: If ``True``, overwrite an existing plist.

    Returns:
        Path to the written plist file.

    Raises:
        UnitExistsError: When the plist exists and ``force`` is ``False``.
    """
    target_dir = plist_dir if plist_dir is not None else default_plist_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    plist_path = target_dir / plist_name
    if plist_path.exists() and not force:
        raise UnitExistsError(f"Plist already exists: {plist_path} (use --force to overwrite)")
    content = render_launchd_plist(command, env=env, workdir=workdir, path_env=path_env)
    plist_path.write_text(content, encoding="utf-8")
    return plist_path


def uninstall(
    plist_dir: Path | None = None,
    plist_name: str = DEFAULT_PLIST_NAME,
) -> bool:
    """Remove a previously installed launchd plist.

    Args:
        plist_dir: Directory holding the plist.
        plist_name: Plist file name.

    Returns:
        ``True`` if the plist was removed, ``False`` if it was not
        present (idempotent).
    """
    target_dir = plist_dir if plist_dir is not None else default_plist_dir()
    plist_path = target_dir / plist_name
    if not plist_path.exists():
        return False
    plist_path.unlink()
    return True


def _launchctl(
    *args: str,
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``launchctl`` with the provided arguments."""
    cmd = ["launchctl", *args]
    run = runner if runner is not None else subprocess.run
    return run(cmd, capture_output=True, text=True, check=False)  # type: ignore[operator,no-any-return]


def load(
    plist_path: Path,
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Load an agent plist into ``launchd``."""
    return _launchctl("load", str(plist_path), runner=runner)


def unload(
    plist_path: Path,
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Unload an agent plist from ``launchd``."""
    return _launchctl("unload", str(plist_path), runner=runner)


def start(
    label: str = DEFAULT_LABEL,
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Start a loaded launchd agent by label."""
    return _launchctl("start", label, runner=runner)


def stop(
    label: str = DEFAULT_LABEL,
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Stop a running launchd agent by label."""
    return _launchctl("stop", label, runner=runner)


def status(
    label: str = DEFAULT_LABEL,
    runner: object | None = None,
) -> str:
    """Return a normalized status string for a launchd agent.

    Parses ``launchctl list <label>`` output and collapses it into one
    of ``"Running"``, ``"Stopped"``, ``"Failed"``, or ``"Unknown"``.
    """
    completed = _launchctl("list", label, runner=runner)
    return parse_status_output(completed.stdout or "", completed.returncode)


def parse_status_output(output: str, returncode: int = 0) -> str:
    """Classify ``launchctl list <label>`` output.

    Args:
        output: Stdout from ``launchctl list``.
        returncode: Exit code from ``launchctl``. Non-zero typically
            means the label is unknown to launchd.

    Returns:
        ``"Running"``, ``"Stopped"``, ``"Failed"``, or ``"Unknown"``.
    """
    if returncode != 0 and not output.strip():
        return "Unknown"
    pid: str | None = None
    last_exit: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip().rstrip(";").strip()
        if line.startswith('"PID"'):
            # Format: "PID" = 1234;
            _, _, value = line.partition("=")
            pid = value.strip()
        elif line.startswith('"LastExitStatus"'):
            _, _, value = line.partition("=")
            last_exit = value.strip()
    if pid is not None:
        return "Running"
    if last_exit is not None and last_exit != "0":
        return "Failed"
    return "Stopped"
