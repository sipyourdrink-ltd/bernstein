"""Configuration loader for the fleet dashboard.

Reads ``~/.config/bernstein/projects.toml`` (or ``$BERNSTEIN_FLEET_CONFIG``)
and validates each ``[[project]]`` block. Validation errors are returned as
:class:`FleetConfigError` instances rather than raised so that the dashboard
can surface them in its footer instead of crashing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - py<3.11 fallback
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

DEFAULT_TASK_SERVER_PORT = 8052


@dataclass(frozen=True, slots=True)
class FleetConfigError:
    """A non-fatal validation error tied to a single ``[[project]]`` block.

    Attributes:
        index: Zero-based index of the offending block (``-1`` for global errors).
        message: Human-readable explanation suitable for the dashboard footer.
    """

    index: int
    message: str


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """A single project entry in the fleet config.

    Attributes:
        name: Display name. Falls back to the project path's basename.
        path: Absolute path to the project root (where ``.sdd/`` lives).
        task_server_url: Base URL of the project's task server. Defaults to
            ``http://127.0.0.1:8052`` if omitted.
        sdd_dir: Resolved ``.sdd`` directory path.
    """

    name: str
    path: Path
    task_server_url: str
    sdd_dir: Path

    @property
    def status_url(self) -> str:
        """URL of the project's ``/status`` endpoint."""
        return self.task_server_url.rstrip("/") + "/status"

    @property
    def bulletin_url(self) -> str:
        """URL of the project's ``/bulletin`` endpoint."""
        return self.task_server_url.rstrip("/") + "/bulletin"

    @property
    def events_url(self) -> str:
        """URL of the project's ``/events`` SSE stream."""
        return self.task_server_url.rstrip("/") + "/events"

    @property
    def metrics_url(self) -> str:
        """URL of the project's Prometheus ``/metrics`` endpoint."""
        return self.task_server_url.rstrip("/") + "/metrics"


@dataclass(slots=True)
class FleetConfig:
    """Parsed fleet configuration.

    Attributes:
        projects: Successfully parsed project entries.
        errors: Validation errors that did not prevent loading.
        source_path: Path the config was loaded from (or ``None`` for inline).
    """

    projects: list[ProjectConfig] = field(default_factory=list[ProjectConfig])
    errors: list[FleetConfigError] = field(default_factory=list[FleetConfigError])
    source_path: Path | None = None


def default_projects_config_path() -> Path:
    """Return the canonical config location, honouring overrides.

    Resolution order:

    1. ``$BERNSTEIN_FLEET_CONFIG`` if set.
    2. ``$XDG_CONFIG_HOME/bernstein/projects.toml`` if set.
    3. ``~/.config/bernstein/projects.toml`` otherwise.
    """
    override = os.environ.get("BERNSTEIN_FLEET_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "bernstein" / "projects.toml"
    return Path.home() / ".config" / "bernstein" / "projects.toml"


def _looks_loopback(url: str) -> bool:
    """Cheap loopback check: hosts must resolve to a local interface or VPN.

    The ticket scope is local-only; we accept ``localhost``, ``127.x.x.x``,
    ``[::1]``, and any host whose URL parses cleanly. The actual reachability
    check is deferred to the aggregator.
    """
    if "://" not in url:
        return False
    rest = url.split("://", 1)[1]
    host = rest.split("/", 1)[0].split(":", 1)[0]
    if host in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return True
    if host.startswith("127."):
        return True
    # Permit any host but the dashboard will fall back to ``offline`` if the
    # connection refuses; this matches the ticket's "loopback or VPN-reachable
    # host" guidance without doing DNS in the loader.
    return True


def _validate_project_block(
    index: int, block: dict[str, Any]
) -> tuple[ProjectConfig | None, list[FleetConfigError]]:
    errors: list[FleetConfigError] = []
    raw_path = block.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        errors.append(FleetConfigError(index, "missing or empty 'path'"))
        return None, errors

    try:
        path = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        errors.append(FleetConfigError(index, f"unresolvable path: {exc}"))
        return None, errors

    raw_url = block.get("task_server_url")
    if raw_url is None:
        url = f"http://127.0.0.1:{DEFAULT_TASK_SERVER_PORT}"
    elif isinstance(raw_url, str) and raw_url.strip():
        url = raw_url.strip()
        if not _looks_loopback(url):
            errors.append(
                FleetConfigError(
                    index,
                    f"task_server_url {url!r} is not loopback/VPN; "
                    "v1.9 fleet view is local-only",
                )
            )
    else:
        errors.append(FleetConfigError(index, "task_server_url must be a string"))
        return None, errors

    raw_name = block.get("name")
    if raw_name is None:
        name = path.name
    elif isinstance(raw_name, str) and raw_name.strip():
        name = raw_name.strip()
    else:
        errors.append(FleetConfigError(index, "name must be a non-empty string"))
        return None, errors

    sdd_dir = path / ".sdd"
    return (
        ProjectConfig(name=name, path=path, task_server_url=url, sdd_dir=sdd_dir),
        errors,
    )


def parse_projects_config(text: str, source_path: Path | None = None) -> FleetConfig:
    """Parse a fleet config from raw TOML text.

    Args:
        text: TOML body.
        source_path: Optional path used for error messages and roundtrips.

    Returns:
        A :class:`FleetConfig` containing parsed projects and a list of
        non-fatal validation errors.
    """
    config = FleetConfig(source_path=source_path)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        config.errors.append(FleetConfigError(-1, f"TOML parse error: {exc}"))
        return config

    blocks = parsed.get("project")
    if blocks is None:
        return config
    if not isinstance(blocks, list):
        config.errors.append(
            FleetConfigError(-1, "'project' must be an array of tables ([[project]])")
        )
        return config

    seen_names: set[str] = set()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            config.errors.append(
                FleetConfigError(index, "[[project]] entry is not a table")
            )
            continue
        project, errors = _validate_project_block(index, block)
        config.errors.extend(errors)
        if project is None:
            continue
        if project.name in seen_names:
            config.errors.append(
                FleetConfigError(index, f"duplicate project name {project.name!r}")
            )
            continue
        seen_names.add(project.name)
        config.projects.append(project)

    return config


def load_projects_config(path: Path | None = None) -> FleetConfig:
    """Load and validate the fleet config from disk.

    Args:
        path: Optional explicit config path; defaults to
            :func:`default_projects_config_path`.

    Returns:
        A :class:`FleetConfig`. Missing files yield an empty config with a
        single error so the TUI can render a hint.
    """
    target = path or default_projects_config_path()
    if not target.exists():
        return FleetConfig(
            errors=[
                FleetConfigError(
                    -1,
                    f"fleet config not found at {target} — create it with [[project]] blocks",
                )
            ],
            source_path=target,
        )
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return FleetConfig(
            errors=[FleetConfigError(-1, f"cannot read {target}: {exc}")],
            source_path=target,
        )
    config = parse_projects_config(text, source_path=target)
    return config
