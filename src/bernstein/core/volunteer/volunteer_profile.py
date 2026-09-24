"""Donor policy profile for autopilot mode (``.bernstein/volunteer_profile.json``).

The profile file is optional: flags alone must fully configure a run (per the
issue's "Proposed approach" — the onboarding flow depends on this). The file
only persists preferences between runs to avoid re-typing the same arguments.

When both a profile file and explicit CLI flags are present, **flags always
override the file**. The file only fills in what flags did not set. This
precedence rule is pinned by test coverage and is the "profile persists
preferences" interpretation of the issue's own text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Schema versions this loader accepts.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: Valid task size caps.
TASK_SIZE_VALUES = ("xs", "s", "m", "l", "xl")

#: Valid egress policies.
EGRESS_POLICY_VALUES = ("package-registries-only", "manifest-allowlist", "unrestricted")


class VolunteerProfileError(ValueError):
    """A profile could not be loaded.

    Carries the offending field so a caller can point at it without parsing
    the message. ``field`` is ``"<document>"`` when the failure is the
    document as a whole (not JSON, not an object).
    """

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


@dataclass(frozen=True, slots=True)
class VolunteerProfile:
    """A donor's declared policy for autopilot mode.

    Attributes:
        version: Schema version; one of :data:`SUPPORTED_SCHEMA_VERSIONS`.
        allowed_projects: Repository names (``owner/repo``) the donor consents
            to work on. Empty means no autopilot work (must be explicit).
        allowed_licenses: SPDX license identifiers the donor accepts.
        allowed_task_types: Task type labels the donor accepts (e.g. ``bug``,
            ``feature``). Empty means all types accepted.
        max_size: Maximum task size (``xs``, ``s``, ``m``, ``l``, ``xl``).
        adapters: CLI adapters the donor permits (e.g. ``claude-code``).
        models: Model identifiers the donor permits.
        max_cpu_percent: CPU ceiling as a percentage (1-100).
        max_memory_mb: Memory ceiling in megabytes.
        max_gpu_percent: GPU ceiling as a percentage (0-100).
        wall_clock_budget_minutes: Total wall clock budget for this run.
        token_budget: Total token budget for this run.
        egress_policy: Network egress policy (``package-registries-only``,
            ``manifest-allowlist``, ``unrestricted``).
    """

    version: int
    allowed_projects: tuple[str, ...]
    allowed_licenses: tuple[str, ...]
    allowed_task_types: tuple[str, ...]
    max_size: str
    adapters: tuple[str, ...]
    models: tuple[str, ...]
    max_cpu_percent: int
    max_memory_mb: int
    max_gpu_percent: int
    wall_clock_budget_minutes: int
    token_budget: int
    egress_policy: str

    @classmethod
    def from_flags(
        cls,
        *,
        allowed_projects: Sequence[str] | None = None,
        allowed_licenses: Sequence[str] | None = None,
        allowed_task_types: Sequence[str] | None = None,
        max_size: str | None = None,
        adapters: Sequence[str] | None = None,
        models: Sequence[str] | None = None,
        max_cpu_percent: int | None = None,
        max_memory_mb: int | None = None,
        max_gpu_percent: int | None = None,
        wall_clock_budget_minutes: int | None = None,
        token_budget: int | None = None,
        egress_policy: str | None = None,
    ) -> VolunteerProfile:
        """Construct a profile from CLI flags alone, with sensible defaults."""
        return cls(
            version=1,
            allowed_projects=tuple(allowed_projects) if allowed_projects else (),
            allowed_licenses=tuple(allowed_licenses) if allowed_licenses else (),
            allowed_task_types=tuple(allowed_task_types) if allowed_task_types else (),
            max_size=max_size or "m",
            adapters=tuple(adapters) if adapters else (),
            models=tuple(models) if models else (),
            max_cpu_percent=max_cpu_percent or 80,
            max_memory_mb=max_memory_mb or 8192,
            max_gpu_percent=max_gpu_percent or 0,
            wall_clock_budget_minutes=wall_clock_budget_minutes or 120,
            token_budget=token_budget or 100000,
            egress_policy=egress_policy or "package-registries-only",
        )

    @classmethod
    def from_flags_and_file(
        cls,
        *,
        file_content: str,
        allowed_projects: Sequence[str] | None = None,
        allowed_licenses: Sequence[str] | None = None,
        allowed_task_types: Sequence[str] | None = None,
        max_size: str | None = None,
        adapters: Sequence[str] | None = None,
        models: Sequence[str] | None = None,
        max_cpu_percent: int | None = None,
        max_memory_mb: int | None = None,
        max_gpu_percent: int | None = None,
        wall_clock_budget_minutes: int | None = None,
        token_budget: int | None = None,
        egress_policy: str | None = None,
    ) -> VolunteerProfile:
        """Merge flags and file, with flags taking precedence."""
        file_profile = load_profile(file_content)
        return cls(
            version=file_profile.version,
            allowed_projects=tuple(allowed_projects) if allowed_projects is not None else file_profile.allowed_projects,
            allowed_licenses=tuple(allowed_licenses) if allowed_licenses is not None else file_profile.allowed_licenses,
            allowed_task_types=(
                tuple(allowed_task_types) if allowed_task_types is not None else file_profile.allowed_task_types
            ),
            max_size=max_size if max_size is not None else file_profile.max_size,
            adapters=tuple(adapters) if adapters is not None else file_profile.adapters,
            models=tuple(models) if models is not None else file_profile.models,
            max_cpu_percent=max_cpu_percent if max_cpu_percent is not None else file_profile.max_cpu_percent,
            max_memory_mb=max_memory_mb if max_memory_mb is not None else file_profile.max_memory_mb,
            max_gpu_percent=max_gpu_percent if max_gpu_percent is not None else file_profile.max_gpu_percent,
            wall_clock_budget_minutes=(
                wall_clock_budget_minutes
                if wall_clock_budget_minutes is not None
                else file_profile.wall_clock_budget_minutes
            ),
            token_budget=token_budget if token_budget is not None else file_profile.token_budget,
            egress_policy=egress_policy if egress_policy is not None else file_profile.egress_policy,
        )


def load_profile(source: str | bytes) -> VolunteerProfile:
    """Parse and validate a profile document.

    Parsing is all-or-nothing: either a fully-validated profile comes back or
    :class:`VolunteerProfileError` is raised naming the field at fault. No
    partially-populated object is ever produced.

    Raises:
        VolunteerProfileError: The document is not a JSON object or any field
            fails validation.
    """
    text = source.decode("utf-8") if isinstance(source, bytes) else source
    try:
        raw: dict[str, Any] = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolunteerProfileError("<document>", f"not valid JSON: {exc}") from exc

    return VolunteerProfile(
        version=_load_version(raw),
        allowed_projects=_load_allowed_projects(raw),
        allowed_licenses=_load_allowed_licenses(raw),
        allowed_task_types=_load_allowed_task_types(raw),
        max_size=_load_max_size(raw),
        adapters=_load_adapters(raw),
        models=_load_models(raw),
        max_cpu_percent=_load_max_cpu_percent(raw),
        max_memory_mb=_load_max_memory_mb(raw),
        max_gpu_percent=_load_max_gpu_percent(raw),
        wall_clock_budget_minutes=_load_wall_clock_budget_minutes(raw),
        token_budget=_load_token_budget(raw),
        egress_policy=_load_egress_policy(raw),
    )


# ---------------------------------------------------------------------------
# Field loaders
# ---------------------------------------------------------------------------


def _load_version(raw: dict[str, Any]) -> int:
    if "version" not in raw:
        raise VolunteerProfileError("version", "required")
    value = raw["version"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerProfileError("version", f"expected an integer, got {type(value).__name__}")
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise VolunteerProfileError("version", f"unsupported schema version {value}; this build accepts {supported}")
    return value


def _load_allowed_projects(raw: dict[str, Any]) -> tuple[str, ...]:
    if "allowed_projects" not in raw:
        raise VolunteerProfileError("allowed_projects", "required")
    value = raw["allowed_projects"]
    if not isinstance(value, list):
        raise VolunteerProfileError("allowed_projects", f"expected a list, got {type(value).__name__}")
    value = cast(list[Any], value)
    if not value:
        raise VolunteerProfileError("allowed_projects", "empty allowlist means no autopilot work; be explicit")
    projects: list[str] = []
    for index, entry in enumerate(value):
        field = f"allowed_projects[{index}]"
        if not isinstance(entry, str):
            raise VolunteerProfileError(field, f"expected a string, got {type(entry).__name__}")
        if not entry:
            raise VolunteerProfileError(field, "empty project name")
        if "/" not in entry:
            raise VolunteerProfileError(field, f"{entry!r} must be owner/repo format")
        projects.append(entry)
    return tuple(projects)


def _load_allowed_licenses(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("allowed_licenses", [])
    if not isinstance(value, list):
        raise VolunteerProfileError("allowed_licenses", f"expected a list, got {type(value).__name__}")
    value = cast(list[Any], value)
    licenses: list[str] = []
    for index, entry in enumerate(value):
        field = f"allowed_licenses[{index}]"
        if not isinstance(entry, str):
            raise VolunteerProfileError(field, f"expected a string, got {type(entry).__name__}")
        if not entry:
            raise VolunteerProfileError(field, "empty license")
        licenses.append(entry)
    return tuple(licenses)


def _load_allowed_task_types(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("allowed_task_types", [])
    if not isinstance(value, list):
        raise VolunteerProfileError("allowed_task_types", f"expected a list, got {type(value).__name__}")
    value = cast(list[Any], value)
    types: list[str] = []
    for index, entry in enumerate(value):
        field = f"allowed_task_types[{index}]"
        if not isinstance(entry, str):
            raise VolunteerProfileError(field, f"expected a string, got {type(entry).__name__}")
        if not entry:
            raise VolunteerProfileError(field, "empty task type")
        types.append(entry)
    return tuple(types)


def _load_max_size(raw: dict[str, Any]) -> str:
    value = raw.get("max_size", "m")
    if not isinstance(value, str):
        raise VolunteerProfileError("max_size", f"expected a string, got {type(value).__name__}")
    if value not in TASK_SIZE_VALUES:
        raise VolunteerProfileError("max_size", f"{value!r} is not one of: {', '.join(TASK_SIZE_VALUES)}")
    return value


def _load_adapters(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("adapters", [])
    if not isinstance(value, list):
        raise VolunteerProfileError("adapters", f"expected a list, got {type(value).__name__}")
    value = cast(list[Any], value)
    adapters: list[str] = []
    for index, entry in enumerate(value):
        field = f"adapters[{index}]"
        if not isinstance(entry, str):
            raise VolunteerProfileError(field, f"expected a string, got {type(entry).__name__}")
        if not entry:
            raise VolunteerProfileError(field, "empty adapter")
        adapters.append(entry)
    return tuple(adapters)


def _load_models(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("models", [])
    if not isinstance(value, list):
        raise VolunteerProfileError("models", f"expected a list, got {type(value).__name__}")
    value = cast(list[Any], value)
    models: list[str] = []
    for index, entry in enumerate(value):
        field = f"models[{index}]"
        if not isinstance(entry, str):
            raise VolunteerProfileError(field, f"expected a string, got {type(entry).__name__}")
        if not entry:
            raise VolunteerProfileError(field, "empty model")
        models.append(entry)
    return tuple(models)


def _load_max_cpu_percent(raw: dict[str, Any]) -> int:
    value = raw.get("max_cpu_percent", 80)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerProfileError("max_cpu_percent", f"expected an integer, got {type(value).__name__}")
    if value < 1:
        raise VolunteerProfileError("max_cpu_percent", f"must be at least 1, got {value}")
    if value > 100:
        raise VolunteerProfileError("max_cpu_percent", f"must be at most 100, got {value}")
    return value


def _load_max_memory_mb(raw: dict[str, Any]) -> int:
    value = raw.get("max_memory_mb", 8192)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerProfileError("max_memory_mb", f"expected an integer, got {type(value).__name__}")
    if value < 256:
        raise VolunteerProfileError("max_memory_mb", f"must be at least 256, got {value}")
    return value


def _load_max_gpu_percent(raw: dict[str, Any]) -> int:
    value = raw.get("max_gpu_percent", 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerProfileError("max_gpu_percent", f"expected an integer, got {type(value).__name__}")
    if value < 0:
        raise VolunteerProfileError("max_gpu_percent", f"must be at least 0, got {value}")
    if value > 100:
        raise VolunteerProfileError("max_gpu_percent", f"must be at most 100, got {value}")
    return value


def _load_wall_clock_budget_minutes(raw: dict[str, Any]) -> int:
    value = raw.get("wall_clock_budget_minutes", 120)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerProfileError("wall_clock_budget_minutes", f"expected an integer, got {type(value).__name__}")
    if value < 1:
        raise VolunteerProfileError("wall_clock_budget_minutes", f"must be at least 1, got {value}")
    return value


def _load_token_budget(raw: dict[str, Any]) -> int:
    value = raw.get("token_budget", 100000)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerProfileError("token_budget", f"expected an integer, got {type(value).__name__}")
    if value < 1:
        raise VolunteerProfileError("token_budget", f"must be at least 1, got {value}")
    return value


def _load_egress_policy(raw: dict[str, Any]) -> str:
    value = raw.get("egress_policy", "package-registries-only")
    if not isinstance(value, str):
        raise VolunteerProfileError("egress_policy", f"expected a string, got {type(value).__name__}")
    if value not in EGRESS_POLICY_VALUES:
        raise VolunteerProfileError("egress_policy", f"{value!r} is not one of: {', '.join(EGRESS_POLICY_VALUES)}")
    return value
