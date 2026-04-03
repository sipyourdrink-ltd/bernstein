"""5-layer settings cascade with provenance tracking and validation.

Settings are layered by precedence (lowest to highest):
    USER      — ~/.bernstein/config.yaml (cross-project defaults)
    PROJECT   — bernstein.yaml / bernstein.yml in workdir
    LOCAL     — <workdir>/.bernstein/config.yaml (local overrides)
    CLI       — command-line / caller-provided overrides
    MANAGED   — server-managed / runtime settings (policy-enforced)

Higher-precedence layers win when the same key is present in multiple layers.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provenance metadata
# ---------------------------------------------------------------------------


@enum.unique
class SettingsSource(enum.Enum):
    """Ordered settings source layers (lowest to highest precedence)."""

    USER = 1
    PROJECT = 2
    LOCAL = 3
    CLI = 4
    MANAGED = 5


@dataclass(frozen=True, slots=True)
class ProvenancedValue:
    """A resolved setting value with its provenance metadata."""

    value: Any
    source: SettingsSource
    source_detail: str = ""


# ---------------------------------------------------------------------------
# Validation schema
# ---------------------------------------------------------------------------

# Expected types and optional allowed-values for known settings.
# Each entry maps key -> (python-type, allowed-values-or-none, converter-or-none).
_SCHEMA: dict[str, tuple[type, tuple[Any, ...] | None, Any | None]] = {
    "cli": (str, None, None),
    "model": (str, None, None),
    "effort": (str, ("low", "normal", "high", "max", "auto"), None),
    "max_agents": (int, None, int),
    "budget": (str, None, None),
    "log_level": (str, ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), None),
    "timeout": (int, None, int),
    "max_tokens": (int, None, int),
    "retry_count": (int, None, int),
    "server_url": (str, None, None),
}


class SettingsValidationError(Exception):
    """Raised when raw data from a source fails validation."""


def _validate_layer(source: SettingsSource, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate raw setting data against the built-in schema.

    Unknown keys are passed through (extension-friendly). Known keys are
    type-checked and optionally value-enumerated.

    Args:
        source: The layer being validated (for error context).
        raw: Raw key-value mapping from the source.

    Returns:
        A cleaned dict with values coerced where converters are defined.

    Raises:
        SettingsValidationError: When a known key has an incompatible type or
            an enumerated value that is not allowed.
    """
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        entry = _SCHEMA.get(key)
        if entry is None:
            # Unknown key — pass through verbatim.
            cleaned[key] = value
            continue

        expected_type, allowed, converter = entry

        # Handle None/null values (YAML renders 'null' as Python None).
        if value is None:
            cleaned[key] = None
            continue

        # Convert if possible/needed.
        actual_value: Any = value
        if converter is not None:
            try:
                actual_value = converter(value)
            except (ValueError, TypeError) as exc:
                raise SettingsValidationError(
                    f"{source.name} layer: key '{key}' value {value!r} cannot be "
                    f"converted to {expected_type.__name__}: {exc}",
                ) from exc
        elif not isinstance(value, expected_type):
            raise SettingsValidationError(
                f"{source.name} layer: key '{key}' expected {expected_type.__name__}, "
                f"got {type(value).__name__} ({value!r})",
            )
            actual_value = value  # unreachable, keeps type checker happy

        # Enumerated check.
        if allowed is not None and actual_value not in allowed:
            raise SettingsValidationError(
                f"{source.name} layer: key '{key}' value {actual_value!r} not in allowed values {allowed}",
            )

        cleaned[key] = actual_value
    return cleaned


# ---------------------------------------------------------------------------
# Cascade class
# ---------------------------------------------------------------------------


def _empty_layers() -> dict[SettingsSource, dict[str, Any]]:
    """Typed factory for SettingsCascade.layers."""
    return {}


@dataclass
class SettingsCascade:
    """Multi-layer settings merger with provenance tracking.

    Layers are stored by enum key. Higher-precedence layers win on merge.
    All values are validated at load time via ``load_layer()``.

    Attributes:
        layers: Dict mapping SettingsSource -> validated dict of settings.
        _merged: Cached effective settings (None = needs rebuild).
    """

    layers: dict[SettingsSource, dict[str, Any]] = field(default_factory=_empty_layers)
    _merged: dict[str, Any] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Layer loading
    # ------------------------------------------------------------------

    def load_layer(self, source: SettingsSource, raw: Mapping[str, Any]) -> None:
        """Validate and register a single source layer.

        Args:
            source: Which source layer this data represents.
            raw: Raw settings dict from the source.

        Raises:
            SettingsValidationError: If validation fails.
        """
        validated = _validate_layer(source, dict(raw))
        self.layers[source] = validated
        self._merged = None  # invalidate cache

    def remove_layer(self, source: SettingsSource) -> None:
        """Remove a previously loaded layer.

        Args:
            source: The layer to remove.
        """
        self.layers.pop(source, None)
        self._merged = None

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _rebuild_merged(self) -> dict[str, Any]:
        """Recompute the effective settings dict from loaded layers."""
        merged: dict[str, Any] = {}
        # Iterate from lowest to highest precedence so higher sources win.
        for source in sorted(self.layers, key=lambda s: s.value):
            merged.update(self.layers[source])
        self._merged = merged
        return merged

    def get(self, key: str, *, default: Any = None) -> ProvenancedValue:
        """Return the effective value for *key* with its provenance.

        Higher-precedence layers win. If the key is absent from all layers,
        returns the provided *default* with source set to ``USER`` (meaning:
        "not configured").

        Args:
            key: Setting key to look up.
            default: Value to return when key is absent from all layers.

        Returns:
            A :class:`ProvenancedValue` with the effective value and the
            source that provided it.
        """
        if self._merged is None:
            self._rebuild_merged()
        assert self._merged is not None

        # Walk layers from highest to lowest to find provenance.
        for source in sorted(self.layers, key=lambda s: s.value, reverse=True):
            if key in self.layers[source]:
                return ProvenancedValue(
                    value=self.layers[source][key],
                    source=source,
                    source_detail=f"provided by {source.name.lower()} layer",
                )

        return ProvenancedValue(
            value=default,
            source=SettingsSource.USER,
            source_detail="default (not configured in any layer)",
        )

    def get_effective(self) -> dict[str, Any]:
        """Return the fully merged effective settings dict."""
        if self._merged is None:
            self._rebuild_merged()
        assert self._merged is not None
        return dict(self._merged)

    def get_all_provenance(self, key: str) -> list[ProvenancedValue]:
        """Return all layer values for a key, ordered by precedence (lowest first).

        Useful for debugging which layer provides what.

        Args:
            key: Setting key to trace across all layers.

        Returns:
            List of ProvenancedValue for each layer that defines the key.
        """
        result: list[ProvenancedValue] = []
        for source in sorted(self.layers, key=lambda s: s.value):
            if key in self.layers[source]:
                result.append(
                    ProvenancedValue(
                        value=self.layers[source][key],
                        source=source,
                        source_detail=f"provided by {source.name.lower()} layer",
                    )
                )
        return result

    def summary(self) -> dict[str, dict[str, Any]]:
        """Return a human-readable summary of all effective settings.

        Keys map to dicts with 'value' and 'source' (source enum name).

        Returns:
            Dict suitable for console output or JSON serialisation.
        """
        if self._merged is None:
            self._rebuild_merged()
        assert self._merged is not None

        result: dict[str, dict[str, Any]] = {}
        for key in self._merged:
            pv = self.get(key)
            result[key] = {"value": pv.value, "source": pv.source.name}
        return result

    # ------------------------------------------------------------------
    # File loading helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_yaml_file(path: Path) -> dict[str, Any]:
        """Safely load a YAML file, returning {} on missing/invalid."""
        if not path.exists():
            return {}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cast("dict[str, Any]", raw)
            return {}
        except Exception as exc:
            logger.warning("Failed to read settings file %s: %s", path, exc)
            return {}

    def load_from_workdir(self, workdir: str | Path) -> None:
        """Discover and load all 5 cascade layers from a working directory.

        Files loaded:
        - USER:   ~/.bernstein/config.yaml
        - PROJECT: <workdir>/bernstein.yaml or bernstein.yml
        - LOCAL:   <workdir>/.bernstein/config.yaml
        - CLI:     <workdir>/.sdd/config/cli_overrides.json
        - MANAGED: <workdir>/.sdd/config/managed_settings.json

        Missing files are silently skipped (the layer is simply not loaded).

        Args:
            workdir: Project root directory.
        """
        workdir = Path(workdir)

        # USER
        user_path = Path.home() / ".bernstein" / "config.yaml"
        user_data = self._read_yaml_file(user_path)
        if user_data:
            self.load_layer(SettingsSource.USER, user_data)
            logger.debug("Loaded USER settings from %s", user_path)

        # PROJECT
        project_path = workdir / "bernstein.yaml"
        if not project_path.exists():
            project_path = workdir / "bernstein.yml"
        project_data = self._read_yaml_file(project_path)
        if project_data:
            self.load_layer(SettingsSource.PROJECT, project_data)
            logger.debug("Loaded PROJECT settings from %s", project_path)

        # LOCAL
        local_path = workdir / ".bernstein" / "config.yaml"
        local_data = self._read_yaml_file(local_path)
        if local_data:
            self.load_layer(SettingsSource.LOCAL, local_data)
            logger.debug("Loaded LOCAL settings from %s", local_path)

        # CLI
        cli_path = workdir / ".sdd" / "config" / "cli_overrides.json"
        cli_data = self._read_json_file(cli_path)
        if cli_data:
            self.load_layer(SettingsSource.CLI, cli_data)
            logger.debug("Loaded CLI settings from %s", cli_path)

        # MANAGED
        managed_path = workdir / ".sdd" / "config" / "managed_settings.json"
        managed_data = self._read_json_file(managed_path)
        if managed_data:
            self.load_layer(SettingsSource.MANAGED, managed_data)
            logger.debug("Loaded MANAGED settings from %s", managed_path)

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        """Safely load a JSON file, returning {} on missing/invalid."""
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cast("dict[str, Any]", raw)
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read settings file %s: %s", path, exc)
            return {}

    def load_from_dict(self, source: SettingsSource, data: dict[str, Any]) -> None:
        """Convenience: register a layer from an in-memory dict.

        Args:
            source: Which source layer the dict represents.
            data: Raw settings dict.
        """
        self.load_layer(source, data)
