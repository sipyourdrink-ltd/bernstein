"""Type-level guards for sensitive / PII data in metrics and storage.

Usage
-----
Wrap sensitive field values in ``SensitiveData`` to prevent accidental logging::

    from bernstein.core.sensitive_data import SensitiveData, strip_sensitive_fields

    @dataclass
    class UserEvent:
        user_id: str
        email: SensitiveData[str]   # PII — redacted in logs / storage
        action: str

    event = UserEvent(
        user_id="u-123",
        email=SensitiveData("alice@example.com"),
        action="login",
    )

    # Accidentally logging the whole event is safe:
    #   str(event.email)  →  "<redacted>"

    # Explicit exposure is required to access the value:
    raw_email = event.email.expose()

    # Strip all SensitiveData fields before writing to general storage:
    safe_dict = strip_sensitive_fields(event)
    # → {"user_id": "u-123", "action": "login"}
"""

from __future__ import annotations

import dataclasses
from typing import Any

__all__ = [
    "SensitiveData",
    "is_sensitive",
    "strip_sensitive_fields",
]


class SensitiveData[T]:
    """Opaque wrapper that marks a value as PII / sensitive.

    The wrapped value is never exposed through ``__str__``, ``__repr__``,
    or comparison operators — callers must call :meth:`expose` explicitly.
    This makes accidental logging safe by design.

    Args:
        value: The sensitive value to protect.
    """

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def expose(self) -> T:
        """Return the raw sensitive value.

        This is the *only* way to access the underlying data.  The call site
        becomes a clear audit trail that PII is intentionally being used.

        Returns:
            The wrapped sensitive value.
        """
        return self._value

    def __repr__(self) -> str:  # pragma: no cover — intentional blanket
        return "SensitiveData(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __eq__(self, other: object) -> bool:
        """Equality is intentionally opaque — compare via .expose() if needed."""
        if isinstance(other, SensitiveData):
            return self._value == other._value  # type: ignore[operator]
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


def is_sensitive(value: object) -> bool:
    """Return True if *value* is a :class:`SensitiveData` instance.

    Args:
        value: Any object to test.

    Returns:
        True if the object is wrapped in SensitiveData.
    """
    return isinstance(value, SensitiveData)


def strip_sensitive_fields(obj: Any) -> dict[str, Any]:
    """Return a copy of *obj* as a plain dict with all sensitive fields removed.

    Accepts dataclasses or plain dicts.  Nested ``SensitiveData`` values are
    dropped entirely (not redacted) so that the result is safe for general
    storage, metrics export, or JSON serialisation.

    Args:
        obj: A dataclass instance or dict whose fields may contain
            :class:`SensitiveData` values.

    Returns:
        A shallow dict containing only non-sensitive fields.

    Raises:
        TypeError: If *obj* is neither a dataclass instance nor a dict.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        source: dict[str, Any] = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    elif isinstance(obj, dict):
        source = dict(obj)
    else:
        raise TypeError(f"strip_sensitive_fields expects a dataclass or dict, got {type(obj).__name__!r}")

    return {k: v for k, v in source.items() if not isinstance(v, SensitiveData)}
