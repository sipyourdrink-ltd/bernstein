"""API backward compatibility checker.

Compares function/method signatures to detect breaking changes such as
removed required parameters, added required parameters, or parameters that
changed from optional to required.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum


class ChangeKind(Enum):
    """Types of signature changes."""

    REMOVED_REQUIRED_PARAM = "removed_required_param"
    ADDED_REQUIRED_PARAM = "added_required_param"
    PARAM_BECAME_REQUIRED = "param_became_required"


@dataclass
class SignatureChange:
    """A single detected change between two function signatures."""

    kind: ChangeKind
    param_name: str


def compare_signatures(
    before: object,
    after: object,
) -> list[SignatureChange]:
    """Compare two callable signatures and return breaking changes.

    Args:
        before: The original callable.
        after: The new callable to compare against.

    Returns:
        List of breaking changes. Empty list means backward-compatible.
    """
    sig_before = inspect.signature(before)  # type: ignore[arg-type]
    sig_after = inspect.signature(after)  # type: ignore[arg-type]

    params_before = sig_before.parameters
    params_after = sig_after.parameters

    changes: list[SignatureChange] = []

    for name, param in params_before.items():
        if name not in params_after and param.default is inspect.Parameter.empty:
            changes.append(SignatureChange(kind=ChangeKind.REMOVED_REQUIRED_PARAM, param_name=name))

    for name, param in params_after.items():
        if name not in params_before:
            if param.default is inspect.Parameter.empty:
                changes.append(SignatureChange(kind=ChangeKind.ADDED_REQUIRED_PARAM, param_name=name))
        elif name in params_before:
            old_param = params_before[name]
            if old_param.default is not inspect.Parameter.empty and param.default is inspect.Parameter.empty:
                changes.append(SignatureChange(kind=ChangeKind.PARAM_BECAME_REQUIRED, param_name=name))

    return changes
