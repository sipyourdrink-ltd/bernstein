"""CFG-007: Multi-scope config (project, user, workspace, env).

Layer config from multiple sources with clear precedence:
    1. DEFAULTS  -- built-in defaults (lowest)
    2. USER      -- ~/.bernstein/config.yaml
    3. PROJECT   -- <workdir>/bernstein.yaml
    4. WORKSPACE -- <workdir>/.bernstein/config.yaml
    5. ENV       -- BERNSTEIN_* environment variables (highest)
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

logger = logging.getLogger(__name__)


@enum.unique
class ConfigScope(enum.Enum):
    """Configuration scope layers ordered by precedence (low to high)."""

    DEFAULTS = 0
    USER = 1
    PROJECT = 2
    WORKSPACE = 3
    ENV = 4


@dataclass(frozen=True, slots=True)
class ScopedValue:
    """A config value with its originating scope."""

    value: Any
    scope: ConfigScope
    source_path: str


_DEFAULTS: dict[str, Any] = {
    "cli": "auto",
    "max_agents": 6,
    "model": None,
    "team": "auto",
    "budget": None,
    "evolution_enabled": True,
    "auto_decompose": True,
    "merge_strategy": "pr",
    "auto_merge": True,
    "log_level": "INFO",
    "timeout": 1800,
}

_ENV_MAP: dict[str, str] = {
    "BERNSTEIN_CLI": "cli",
    "BERNSTEIN_MAX_AGENTS": "max_agents",
    "BERNSTEIN_MODEL": "model",
    "BERNSTEIN_BUDGET": "budget",
    "BERNSTEIN_TEAM": "team",
    "BERNSTEIN_MERGE_STRATEGY": "merge_strategy",
    "BERNSTEIN_LOG_LEVEL": "log_level",
    "BERNSTEIN_TIMEOUT": "timeout",
}

_INT_KEYS: frozenset[str] = frozenset({"max_agents", "timeout"})
_BOOL_KEYS: frozenset[str] = frozenset({"evolution_enabled", "auto_decompose", "auto_merge"})


def _coerce_env_value(key: str, raw: str) -> Any:
    if key in _INT_KEYS:
        return int(raw)
    if key in _BOOL_KEYS:
        return raw.lower() in ("1", "true", "yes")
    return raw


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return cast("dict[str, Any]", loaded)
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to load config from %s: %s", path, exc)
        return {}


def _load_env_scope() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for env_var, key in _ENV_MAP.items():
        raw = os.environ.get(env_var)
        if raw is not None:
            try:
                result[key] = _coerce_env_value(key, raw)
            except (ValueError, TypeError) as exc:
                logger.warning("Invalid env var %s=%r: %s", env_var, raw, exc)
    return result


@dataclass
class MultiScopeConfig:
    """Layered config resolution with provenance tracking."""

    workdir: Path
    layers: dict[ConfigScope, dict[str, Any]] = field(default_factory=dict[ConfigScope, dict[str, Any]])
    provenance: dict[str, ScopedValue] = field(default_factory=dict[str, ScopedValue])

    def load(self) -> None:
        user_path = Path.home() / ".bernstein" / "config.yaml"
        project_path = self.workdir / "bernstein.yaml"
        workspace_path = self.workdir / ".bernstein" / "config.yaml"
        self.layers = {
            ConfigScope.DEFAULTS: dict(_DEFAULTS),
            ConfigScope.USER: _load_yaml_file(user_path),
            ConfigScope.PROJECT: _load_yaml_file(project_path),
            ConfigScope.WORKSPACE: _load_yaml_file(workspace_path),
            ConfigScope.ENV: _load_env_scope(),
        }
        self.provenance.clear()
        source_paths = {
            ConfigScope.DEFAULTS: "<defaults>",
            ConfigScope.USER: str(user_path),
            ConfigScope.PROJECT: str(project_path),
            ConfigScope.WORKSPACE: str(workspace_path),
            ConfigScope.ENV: "<env>",
        }
        for scope in ConfigScope:
            layer = self.layers.get(scope, {})
            path = source_paths.get(scope, "<unknown>")
            for key, value in layer.items():
                self.provenance[key] = ScopedValue(value=value, scope=scope, source_path=path)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self.provenance.get(key)
        if entry is not None:
            return entry.value
        return default

    def get_scoped(self, key: str) -> ScopedValue | None:
        return self.provenance.get(key)

    def effective(self) -> dict[str, Any]:
        return {key: sv.value for key, sv in self.provenance.items()}

    def scope_summary(self) -> list[dict[str, Any]]:
        return [
            {"scope": scope.name, "precedence": scope.value, "key_count": len(self.layers.get(scope, {}))}
            for scope in ConfigScope
        ]

    def keys_from_scope(self, scope: ConfigScope) -> list[str]:
        return [key for key, sv in self.provenance.items() if sv.scope == scope]
