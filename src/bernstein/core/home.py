"""Global ~/.bernstein home directory management.

Provides cross-project config storage, catalog cache, and cost tracking.
Config precedence (highest to lowest):
  project .sdd/config.yaml > ~/.bernstein/config.yaml > built-in defaults
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

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


# ---------------------------------------------------------------------------
# Config resolution with precedence
# ---------------------------------------------------------------------------


def resolve_config(
    key: str,
    *,
    home: BernsteinHome,
    project_dir: Path,
) -> dict[str, Any]:
    """Resolve the effective value for *key* across all config layers.

    Precedence (highest first):
    1. ``<project>/.sdd/config.yaml``
    2. ``~/.bernstein/config.yaml``
    3. Built-in defaults

    Args:
        key: Config key to look up.
        home: BernsteinHome instance (global config).
        project_dir: Project root for loading ``.sdd/config.yaml``.

    Returns:
        Dict with ``value`` (effective value) and ``source`` (one of
        ``"project"``, ``"global"``, ``"default"``).
    """
    # 1. Project .sdd/config.yaml
    sdd_config = project_dir / ".sdd" / "config.yaml"
    if sdd_config.exists():
        try:
            data = yaml.safe_load(sdd_config.read_text(encoding="utf-8"))
            if isinstance(data, dict) and key in data:
                return {"value": data[key], "source": "project"}
        except Exception:
            pass

    # 2. Global ~/.bernstein/config.yaml
    global_data = home._load()  # type: ignore[reportPrivateUsage]
    if key in global_data:
        return {"value": global_data[key], "source": "global"}

    # 3. Built-in defaults
    return {"value": _DEFAULTS.get(key), "source": "default"}
