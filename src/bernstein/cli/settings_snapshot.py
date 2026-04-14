"""Settings snapshot — capture and serialize effective settings for traces.

Provides ``capture_settings_snapshot()`` that collects all settings
sources (environment, config files, defaults) with provenance info,
and ``save_settings_snapshot()`` that persists to .sdd/traces/.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SettingValue:
    """A single setting with its value and provenance.

    Attributes:
        key: The setting key (e.g. "model", "effort", "timeout").
        value: The effective value.
        source: Where the value came from (env, config, default, cli).
        source_detail: Specific source file or env var name.
    """

    key: str
    value: Any
    source: str
    source_detail: str = ""


@dataclass
class SettingsSnapshot:
    """Complete settings snapshot for a trace.

    Attributes:
        captured_at: When the snapshot was taken.
        settings: Dict of setting key -> SettingValue.
        env_vars: Relevant environment variables.
        config_paths: Config file paths that were checked.
    """

    captured_at: datetime
    settings: dict[str, SettingValue] = field(default_factory=dict[str, SettingValue])
    env_vars: dict[str, str] = field(default_factory=dict[str, str])
    config_paths: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON output."""
        return {
            "captured_at": self.captured_at.isoformat(),
            "settings": {
                k: {
                    "key": v.key,
                    "value": v.value,
                    "source": v.source,
                    "source_detail": v.source_detail,
                }
                for k, v in self.settings.items()
            },
            "env_vars": self.env_vars,
            "config_paths": self.config_paths,
        }

    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value by key.

        Args:
            key: The setting key.
            default: Default value if key not found.

        Returns:
            The setting value, or default.
        """
        sv = self.settings.get(key)
        return sv.value if sv is not None else default


# ---------------------------------------------------------------------------
# Settings collection
# ---------------------------------------------------------------------------

# Environment variables relevant to Bernstein
_RELEVANT_ENV_VARS: list[str] = [
    "BERNSTEIN_SERVER_URL",
    "BERNSTEIN_AUTH_TOKEN",
    "BERNSTEIN_MODEL",
    "BERNSTEIN_EFFORT",
    "BERNSTEIN_LOG_LEVEL",
    "BERNSTEIN_TUI_THEME",
    "BERNSTEIN_TIMEOUT",
    "BERNSTEIN_MAX_TOKENS",
    "BERNSTEIN_RETRY_COUNT",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
]

# Config files to check (in priority order)
_CONFIG_FILES: list[str] = [
    ".bernstein/config.yaml",
    ".bernstein/config.json",
    "bernstein.yaml",
    "~/.bernstein/config.yaml",
]

# Default settings
_DEFAULTS: dict[str, Any] = {
    "model": "auto",
    "effort": "normal",
    "timeout": 300,
    "max_tokens": 4096,
    "retry_count": 3,
    "log_level": "INFO",
    "server_url": "http://localhost:8052",
}


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read a config file and return its contents as a dict.

    Args:
        path: Path to the config file.

    Returns:
        Dict of config values, or empty dict if file doesn't exist.
    """
    if not path.exists():
        return {}

    try:
        content = path.read_text(encoding="utf-8")

        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml

                return yaml.safe_load(content) or {}
            except ImportError:
                return {}
        elif path.suffix == ".json":
            return json.loads(content)
        else:
            return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read config %s: %s", path, exc)
        return {}


_EXTRA_SETTING_KEYS = frozenset(
    {
        "model",
        "effort",
        "timeout",
        "max_tokens",
        "retry_count",
        "log_level",
        "server_url",
    }
)


def _is_known_setting(key: str) -> bool:
    """Check whether a key is a recognized setting name."""
    return key in _DEFAULTS or key in _EXTRA_SETTING_KEYS


def _load_settings_from_configs(
    working_dir: Path,
    settings: dict[str, SettingValue],
) -> list[str]:
    """Load settings from config files and return list of paths checked."""
    config_paths: list[str] = []
    for config_rel in _CONFIG_FILES:
        config_path = Path(config_rel).expanduser()
        if not config_path.is_absolute():
            config_path = working_dir / config_rel
        config_paths.append(str(config_path))
        if not config_path.exists():
            continue
        config_data = _read_config_file(config_path)
        for key, value in config_data.items():
            if _is_known_setting(key):
                settings[key] = SettingValue(
                    key=key,
                    value=value,
                    source="config",
                    source_detail=str(config_path),
                )
    return config_paths


def _load_settings_from_env(settings: dict[str, SettingValue]) -> dict[str, str]:
    """Load settings from environment variables. Returns captured env vars."""
    env_vars: dict[str, str] = {}
    for env_key in _RELEVANT_ENV_VARS:
        env_value = os.environ.get(env_key)
        if env_value is None:
            continue
        env_vars[env_key] = env_value
        setting_key = env_key.replace("BERNSTEIN_", "").lower()
        if _is_known_setting(setting_key):
            settings[setting_key] = SettingValue(
                key=setting_key,
                value=env_value,
                source="env",
                source_detail=env_key,
            )
    return env_vars


def capture_settings_snapshot(
    working_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> SettingsSnapshot:
    """Capture a snapshot of all effective settings with provenance.

    Collects settings from:
    1. Default values
    2. Config files
    3. Environment variables
    4. Extra overrides (e.g. from CLI args)

    Args:
        working_dir: Working directory for config file resolution.
        extra_env: Additional environment variable overrides.

    Returns:
        SettingsSnapshot with all settings and their provenance.
    """
    if working_dir is None:
        working_dir = Path.cwd()

    now = datetime.now(tz=UTC)
    settings: dict[str, SettingValue] = {
        key: SettingValue(key=key, value=value, source="default", source_detail="built-in defaults")
        for key, value in _DEFAULTS.items()
    }

    config_paths = _load_settings_from_configs(working_dir, settings)
    env_vars = _load_settings_from_env(settings)

    if extra_env:
        for key, value in extra_env.items():
            settings[key] = SettingValue(
                key=key,
                value=value,
                source="cli",
                source_detail="command line argument",
            )

    return SettingsSnapshot(
        captured_at=now,
        settings=settings,
        env_vars=env_vars,
        config_paths=config_paths,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_settings_snapshot(
    snapshot: SettingsSnapshot,
    traces_dir: Path | None = None,
    filename: str | None = None,
) -> Path:
    """Save a settings snapshot to .sdd/traces/.

    Args:
        snapshot: The settings snapshot to save.
        traces_dir: Directory to save the snapshot. If None, uses .sdd/traces/.
        filename: Filename for the snapshot. If None, uses timestamp.

    Returns:
        Path to the saved snapshot file.
    """
    if traces_dir is None:
        traces_dir = Path.cwd() / ".sdd" / "traces"

    traces_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        timestamp = snapshot.captured_at.strftime("%Y%m%d_%H%M%S")
        filename = f"settings_{timestamp}.json"

    snapshot_path = traces_dir / filename

    data = snapshot.to_dict()
    snapshot_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return snapshot_path


def format_snapshot(snapshot: SettingsSnapshot) -> str:
    """Format a settings snapshot as a readable string.

    Args:
        snapshot: The settings snapshot to format.

    Returns:
        Formatted string suitable for console output.
    """
    lines: list[str] = []
    lines.append("Settings Snapshot")
    lines.append(f"  Captured: {snapshot.captured_at.isoformat()}")
    lines.append("")

    lines.append("Settings (with provenance):")
    lines.append("-" * 50)
    for key, sv in sorted(snapshot.settings.items()):
        value_str = str(sv.value)
        if len(value_str) > 40:
            value_str = value_str[:37] + "..."
        lines.append(f"  {key:20s} = {value_str:20s}  [{sv.source}]")

    if snapshot.env_vars:
        lines.append("")
        lines.append("Environment Variables:")
        lines.append("-" * 50)
        for key, value in sorted(snapshot.env_vars.items()):
            # Mask sensitive values
            if "KEY" in key or "TOKEN" in key or "SECRET" in key:
                value = value[:8] + "..." if len(value) > 8 else "***"
            lines.append(f"  {key:30s} = {value}")

    return "\n".join(lines)
