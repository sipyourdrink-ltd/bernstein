"""systemd helpers for installing Bernstein as a user/system unit.

All public functions are pure wrappers around ``systemctl`` and the
filesystem, so they are easy to mock in tests.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from bernstein.core.daemon._render import render_template
from bernstein.core.daemon.errors import UnitExistsError, UnitNotFoundError

__all__ = [
    "DEFAULT_UNIT_NAME",
    "default_user_unit_dir",
    "install_systemd_system_unit",
    "install_systemd_user_unit",
    "parse_status_output",
    "render_systemd_system_unit",
    "render_systemd_user_unit",
    "restart",
    "start",
    "status",
    "stop",
    "uninstall",
]

DEFAULT_UNIT_NAME = "bernstein.service"

_Scope = Literal["user", "system"]


def default_user_unit_dir() -> Path:
    """Return the default directory for user-scope systemd units."""
    return Path(os.path.expanduser("~/.config/systemd/user"))


def _env_lines(env: dict[str, str]) -> str:
    """Render extra ``Environment=`` lines for a systemd unit."""
    return "\n".join(f'Environment="{key}={value}"' for key, value in env.items())


def _render(
    template_name: str,
    command: str,
    env: dict[str, str],
    workdir: str | None = None,
    path_env: str | None = None,
) -> str:
    """Render a systemd unit template with the supplied substitutions."""
    mapping = {
        "CMD": command,
        "WORKDIR": workdir if workdir is not None else str(Path.home()),
        "PATH": path_env if path_env is not None else os.environ.get("PATH", ""),
        "ENV_LINES": _env_lines(env),
    }
    return render_template(template_name, mapping)


def render_systemd_user_unit(
    command: str,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
    path_env: str | None = None,
) -> str:
    """Render the systemd user unit content for ``command``.

    Args:
        command: The ``ExecStart`` command line.
        env: Extra environment variables (beyond ``PATH``).
        workdir: Optional working directory; defaults to ``$HOME``.
        path_env: Optional ``PATH`` override; defaults to the current
            process's ``PATH``.

    Returns:
        The rendered unit file content.
    """
    return _render(
        "systemd-user.service.template",
        command=command,
        env=env or {},
        workdir=workdir,
        path_env=path_env,
    )


def render_systemd_system_unit(
    command: str,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
    path_env: str | None = None,
) -> str:
    """Render the systemd system unit content for ``command``.

    Args:
        command: The ``ExecStart`` command line.
        env: Extra environment variables (beyond ``PATH``).
        workdir: Optional working directory; defaults to ``$HOME``.
        path_env: Optional ``PATH`` override; defaults to the current
            process's ``PATH``.

    Returns:
        The rendered unit file content.
    """
    return _render(
        "systemd-system.service.template",
        command=command,
        env=env or {},
        workdir=workdir,
        path_env=path_env,
    )


def _write_unit(
    unit_dir: Path,
    unit_name: str,
    content: str,
    force: bool,
) -> Path:
    """Write ``content`` to ``unit_dir / unit_name``, honoring ``force``."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / unit_name
    if unit_path.exists() and not force:
        raise UnitExistsError(f"Unit already exists: {unit_path} (use --force to overwrite)")
    unit_path.write_text(content, encoding="utf-8")
    return unit_path


def install_systemd_user_unit(
    command: str,
    unit_dir: Path | None = None,
    env: dict[str, str] | None = None,
    unit_name: str = DEFAULT_UNIT_NAME,
    workdir: str | None = None,
    path_env: str | None = None,
    force: bool = False,
) -> Path:
    """Install a user-scope systemd unit that runs ``command``.

    Args:
        command: The ``ExecStart`` command line.
        unit_dir: Directory for the unit file; defaults to
            ``~/.config/systemd/user``.
        env: Extra environment variables to embed in the unit.
        unit_name: File name for the unit (default ``bernstein.service``).
        workdir: Working directory set on the unit.
        path_env: ``PATH`` value baked into the unit.
        force: If ``True``, overwrite an existing unit.

    Returns:
        Path to the written unit file.

    Raises:
        UnitExistsError: When the unit exists and ``force`` is ``False``.
    """
    target_dir = unit_dir if unit_dir is not None else default_user_unit_dir()
    content = render_systemd_user_unit(command, env=env, workdir=workdir, path_env=path_env)
    return _write_unit(target_dir, unit_name, content, force=force)


def install_systemd_system_unit(
    command: str,
    unit_dir: Path | None = None,
    env: dict[str, str] | None = None,
    unit_name: str = DEFAULT_UNIT_NAME,
    workdir: str | None = None,
    path_env: str | None = None,
    force: bool = False,
) -> Path:
    """Install a system-scope systemd unit that runs ``command``.

    Args:
        command: The ``ExecStart`` command line.
        unit_dir: Directory for the unit file; defaults to
            ``/etc/systemd/system``.
        env: Extra environment variables to embed in the unit.
        unit_name: File name for the unit (default ``bernstein.service``).
        workdir: Working directory set on the unit.
        path_env: ``PATH`` value baked into the unit.
        force: If ``True``, overwrite an existing unit.

    Returns:
        Path to the written unit file.

    Raises:
        UnitExistsError: When the unit exists and ``force`` is ``False``.
    """
    target_dir = unit_dir if unit_dir is not None else Path("/etc/systemd/system")
    content = render_systemd_system_unit(command, env=env, workdir=workdir, path_env=path_env)
    return _write_unit(target_dir, unit_name, content, force=force)


def uninstall(
    unit_dir: Path | None = None,
    unit_name: str = DEFAULT_UNIT_NAME,
    scope: _Scope = "user",
) -> bool:
    """Remove a previously installed systemd unit.

    Args:
        unit_dir: Directory holding the unit file.
        unit_name: Unit file name.
        scope: ``"user"`` or ``"system"``; used to pick the default dir.

    Returns:
        ``True`` if the unit was removed, ``False`` if it was not present
        (idempotent — no error is raised).
    """
    if unit_dir is None:
        unit_dir = default_user_unit_dir() if scope == "user" else Path("/etc/systemd/system")
    unit_path = unit_dir / unit_name
    if not unit_path.exists():
        return False
    unit_path.unlink()
    return True


def _systemctl(
    *args: str,
    scope: _Scope = "user",
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``systemctl`` with ``--user`` when ``scope`` is ``"user"``."""
    cmd: list[str] = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    cmd.extend(args)
    run = runner if runner is not None else subprocess.run
    # Typing: ``runner`` is an abstract callable in tests; delegate to it.
    return run(cmd, capture_output=True, text=True, check=False)  # type: ignore[operator,no-any-return]


def start(
    unit_name: str = DEFAULT_UNIT_NAME,
    scope: _Scope = "user",
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Start the given unit via ``systemctl``."""
    return _systemctl("start", unit_name, scope=scope, runner=runner)


def stop(
    unit_name: str = DEFAULT_UNIT_NAME,
    scope: _Scope = "user",
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Stop the given unit via ``systemctl``."""
    return _systemctl("stop", unit_name, scope=scope, runner=runner)


def restart(
    unit_name: str = DEFAULT_UNIT_NAME,
    scope: _Scope = "user",
    runner: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Restart the given unit via ``systemctl``."""
    return _systemctl("restart", unit_name, scope=scope, runner=runner)


def status(
    unit_name: str = DEFAULT_UNIT_NAME,
    scope: _Scope = "user",
    runner: object | None = None,
) -> str:
    """Return a normalized status string.

    Parses ``systemctl status`` output and collapses it into one of
    ``"Running"``, ``"Stopped"``, ``"Failed"``, or ``"Unknown"``.
    """
    completed = _systemctl("status", unit_name, scope=scope, runner=runner)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if not stdout and not stderr:
        # No output usually means the unit is not loaded.
        raise UnitNotFoundError(f"No status output for {unit_name}")
    return parse_status_output(stdout + "\n" + stderr)


def parse_status_output(output: str) -> str:
    """Classify the ``systemctl status`` output.

    Args:
        output: Combined stdout + stderr from ``systemctl status``.

    Returns:
        ``"Running"``, ``"Stopped"``, ``"Failed"``, or ``"Unknown"``.
    """
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Active:"):
            body = line[len("Active:") :].strip()
            if body.startswith("active"):
                return "Running"
            if body.startswith("failed"):
                return "Failed"
            if body.startswith("inactive") or body.startswith("deactivating"):
                return "Stopped"
    return "Unknown"
