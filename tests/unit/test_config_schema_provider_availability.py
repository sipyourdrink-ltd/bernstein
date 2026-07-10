"""Config-schema validation for the provider_availability section (issue #2355).

A fallback element below the role's conformance floor must be rejected at
config validation time, not at first dispatch.
"""

from __future__ import annotations

from typing import Any

import pytest
from bernstein.core.config_schema import BernsteinConfig
from pydantic import ValidationError


def _config(provider_availability: dict[str, Any]) -> dict[str, Any]:
    return {"goal": "Test goal", "provider_availability": provider_availability}


def test_valid_provider_availability_section_round_trips() -> None:
    cfg = BernsteinConfig(
        **_config(
            {
                "probe_ttl_minutes": 10,
                "probes_enabled": True,
                "roles": {
                    "developer": {
                        "conformance_floor": "advanced",
                        "chain": [
                            {"adapter": "claude", "model": "opus", "conformance": "expert"},
                            {"adapter": "codex", "model": "gpt-5.2", "conformance": "advanced"},
                        ],
                    },
                },
            }
        )
    )
    assert cfg.provider_availability is not None
    assert cfg.provider_availability.probe_ttl_minutes == 10
    role = cfg.provider_availability.roles["developer"]
    assert role.conformance_floor == "advanced"
    assert role.chain[1].adapter == "codex"


def test_fallback_below_conformance_floor_is_rejected() -> None:
    """AC: a fallback below the role's conformance floor fails config validation."""
    with pytest.raises(ValidationError, match="conformance"):
        BernsteinConfig(
            **_config(
                {
                    "roles": {
                        "developer": {
                            "conformance_floor": "advanced",
                            "chain": [
                                {"adapter": "claude", "model": "opus", "conformance": "expert"},
                                {"adapter": "qwen", "model": "qwen3-coder", "conformance": "basic"},
                            ],
                        },
                    },
                }
            )
        )


def test_empty_chain_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BernsteinConfig(**_config({"roles": {"developer": {"conformance_floor": "basic", "chain": []}}}))


def test_unknown_conformance_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BernsteinConfig(
            **_config(
                {
                    "roles": {
                        "developer": {
                            "conformance_floor": "basic",
                            "chain": [{"adapter": "claude", "model": "opus", "conformance": "galactic"}],
                        },
                    },
                }
            )
        )


def test_probe_ttl_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        BernsteinConfig(
            **_config(
                {
                    "probe_ttl_minutes": 0,
                    "roles": {
                        "developer": {
                            "conformance_floor": "basic",
                            "chain": [{"adapter": "claude", "model": "opus", "conformance": "basic"}],
                        },
                    },
                }
            )
        )
