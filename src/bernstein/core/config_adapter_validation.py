"""CFG-010: Config validation for adapter-specific settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdapterValidationError:
    adapter: str
    field: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {"adapter": self.adapter, "field": self.field, "message": self.message, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class AdapterFieldSpec:
    name: str
    expected_type: type
    min_value: int | float | None = None
    max_value: int | float | None = None
    allowed_values: frozenset[Any] | None = None
    description: str = ""


_CLAUDE_FIELDS: tuple[AdapterFieldSpec, ...] = (
    AdapterFieldSpec(
        name="max_turns", expected_type=int, min_value=1, max_value=500, description="Maximum conversation turns."
    ),
    AdapterFieldSpec(
        name="model",
        expected_type=str,
        allowed_values=frozenset(
            {"opus", "sonnet", "haiku", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"}
        ),
        description="Model name or alias.",
    ),
    AdapterFieldSpec(
        name="output_format",
        expected_type=str,
        allowed_values=frozenset({"json", "text", "stream-json"}),
        description="Output format for Claude Code CLI.",
    ),
    AdapterFieldSpec(
        name="permission_mode",
        expected_type=str,
        allowed_values=frozenset({"default", "plan", "bypasstool"}),
        description="Permission approval mode.",
    ),
)
_CODEX_FIELDS: tuple[AdapterFieldSpec, ...] = (
    AdapterFieldSpec(
        name="approval_mode",
        expected_type=str,
        allowed_values=frozenset({"auto-edit", "suggest", "full-auto"}),
        description="Codex approval mode.",
    ),
    AdapterFieldSpec(name="model", expected_type=str, description="OpenAI model name."),
)
_GEMINI_FIELDS: tuple[AdapterFieldSpec, ...] = (
    AdapterFieldSpec(name="model", expected_type=str, description="Gemini model name."),
    AdapterFieldSpec(
        name="sandbox", expected_type=str, allowed_values=frozenset({"docker", "none"}), description="Sandbox mode."
    ),
)
_ADAPTER_SPECS: dict[str, tuple[AdapterFieldSpec, ...]] = {
    "claude": _CLAUDE_FIELDS,
    "codex": _CODEX_FIELDS,
    "gemini": _GEMINI_FIELDS,
}


def _validate_field(adapter: str, spec: AdapterFieldSpec, value: Any) -> list[AdapterValidationError]:
    errors: list[AdapterValidationError] = []
    if not isinstance(value, spec.expected_type):
        errors.append(
            AdapterValidationError(
                adapter=adapter,
                field=spec.name,
                message=f"Expected {spec.expected_type.__name__}, got {type(value).__name__}: {value!r}",
            )
        )
        return errors
    if spec.min_value is not None and isinstance(value, (int, float)) and value < spec.min_value:
        errors.append(
            AdapterValidationError(
                adapter=adapter, field=spec.name, message=f"Value {value} is below minimum {spec.min_value}"
            )
        )
    if spec.max_value is not None and isinstance(value, (int, float)) and value > spec.max_value:
        errors.append(
            AdapterValidationError(
                adapter=adapter, field=spec.name, message=f"Value {value} is above maximum {spec.max_value}"
            )
        )
    if spec.allowed_values is not None and value not in spec.allowed_values:
        errors.append(
            AdapterValidationError(
                adapter=adapter,
                field=spec.name,
                message=f"Value {value!r} not in allowed values: {sorted(spec.allowed_values)}",
                severity="warning",
            )
        )
    return errors


@dataclass
class AdapterConfigValidator:
    specs: dict[str, tuple[AdapterFieldSpec, ...]] = field(default_factory=lambda: dict(_ADAPTER_SPECS))

    def validate(self, adapter: str, config: dict[str, Any]) -> list[AdapterValidationError]:
        adapter_specs = self.specs.get(adapter)
        if adapter_specs is None:
            return []
        errors: list[AdapterValidationError] = []
        for spec in adapter_specs:
            if spec.name in config:
                errors.extend(_validate_field(adapter, spec, config[spec.name]))
        return errors

    def validate_all(self, config: dict[str, Any]) -> list[AdapterValidationError]:
        errors: list[AdapterValidationError] = []
        cli = config.get("cli", "auto")
        if cli != "auto" and cli in self.specs:
            errors.extend(self.validate(cli, config))
        role_config = config.get("role_config", {})
        if isinstance(role_config, dict):
            typed_role_config = cast("dict[str, Any]", role_config)
            for _role, role_settings in typed_role_config.items():
                if isinstance(role_settings, dict):
                    typed_role_settings = cast("dict[str, Any]", role_settings)
                    role_cli: str | None = typed_role_settings.get("cli")
                    if role_cli and role_cli in self.specs:
                        errors.extend(self.validate(role_cli, typed_role_settings))
        return errors

    def supported_adapters(self) -> list[str]:
        return sorted(self.specs.keys())

    def fields_for_adapter(self, adapter: str) -> list[dict[str, Any]]:
        specs = self.specs.get(adapter, ())
        return [
            {
                "name": s.name,
                "type": s.expected_type.__name__,
                "min": s.min_value,
                "max": s.max_value,
                "allowed": sorted(s.allowed_values) if s.allowed_values else None,
                "description": s.description,
            }
            for s in specs
        ]
