"""CFG-012: Config override via CLI flags for all key settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CLIOverride:
    flag: str
    config_key: str
    value: Any
    raw: str


@dataclass(frozen=True, slots=True)
class CLIFlagSpec:
    flag: str
    config_key: str
    value_type: type
    description: str = ""
    short: str = ""


SUPPORTED_FLAGS: tuple[CLIFlagSpec, ...] = (
    CLIFlagSpec("--max-agents", "max_agents", int, "Maximum concurrent agents", "-n"),
    CLIFlagSpec("--budget", "budget", str, "Spending cap", "-b"),
    CLIFlagSpec("--model", "model", str, "Model override"),
    CLIFlagSpec("--cli", "cli", str, "CLI agent backend"),
    CLIFlagSpec("--team", "team", str, "Role team"),
    CLIFlagSpec("--merge-strategy", "merge_strategy", str, "How agent work reaches main"),
    CLIFlagSpec("--timeout", "timeout", int, "Agent timeout in seconds", "-t"),
    CLIFlagSpec("--log-level", "log_level", str, "Log level"),
    CLIFlagSpec("--no-evolution", "evolution_enabled", bool, "Disable self-evolution"),
    CLIFlagSpec("--no-decompose", "auto_decompose", bool, "Disable auto decomposition"),
    CLIFlagSpec("--auto-merge", "auto_merge", bool, "Enable auto-merge"),
    CLIFlagSpec("--max-cost-per-agent", "max_cost_per_agent", float, "Per-agent cost cap"),
    CLIFlagSpec("--internal-llm-provider", "internal_llm_provider", str, "LLM provider for planning"),
    CLIFlagSpec("--internal-llm-model", "internal_llm_model", str, "Model for internal LLM"),
)

_FLAG_TO_SPEC: dict[str, CLIFlagSpec] = {}
for _spec in SUPPORTED_FLAGS:
    _FLAG_TO_SPEC[_spec.flag] = _spec
    if _spec.short:
        _FLAG_TO_SPEC[_spec.short] = _spec


def _coerce_value(spec: CLIFlagSpec, raw: str) -> Any:
    if spec.value_type is bool:
        if spec.flag.startswith("--no-"):
            return raw.lower() not in ("1", "true", "yes") if raw else False
        return raw.lower() in ("1", "true", "yes") if raw else True
    if spec.value_type is int:
        return int(raw)
    if spec.value_type is float:
        return float(raw)
    return raw


def parse_cli_overrides(flags: dict[str, str]) -> list[CLIOverride]:
    overrides: list[CLIOverride] = []
    for flag, raw in flags.items():
        spec = _FLAG_TO_SPEC.get(flag)
        if spec is None:
            raise ValueError(f"Unknown CLI flag: {flag}")
        try:
            value = _coerce_value(spec, raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid value for {flag}: {raw!r} ({exc})") from exc
        overrides.append(CLIOverride(flag=flag, config_key=spec.config_key, value=value, raw=raw))
    return overrides


def apply_overrides(config: dict[str, Any], overrides: list[CLIOverride]) -> dict[str, Any]:
    result = dict(config)
    for override in overrides:
        result[override.config_key] = override.value
    return result


@dataclass
class CLIOverrideManager:
    overrides: list[CLIOverride] = field(default_factory=list[CLIOverride])

    def parse(self, flags: dict[str, str]) -> None:
        self.overrides = parse_cli_overrides(flags)

    def apply(self, config: dict[str, Any]) -> dict[str, Any]:
        return apply_overrides(config, self.overrides)

    def as_dict(self) -> dict[str, Any]:
        return {o.config_key: o.value for o in self.overrides}

    @staticmethod
    def supported_flags() -> list[dict[str, str]]:
        return [
            {
                "flag": s.flag,
                "short": s.short,
                "config_key": s.config_key,
                "type": s.value_type.__name__,
                "description": s.description,
            }
            for s in SUPPORTED_FLAGS
        ]
