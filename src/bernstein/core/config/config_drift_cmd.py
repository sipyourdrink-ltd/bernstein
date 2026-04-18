"""CFG-009: Config drift detection comparing current vs default values."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConfigDeviation:
    key: str
    kind: Literal["changed", "added", "removed"]
    default_value: Any = None
    current_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "default_value": self.default_value,
            "current_value": self.current_value,
        }


@dataclass(frozen=True)
class ConfigDiffReport:
    deviations: list[ConfigDeviation] = field(default_factory=list[ConfigDeviation])
    total_keys: int = 0
    changed_count: int = 0
    added_count: int = 0
    removed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "deviations": [d.to_dict() for d in self.deviations],
            "total_keys": self.total_keys,
            "changed_count": self.changed_count,
            "added_count": self.added_count,
            "removed_count": self.removed_count,
        }

    @property
    def has_deviations(self) -> bool:
        return len(self.deviations) > 0


_BUILTIN_DEFAULTS: dict[str, Any] = {
    "cli": "auto",
    "max_agents": 6,
    "model": None,
    "team": "auto",
    "budget": None,
    "evolution_enabled": True,
    "auto_decompose": True,
    "merge_strategy": "pr",
    "auto_merge": True,
    "internal_llm_provider": "openrouter_free",
    "internal_llm_model": "nvidia/nemotron-3-super-120b-a12b",
    "log_level": "INFO",
    "timeout": 1800,
}


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_dict(cast("dict[str, Any]", value), full_key))
        else:
            flat[full_key] = value
    return flat


def diff_against_defaults(current: dict[str, Any], defaults: dict[str, Any] | None = None) -> ConfigDiffReport:
    if defaults is None:
        defaults = _BUILTIN_DEFAULTS
    flat_current = _flatten_dict(current)
    flat_defaults = _flatten_dict(defaults)
    all_keys = sorted(set(flat_current) | set(flat_defaults))
    deviations: list[ConfigDeviation] = []
    changed = added = removed = 0
    for key in all_keys:
        in_current = key in flat_current
        in_defaults = key in flat_defaults
        if in_current and in_defaults:
            if flat_current[key] != flat_defaults[key]:
                deviations.append(
                    ConfigDeviation(
                        key=key, kind="changed", default_value=flat_defaults[key], current_value=flat_current[key]
                    )
                )
                changed += 1
        elif in_current:
            deviations.append(ConfigDeviation(key=key, kind="added", current_value=flat_current[key]))
            added += 1
        else:
            deviations.append(ConfigDeviation(key=key, kind="removed", default_value=flat_defaults[key]))
            removed += 1
    return ConfigDiffReport(
        deviations=deviations, total_keys=len(all_keys), changed_count=changed, added_count=added, removed_count=removed
    )
