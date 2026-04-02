"""Global ~/.bernstein home directory management.

Provides cross-project config storage, catalog cache, and cost tracking.
Config precedence (highest to lowest):
  session overrides > project .sdd/config.yaml > ~/.bernstein/config.yaml > built-in defaults
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

# Built-in defaults for known keys.
_DEFAULTS: dict[str, Any] = {
    "cli": "claude",
    "budget": None,
    "max_agents": 6,
    "effort": "max",
    "model": None,
}

_DEFAULT_CONFIG_YAML = """\
# Bernstein global config (~/.bernstein/config.yaml)
# Values here apply to all projects unless overridden by project config.

# Default CLI adapter: claude | codex | gemini | qwen
cli: claude

# Default spending cap in USD (null = no limit)
budget: null

# Default max concurrent agents
max_agents: 6

# Default effort level: max | medium | low
effort: max

# Default model override (null = adapter default)
model: null
"""

ConfigSource = Literal["session", "project", "global", "default"]


class ConfigProvenanceLayer(TypedDict):
    """Single configuration layer in a resolved precedence chain."""

    source: ConfigSource
    value: object
    redacted_value: object
    path: str | None


class ConfigResolution(TypedDict):
    """Resolved config value with provenance metadata."""

    value: object
    source: ConfigSource
    source_chain: list[ConfigProvenanceLayer]


class BernsteinHome:
    """Manages the global ~/.bernstein home directory.

    Attributes:
        path: Path to the ~/.bernstein directory.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def default(cls) -> BernsteinHome:
        """Return a BernsteinHome pointing at ~/.bernstein."""
        return cls(Path.home() / ".bernstein")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure(self) -> None:
        """Create directory structure and default config if not present."""
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "agents").mkdir(exist_ok=True)
        (self.path / "metrics").mkdir(exist_ok=True)
        (self.path / "mcp").mkdir(exist_ok=True)

        config_path = self.path / "config.yaml"
        if not config_path.exists():
            config_path.write_text(_DEFAULT_CONFIG_YAML)

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        """Load the global config.yaml, returning an empty dict if missing."""
        config_path = self.path / "config.yaml"
        if not config_path.exists():
            return {}
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}  # type: ignore[reportUnknownVariableType]
        except Exception:
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        """Persist data to config.yaml, creating home dir if needed."""
        self.ensure()
        config_path = self.path / "config.yaml"
        config_path.write_text(yaml.dump(data, default_flow_style=False))

    def get(self, key: str) -> Any:
        """Return the global value for *key*, or None if not set.

        Args:
            key: Config key name.

        Returns:
            Value from global config, or None if absent.
        """
        data = self._load()
        if key in data:
            return data[key]
        return _DEFAULTS.get(key)

    def set(self, key: str, value: Any) -> None:
        """Persist *key=value* in the global config.

        Creates the home directory if it does not yet exist.

        Args:
            key: Config key name.
            value: Value to store (must be YAML-serialisable).
        """
        data = self._load()
        data[key] = value
        self._save(data)

    def all(self) -> dict[str, Any]:
        """Return the full global config dict (merged with defaults).

        Returns:
            Dict containing all known config keys and their effective values.
        """
        data = self._load()
        merged = dict(_DEFAULTS)
        merged.update(data)
        return merged


_ENV_OVERRIDE_MAP: dict[str, str] = {
    "cli": "BERNSTEIN_CLI",
    "budget": "BERNSTEIN_BUDGET",
    "max_agents": "BERNSTEIN_MAX_AGENTS",
    "effort": "BERNSTEIN_EFFORT",
    "model": "BERNSTEIN_MODEL",
}


def _redact_config_value(key: str, value: object) -> object:
    """Return a redacted display value for sensitive configuration fields."""
    lowered = key.lower()
    if any(token in lowered for token in ("secret", "token", "password", "key")) and value is not None:
        return "***REDACTED***"
    return value


def _coerce_config_value(key: str, raw: object) -> object:
    """Coerce raw config values based on built-in defaults."""
    default = _DEFAULTS.get(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"null", "none"}:
            return None
        if isinstance(default, int):
            try:
                return int(raw)
            except ValueError:
                return raw
        if isinstance(default, float):
            try:
                return float(raw)
            except ValueError:
                return raw
    return raw


def _session_overrides_from_env() -> dict[str, object]:
    """Build session-only overrides from Bernstein environment variables."""
    overrides: dict[str, object] = {}
    for key, env_name in _ENV_OVERRIDE_MAP.items():
        value = os.environ.get(env_name)
        if value is not None:
            overrides[key] = _coerce_config_value(key, value)
    return overrides


def _load_project_config(project_dir: Path) -> dict[str, object]:
    """Load ``.sdd/config.yaml`` for a project when present."""
    sdd_config = project_dir / ".sdd" / "config.yaml"
    if not sdd_config.exists():
        return {}
    try:
        data = yaml.safe_load(sdd_config.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Config resolution with precedence
# ---------------------------------------------------------------------------


def resolve_config(
    key: str,
    *,
    home: BernsteinHome,
    project_dir: Path,
    session_overrides: Mapping[str, object] | None = None,
) -> ConfigResolution:
    """Resolve the effective value for *key* across all config layers.

    Precedence (highest first):
    1. Session-only overrides (environment or caller-provided)
    2. ``<project>/.sdd/config.yaml``
    3. ``~/.bernstein/config.yaml``
    4. Built-in defaults

    Args:
        key: Config key to look up.
        home: BernsteinHome instance (global config).
        project_dir: Project root for loading ``.sdd/config.yaml``.
        session_overrides: Optional session-only overrides.

    Returns:
        Typed mapping with the effective ``value``, winning ``source``, and the
        full ``source_chain`` in descending-precedence order.
    """
    project_config = _load_project_config(project_dir)
    global_data = home._load()
    combined_session_overrides = {**_session_overrides_from_env(), **dict(session_overrides or {})}

    layers: list[ConfigProvenanceLayer] = []
    if key in combined_session_overrides:
        value = _coerce_config_value(key, combined_session_overrides[key])
        layers.append(
            {
                "source": "session",
                "value": value,
                "redacted_value": _redact_config_value(key, value),
                "path": None,
            }
        )
    if key in project_config:
        value = project_config[key]
        layers.append(
            {
                "source": "project",
                "value": value,
                "redacted_value": _redact_config_value(key, value),
                "path": str(project_dir / ".sdd" / "config.yaml"),
            }
        )
    if key in global_data:
        value = global_data[key]
        layers.append(
            {
                "source": "global",
                "value": value,
                "redacted_value": _redact_config_value(key, value),
                "path": str(home.path / "config.yaml"),
            }
        )

    default_value = _DEFAULTS.get(key)
    layers.append(
        {
            "source": "default",
            "value": default_value,
            "redacted_value": _redact_config_value(key, default_value),
            "path": None,
        }
    )

    winning = layers[0]
    return {
        "value": winning["value"],
        "source": winning["source"],
        "source_chain": layers,
    }


def resolve_config_bundle(
    *,
    home: BernsteinHome,
    project_dir: Path,
    keys: tuple[str, ...] | None = None,
    session_overrides: Mapping[str, object] | None = None,
) -> dict[str, ConfigResolution]:
    """Resolve a stable bundle of config keys with provenance."""
    target_keys = keys or tuple(sorted(_DEFAULTS))
    return {
        key: resolve_config(
            key,
            home=home,
            project_dir=project_dir,
            session_overrides=session_overrides,
        )
        for key in target_keys
    }
