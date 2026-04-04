"""API backward compatibility checker for function/method signatures.

Compares two callables and returns a list of breaking changes — parameter
removals, new required parameters, or optional-to-required promotions.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class ChangeKind(StrEnum):
    """Classification of a signature change."""

    REMOVED_REQUIRED_PARAM = "removed_required_param"
    ADDED_REQUIRED_PARAM = "added_required_param"
    PARAM_BECAME_REQUIRED = "param_became_required"


@dataclass(frozen=True)
class SignatureChange:
    """A single detected breaking change between two function signatures."""

    kind: ChangeKind
    param_name: str

    def __repr__(self) -> str:
        return f"SignatureChange({self.kind.value!r}, {self.param_name!r})"


def _has_default(param: inspect.Parameter) -> bool:
    return param.default is not inspect.Parameter.empty


def compare_signatures(
    before: Callable[..., Any],
    after: Callable[..., Any],
) -> list[SignatureChange]:
    """Compare two callables and return breaking signature changes.

    Args:
        before: The original callable (pre-change).
        after: The updated callable (post-change).

    Returns:
        A list of :class:`SignatureChange` objects describing each breaking
        change.  Returns an empty list when the signatures are
        backward-compatible.

    Breaking changes detected:
    - A required parameter was removed (callers supplying it positionally break).
    - A new required parameter was added (existing callers miss the argument).
    - An optional parameter became required (callers relying on the default break).
    """
    _SKIP = {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}

    before_params: dict[str, inspect.Parameter] = {
        name: p
        for name, p in inspect.signature(before).parameters.items()
        if p.kind not in _SKIP
    }
    after_params: dict[str, inspect.Parameter] = {
        name: p
        for name, p in inspect.signature(after).parameters.items()
        if p.kind not in _SKIP
    }

    changes: list[SignatureChange] = []

    # Removed required parameters — breaking for positional or keyword callers.
    for name, param in before_params.items():
        if name not in after_params and not _has_default(param):
            changes.append(SignatureChange(ChangeKind.REMOVED_REQUIRED_PARAM, name))

    # New or changed parameters in `after`.
    for name, after_param in after_params.items():
        if name not in before_params:
            # Completely new parameter.
            if not _has_default(after_param):
                changes.append(SignatureChange(ChangeKind.ADDED_REQUIRED_PARAM, name))
        else:
            before_param = before_params[name]
            # Was optional, now required — callers that relied on the default break.
            if _has_default(before_param) and not _has_default(after_param):
                changes.append(SignatureChange(ChangeKind.PARAM_BECAME_REQUIRED, name))

    return changes
