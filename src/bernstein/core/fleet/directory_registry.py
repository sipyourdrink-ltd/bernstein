"""Filesystem-as-service-registry for the fleet supervisor.

Scans a root directory and treats each subdirectory containing a
``bernstein.yaml`` file as a registered instance. This removes the need
to maintain a central supervisor config when running several Bernstein
instances on a single host: drop an instance directory under
``$BERNSTEIN_FLEET_ROOT`` and it gets picked up on the next reload.

Per-instance directory contract:

* ``<instance>/bernstein.yaml`` (required): minimal YAML manifest with
  optional ``name``, ``path`` (project root), and ``task_server_url``
  keys. When ``path`` is omitted it defaults to ``<instance>``.
* ``<instance>/logs/`` (optional): conventional log directory; created
  lazily by the supervisor when missing.
* ``<instance>/.disabled`` (optional): presence flag file; when present
  the registry skips the instance entirely.

Validation errors are returned alongside successful specs so the
supervisor can surface them without crashing on a single malformed
directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bernstein.core.fleet.config import (
    DEFAULT_TASK_SERVER_PORT,
    FleetConfig,
    FleetConfigError,
    ProjectConfig,
)
from bernstein.core.tasks.models import DispatchTier, TaskType

DEFAULT_FLEET_ROOT = "~/.bernstein/fleet"
MANIFEST_FILENAME = "bernstein.yaml"
DISABLED_FLAG_FILENAME = ".disabled"


@dataclass(frozen=True, slots=True)
class WorkerCapabilitySpec:
    """One worker's explicitly declared routing eligibility; grants no authority."""

    worker_id: str
    task_types: list[TaskType]
    dispatch_tiers: list[DispatchTier]
    capabilities: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    """A discovered fleet instance.

    Attributes:
        name: Instance name. Defaults to the directory basename.
        directory: Absolute path to the instance directory (the one that
            holds ``bernstein.yaml``).
        project_path: Project root the instance points at. Defaults to
            ``directory`` when the manifest omits ``path``.
        task_server_url: Base URL of the task server for this instance.
        disabled: ``True`` when a ``.disabled`` flag file is present.
        workers: Validated routing matrix; empty means dashboard-only.
    """

    name: str
    directory: Path
    project_path: Path
    task_server_url: str
    disabled: bool = False
    workers: list[WorkerCapabilitySpec] = field(default_factory=list[WorkerCapabilitySpec])

    def to_project_config(self) -> ProjectConfig:
        """Project the spec onto the existing :class:`ProjectConfig` shape.

        This lets the directory-registry plug into the existing fleet
        supervisor without duplicating downstream logic.
        """
        return ProjectConfig(
            name=self.name,
            path=self.project_path,
            task_server_url=self.task_server_url,
            sdd_dir=self.project_path / ".sdd",
        )


@dataclass(slots=True)
class RegistryScanResult:
    """Outcome of a single :meth:`DirectoryRegistry.scan` call.

    Attributes:
        instances: Specs for enabled instances (``.disabled`` skipped).
        disabled: Specs that were skipped because of a ``.disabled`` flag.
        errors: Non-fatal validation errors keyed by directory index.
        root: The root the scan was performed against.
    """

    instances: list[InstanceSpec] = field(default_factory=list[InstanceSpec])
    disabled: list[InstanceSpec] = field(default_factory=list[InstanceSpec])
    errors: list[FleetConfigError] = field(default_factory=list[FleetConfigError])
    root: Path | None = None


def default_fleet_root() -> Path:
    """Return the canonical fleet root, honouring ``$BERNSTEIN_FLEET_ROOT``."""
    override = os.environ.get("BERNSTEIN_FLEET_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_FLEET_ROOT).expanduser()


def _load_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read and parse a ``bernstein.yaml`` manifest.

    Returns:
        ``(data, error)``. Exactly one of the two is ``None``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc}"

    try:
        import yaml  # local import - yaml is a heavy dep
    except ImportError:  # pragma: no cover - yaml is a hard dep elsewhere
        return None, "PyYAML is required to load fleet manifests"

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"YAML parse error in {path.name}: {exc}"

    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"{path.name} must be a YAML mapping"
    return data, None


def _parse_workers(raw: Any) -> list[WorkerCapabilitySpec]:
    """Validate the entire matrix before returning any eligible workers."""
    if not isinstance(raw, list):
        raise ValueError("must be a list")
    workers: list[WorkerCapabilitySpec] = []
    seen: set[str] = set()
    required = {"worker_id", "task_types", "dispatch_tiers"}
    for index, row in enumerate(raw):
        if not isinstance(row, dict) or not required <= row.keys() or row.keys() - (required | {"capabilities"}):
            raise ValueError(f"row {index}: missing required or unknown fields")
        worker_id = row["worker_id"]
        if not isinstance(worker_id, str) or not worker_id.strip() or worker_id in seen:
            raise ValueError(f"row {index}: worker_id must be nonempty and unique")
        for key in ("task_types", "dispatch_tiers", "capabilities"):
            values = row.get(key, [])
            if (
                not isinstance(values, list)
                or (key != "capabilities" and not values)
                or any(not isinstance(value, str) or not value.strip() for value in values)
            ):
                raise ValueError(f"row {index}: {key} must contain nonempty strings in a list")
        workers.append(
            WorkerCapabilitySpec(
                worker_id=worker_id,
                task_types=[TaskType(value) for value in row["task_types"]],
                dispatch_tiers=[DispatchTier(value) for value in row["dispatch_tiers"]],
                capabilities=row.get("capabilities", []),
            )
        )
        seen.add(worker_id)
    return workers


def _build_spec(
    index: int,
    directory: Path,
    manifest: dict[str, Any],
    disabled: bool,
) -> tuple[InstanceSpec | None, list[FleetConfigError]]:
    errors: list[FleetConfigError] = []

    raw_name = manifest.get("name")
    if raw_name is None:
        name = directory.name
    elif isinstance(raw_name, str) and raw_name.strip():
        name = raw_name.strip()
    else:
        errors.append(FleetConfigError(index, "name must be a non-empty string"))
        return None, errors

    raw_path = manifest.get("path")
    if raw_path is None:
        project_path = directory
    elif isinstance(raw_path, str) and raw_path.strip():
        try:
            project_path = Path(raw_path).expanduser()
            if not project_path.is_absolute():
                project_path = (directory / project_path).resolve()
            else:
                project_path = project_path.resolve()
        except (OSError, RuntimeError) as exc:
            errors.append(FleetConfigError(index, f"unresolvable path: {exc}"))
            return None, errors
    else:
        errors.append(FleetConfigError(index, "path must be a non-empty string"))
        return None, errors

    raw_url = manifest.get("task_server_url")
    if raw_url is None:
        url = f"http://127.0.0.1:{DEFAULT_TASK_SERVER_PORT}"
    elif isinstance(raw_url, str) and raw_url.strip():
        url = raw_url.strip()
    else:
        errors.append(FleetConfigError(index, "task_server_url must be a string"))
        return None, errors

    try:
        workers = _parse_workers(manifest.get("workers", []))
    except ValueError as exc:
        errors.append(FleetConfigError(index, f"workers: {exc}"))
        workers = []

    spec = InstanceSpec(
        name=name,
        directory=directory,
        project_path=project_path,
        task_server_url=url,
        disabled=disabled,
        workers=workers,
    )
    return spec, errors


class DirectoryRegistry:
    """Filesystem-driven fleet registry.

    Each call to :meth:`scan` re-reads the configured root and returns a
    fresh :class:`RegistryScanResult`. The registry holds no cached
    state between scans, which keeps ``bernstein fleet reload``
    semantically equivalent to "scan again".

    Args:
        root: Override the fleet root. Defaults to :func:`default_fleet_root`.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else default_fleet_root()

    @property
    def root(self) -> Path:
        """Return the configured fleet root."""
        return self._root

    def scan(self) -> RegistryScanResult:
        """Walk the fleet root once and return enabled + disabled specs."""
        result = RegistryScanResult(root=self._root)
        if not self._root.exists():
            result.errors.append(
                FleetConfigError(
                    -1,
                    f"fleet root {self._root} does not exist; create it or set $BERNSTEIN_FLEET_ROOT",
                )
            )
            return result
        if not self._root.is_dir():
            result.errors.append(FleetConfigError(-1, f"fleet root {self._root} is not a directory"))
            return result

        seen_names: set[str] = set()
        try:
            children = sorted(self._root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            result.errors.append(FleetConfigError(-1, f"cannot list {self._root}: {exc}"))
            return result

        index = -1
        for child in children:
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            index += 1
            manifest_path = child / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            data, err = _load_manifest(manifest_path)
            if err is not None or data is None:
                result.errors.append(FleetConfigError(index, err or "unknown manifest error"))
                continue
            disabled = (child / DISABLED_FLAG_FILENAME).exists()
            spec, errors = _build_spec(index, child, data, disabled)
            result.errors.extend(errors)
            if spec is None:
                continue
            if spec.name in seen_names:
                result.errors.append(FleetConfigError(index, f"duplicate instance name {spec.name!r}"))
                continue
            seen_names.add(spec.name)
            if spec.disabled:
                result.disabled.append(spec)
            else:
                result.instances.append(spec)

        return result

    def as_fleet_config(self) -> FleetConfig:
        """Project the latest scan onto the existing :class:`FleetConfig`.

        Errors and disabled instances surface via the returned config's
        ``errors`` list (disabled entries get an informational error so
        operators can see them in the fleet footer).
        """
        scan = self.scan()
        config = FleetConfig(
            projects=[s.to_project_config() for s in scan.instances],
            errors=scan.errors.copy(),
            source_path=self._root,
        )
        for index, spec in enumerate(scan.disabled):
            config.errors.append(
                FleetConfigError(
                    index,
                    f"instance {spec.name!r} skipped via {DISABLED_FLAG_FILENAME}",
                )
            )
        return config


def load_directory_registry(root: Path | None = None) -> RegistryScanResult:
    """One-shot convenience wrapper around :meth:`DirectoryRegistry.scan`."""
    return DirectoryRegistry(root).scan()
