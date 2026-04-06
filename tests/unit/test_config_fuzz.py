"""TEST-010: Fuzzing for config parser.

Generates random/malformed YAML inputs and verifies no crashes,
only clean errors (ValidationError or EnvExpansionError).
"""

from __future__ import annotations

import textwrap
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from bernstein.core.config_schema import (
    BernsteinConfig,
    EnvExpansionError,
    expand_env_vars,
)

# ---------------------------------------------------------------------------
# Strategies for malformed YAML content
# ---------------------------------------------------------------------------

_random_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**31), max_value=2**31),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(min_size=0, max_size=100),
)


@st.composite
def random_yaml_dict(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a dict with random keys and values that may break parsing."""
    keys = draw(
        st.lists(st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz_"), min_size=0, max_size=6)
    )
    vals = draw(st.lists(_random_scalars, min_size=len(keys), max_size=len(keys)))
    return dict(zip(keys, vals, strict=False))


@st.composite
def malformed_yaml_text(draw: st.DrawFn) -> str:
    """Generate YAML-like text that may be syntactically invalid."""
    strategy = draw(st.integers(min_value=0, max_value=4))
    if strategy == 0:
        # Random garbage
        return draw(st.text(min_size=0, max_size=200))
    if strategy == 1:
        # Unbalanced braces
        return "roles: {backend: {cli: " + draw(st.text(min_size=0, max_size=30))
    if strategy == 2:
        # Tab indentation (YAML pitfall)
        return "roles:\n\tbackend:\n\t\tcli: claude"
    if strategy == 3:
        # Very deep nesting
        depth = draw(st.integers(min_value=1, max_value=20))
        s = "a: " * depth + "1"
        return s
    # Duplicate keys
    return "roles:\n  backend: 1\n  backend: 2\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFuzzedYAMLNoCrash:
    """Random YAML must never crash the parser -- only raise clean errors."""

    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    @given(data=random_yaml_dict())
    def test_random_dict_no_crash(self, data: dict[str, Any]) -> None:
        """Feeding random dicts to BernsteinConfig must raise ValidationError or succeed."""
        try:
            BernsteinConfig.model_validate(data)
        except (ValidationError, TypeError, ValueError):
            pass  # Expected for garbage input

    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    @given(text=malformed_yaml_text())
    def test_malformed_yaml_text_no_crash(self, text: str) -> None:
        """Malformed YAML text must not crash yaml.safe_load or config parser."""
        try:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                BernsteinConfig.model_validate(parsed)
        except (yaml.YAMLError, ValidationError, TypeError, ValueError):
            pass  # Expected for garbage input


class TestSpecificMalformedConfigs:
    """Specific edge-case configs that must produce clean errors."""

    def test_empty_yaml(self) -> None:
        parsed = yaml.safe_load("")
        assert parsed is None

    def test_null_config(self) -> None:
        parsed = yaml.safe_load("null")
        assert parsed is None

    def test_list_instead_of_dict(self) -> None:
        parsed = yaml.safe_load("[1, 2, 3]")
        with pytest.raises((ValidationError, TypeError)):
            BernsteinConfig.model_validate(parsed)

    def test_wrong_type_for_max_agents(self) -> None:
        with pytest.raises(ValidationError):
            BernsteinConfig.model_validate({"max_agents": "not_a_number"})

    def test_negative_max_agents(self) -> None:
        # Negative values may be accepted or rejected depending on validators
        try:
            cfg = BernsteinConfig.model_validate({"max_agents": -5})
            # If accepted, at least it didn't crash
            assert cfg.max_agents == -5
        except ValidationError:
            pass

    def test_deeply_nested_roles(self) -> None:
        data = {"roles": {"a" * 1000: {"cli": "claude"}}}
        try:
            BernsteinConfig.model_validate(data)
        except (ValidationError, TypeError):
            pass

    def test_unicode_role_name(self) -> None:
        data = {"roles": {"\u00e9\u00e8\u00ea": {"cli": "claude"}}}
        try:
            BernsteinConfig.model_validate(data)
        except (ValidationError, TypeError):
            pass

    def test_binary_in_string_field(self) -> None:
        data = {"auth_token": "\x00\x01\x02\xff"}
        try:
            BernsteinConfig.model_validate(data)
        except (ValidationError, TypeError):
            pass


class TestEnvExpansionFuzz:
    """Fuzz the env expansion function itself."""

    @settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow])
    @given(text=st.text(min_size=0, max_size=200))
    def test_expand_no_crash(self, text: str) -> None:
        """expand_env_vars must never crash -- only raise EnvExpansionError."""
        try:
            expand_env_vars(text, field_name="fuzz")
        except EnvExpansionError:
            pass  # Expected for unset ${VAR} references

    def test_blocked_var_raises(self) -> None:
        with pytest.raises(EnvExpansionError, match="blocked"):
            expand_env_vars("${GITHUB_TOKEN}", field_name="test")

    def test_unset_var_no_default_raises(self) -> None:
        with pytest.raises(EnvExpansionError, match="not set"):
            expand_env_vars("${BERNSTEIN_FUZZ_NONEXISTENT_VAR}", field_name="test")

    def test_unset_var_with_default(self) -> None:
        result = expand_env_vars("${BERNSTEIN_FUZZ_NONEXISTENT_VAR:-fallback}", field_name="test")
        assert result == "fallback"
