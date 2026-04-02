"""Settings snapshot capture and serialization for execution traces.

Captures the effective settings used at run start with provenance tracking
(environment, bernstein.yaml, .sdd/config.yaml, CLI args) so that any trace
can be fully reproduced later.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key setting names
# ---------------------------------------------------------------------------

_SETTING_NAMES = (
    "model",
    "effort",
    "max_tokens",
    "parallelism",
    "approval_mode",
    "routing_strategy",
    "compliance_preset",
    "storage_backend",
    "audit_mode",
    "container_mode",
    "plan_mode",
    "agent_timeout_s",
)


@dataclass
class SettingValue:
    """A single setting with its effective value and provenance."""

    name: str
    value: str | int | float | bool | None
    source: str  # "env", "config", "cli", "default"
    raw_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "source": self.source,
            "raw_value": self.raw_value,
        }


@dataclass
class SettingsSnapshot:
    """Complete settings snapshot for a Bernstein run."""

    capture_ts: float = 0.0
    workdir: str = ""
    settings: list[SettingValue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_ts": self.capture_ts,
            "workdir": self.workdir,
            "settings": [s.to_dict() for s in self.settings],
        }

    def save(self, traces_dir: Path) -> Path:
        """Save this snapshot to .sdd/traces/settings-snapshot.json."""
        traces_dir.mkdir(parents=True, exist_ok=True)
        path = traces_dir / "settings-snapshot.json"
        path.write_text(
            json.dumps(self.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        return path


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _read_yaml_safe(path: Path) -> dict[str, Any]:
    """Read a YAML file, returning empty dict on any failure."""
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_config_setting(key: str, workdir: Path) -> str | int | float | bool | None:
    """Look up a setting in bernstein.yaml or .sdd/config.yaml."""
    # Try .sdd/config.yaml first (most specific)
    sdd_cfg = _read_yaml_safe(workdir / ".sdd" / "config.yaml")
    if key in sdd_cfg:
        return sdd_cfg[key]
    # Fall back to bernstein.yaml
    root_cfg = _read_yaml_safe(workdir / "bernstein.yaml")
    if key in root_cfg:
        return root_cfg[key]
    return None


def _get_env_setting(key: str) -> str | None:
    """Look up a setting in environment variables."""
    return os.environ.get(f"BERNSTEIN_{key.upper()}")


def capture_settings(workdir: Path | None = None) -> SettingsSnapshot:
    """Capture the effective settings for the current Bernstein run.

    For each tracked setting, determines the effective value and where it
    came from (env > config file > inferred default).

    Args:
        workdir: Project root directory. Defaults to cwd.

    Returns:
        SettingsSnapshot with all tracked settings.
    """
    import time

    workdir = workdir or Path.cwd()
    snapshot = SettingsSnapshot(capture_ts=time.time(), workdir=str(workdir))

    for name in _SETTING_NAMES:
        env_val = _get_env_setting(name)
        cfg_val = _get_config_setting(name, workdir)

        if env_val is not None:
            # Try to convert to a typed value
            typed = _coerce_type(env_val)
            snapshot.settings.append(SettingValue(name=name, value=typed, source="env", raw_value=env_val))
        elif cfg_val is not None:
            snapshot.settings.append(SettingValue(name=name, value=cfg_val, source="config"))
        else:
            snapshot.settings.append(
                SettingValue(
                    name=name,
                    value=None,
                    source="default",
                    raw_value="not set",
                )
            )

    return snapshot


def _coerce_type(value: str) -> str | int | float | bool:
    """Coerce a string value to the most appropriate Python type."""
    lower = value.lower()
    if lower in ("true", "yes", "1"):
        return True
    if lower in ("false", "no", "0"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
