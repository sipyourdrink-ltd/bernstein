"""Tests for SensitiveData type-level PII guards (sensitive_data.py)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from bernstein.core.sensitive_data import (
    SensitiveData,
    is_sensitive,
    strip_sensitive_fields,
)

# ---------------------------------------------------------------------------
# SensitiveData — basic wrapper behaviour
# ---------------------------------------------------------------------------


class TestSensitiveDataWrapper:
    def test_expose_returns_value(self) -> None:
        sd: SensitiveData[str] = SensitiveData("secret")
        assert sd.expose() == "secret"

    def test_str_is_redacted(self) -> None:
        sd: SensitiveData[str] = SensitiveData("secret")
        assert str(sd) == "<redacted>"

    def test_repr_does_not_contain_value(self) -> None:
        sd: SensitiveData[str] = SensitiveData("secret")
        assert "secret" not in repr(sd)

    def test_works_with_non_string_types(self) -> None:
        sd: SensitiveData[int] = SensitiveData(12345)
        assert sd.expose() == 12345
        assert str(sd) == "<redacted>"

    def test_equality_by_inner_value(self) -> None:
        a: SensitiveData[str] = SensitiveData("same")
        b: SensitiveData[str] = SensitiveData("same")
        assert a == b

    def test_inequality_by_inner_value(self) -> None:
        a: SensitiveData[str] = SensitiveData("x")
        b: SensitiveData[str] = SensitiveData("y")
        assert a != b

    def test_not_equal_to_raw_value(self) -> None:
        sd: SensitiveData[str] = SensitiveData("secret")
        assert sd != "secret"

    def test_hashable(self) -> None:
        sd: SensitiveData[str] = SensitiveData("key")
        assert hash(sd) == hash("key")

    def test_usable_as_dict_key(self) -> None:
        sd: SensitiveData[str] = SensitiveData("k")
        d: dict[SensitiveData[str], int] = {sd: 1}
        assert d[sd] == 1


# ---------------------------------------------------------------------------
# is_sensitive()
# ---------------------------------------------------------------------------


class TestIsSensitive:
    def test_true_for_sensitive_data(self) -> None:
        assert is_sensitive(SensitiveData("x")) is True

    def test_false_for_plain_string(self) -> None:
        assert is_sensitive("hello") is False

    def test_false_for_none(self) -> None:
        assert is_sensitive(None) is False

    def test_false_for_int(self) -> None:
        assert is_sensitive(42) is False


# ---------------------------------------------------------------------------
# strip_sensitive_fields() — dict input
# ---------------------------------------------------------------------------


class TestStripSensitiveFieldsDict:
    def test_removes_sensitive_values(self) -> None:
        data: dict[str, Any] = {
            "user_id": "u-123",
            "email": SensitiveData("alice@example.com"),
            "action": "login",
        }
        result = strip_sensitive_fields(data)
        assert "email" not in result

    def test_retains_non_sensitive_values(self) -> None:
        data: dict[str, Any] = {
            "user_id": "u-123",
            "email": SensitiveData("alice@example.com"),
            "action": "login",
        }
        result = strip_sensitive_fields(data)
        assert result["user_id"] == "u-123"
        assert result["action"] == "login"

    def test_empty_dict(self) -> None:
        assert strip_sensitive_fields({}) == {}

    def test_all_sensitive(self) -> None:
        data: dict[str, Any] = {"a": SensitiveData("x"), "b": SensitiveData(1)}
        assert strip_sensitive_fields(data) == {}

    def test_no_sensitive_fields(self) -> None:
        data: dict[str, Any] = {"x": 1, "y": "hello"}
        assert strip_sensitive_fields(data) == {"x": 1, "y": "hello"}

    def test_does_not_mutate_original(self) -> None:
        sd: SensitiveData[str] = SensitiveData("secret")
        data: dict[str, Any] = {"a": sd, "b": "safe"}
        strip_sensitive_fields(data)
        assert "a" in data  # original unchanged


# ---------------------------------------------------------------------------
# strip_sensitive_fields() — dataclass input
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _UserEvent:
    user_id: str
    email: SensitiveData[str]
    action: str


@dataclasses.dataclass
class _AllClean:
    x: int
    y: str


class TestStripSensitiveFieldsDataclass:
    def test_removes_sensitive_field(self) -> None:
        event = _UserEvent(user_id="u-1", email=SensitiveData("a@b.com"), action="view")
        result = strip_sensitive_fields(event)
        assert "email" not in result

    def test_retains_non_sensitive_fields(self) -> None:
        event = _UserEvent(user_id="u-2", email=SensitiveData("c@d.com"), action="edit")
        result = strip_sensitive_fields(event)
        assert result["user_id"] == "u-2"
        assert result["action"] == "edit"

    def test_all_clean_dataclass(self) -> None:
        obj = _AllClean(x=1, y="hello")
        assert strip_sensitive_fields(obj) == {"x": 1, "y": "hello"}


# ---------------------------------------------------------------------------
# strip_sensitive_fields() — error handling
# ---------------------------------------------------------------------------


class TestStripSensitiveFieldsErrors:
    def test_raises_on_unsupported_type(self) -> None:
        with pytest.raises(TypeError, match="dataclass or dict"):
            strip_sensitive_fields([1, 2, 3])  # type: ignore[arg-type]

    def test_raises_on_plain_string(self) -> None:
        with pytest.raises(TypeError, match="dataclass or dict"):
            strip_sensitive_fields("hello")  # type: ignore[arg-type]
