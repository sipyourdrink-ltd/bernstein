"""Tests for bernstein.core.config_adapter_validation (CFG-010)."""

from __future__ import annotations

from bernstein.core.config_adapter_validation import (
    AdapterConfigValidator,
    AdapterFieldSpec,
    AdapterValidationError,
)


class TestAdapterValidationError:
    def test_to_dict(self) -> None:
        err = AdapterValidationError(
            adapter="claude",
            field="max_turns",
            message="Value -1 is below minimum 1",
        )
        d = err.to_dict()
        assert d["adapter"] == "claude"
        assert d["field"] == "max_turns"


class TestAdapterFieldSpec:
    def test_basic_spec(self) -> None:
        spec = AdapterFieldSpec(name="max_turns", expected_type=int, min_value=1, max_value=500)
        assert spec.name == "max_turns"
        assert spec.expected_type is int


class TestAdapterConfigValidator:
    def test_valid_claude_config(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("claude", {"max_turns": 50})
        assert len(errors) == 0

    def test_invalid_type(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("claude", {"max_turns": "fifty"})
        assert len(errors) == 1
        assert "Expected int" in errors[0].message

    def test_below_minimum(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("claude", {"max_turns": 0})
        assert len(errors) == 1
        assert "below minimum" in errors[0].message

    def test_above_maximum(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("claude", {"max_turns": 1000})
        assert len(errors) == 1
        assert "above maximum" in errors[0].message

    def test_invalid_allowed_value(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("claude", {"output_format": "xml"})
        assert len(errors) == 1
        assert errors[0].severity == "warning"

    def test_unknown_adapter_no_errors(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("unknown_adapter", {"max_turns": 50})
        assert len(errors) == 0

    def test_missing_field_no_error(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("claude", {})
        assert len(errors) == 0

    def test_validate_all_with_role_config(self) -> None:
        validator = AdapterConfigValidator()
        config = {
            "cli": "claude",
            "role_config": {
                "backend": {"cli": "claude", "max_turns": 0},
            },
        }
        errors = validator.validate_all(config)
        assert any("below minimum" in e.message for e in errors)

    def test_supported_adapters(self) -> None:
        validator = AdapterConfigValidator()
        adapters = validator.supported_adapters()
        assert "claude" in adapters
        assert "codex" in adapters

    def test_fields_for_adapter(self) -> None:
        validator = AdapterConfigValidator()
        fields = validator.fields_for_adapter("claude")
        names = [f["name"] for f in fields]
        assert "max_turns" in names

    def test_codex_valid_config(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("codex", {"approval_mode": "full-auto"})
        assert len(errors) == 0

    def test_codex_invalid_approval_mode(self) -> None:
        validator = AdapterConfigValidator()
        errors = validator.validate("codex", {"approval_mode": "invalid"})
        assert len(errors) == 1
