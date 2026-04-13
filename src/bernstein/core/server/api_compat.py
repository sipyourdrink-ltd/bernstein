"""API backward compatibility checker.

Compares function/method signatures to detect breaking changes such as
removed required parameters, added required parameters, parameters that
changed from optional to required, or parameter kind changes (e.g. a
positional-or-keyword parameter becoming keyword-only).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum

_PK = inspect.Parameter


class ChangeKind(Enum):
    """Types of signature changes."""

    REMOVED_REQUIRED_PARAM = "removed_required_param"
    ADDED_REQUIRED_PARAM = "added_required_param"
    PARAM_BECAME_REQUIRED = "param_became_required"
    PARAM_BECAME_KEYWORD_ONLY = "param_became_keyword_only"
    PARAM_BECAME_POSITIONAL_ONLY = "param_became_positional_only"


@dataclass
class SignatureChange:
    """A single detected change between two function signatures."""

    kind: ChangeKind
    param_name: str


def _check_removed_or_changed_params(
    params_before: dict[str, inspect.Parameter],
    params_after: dict[str, inspect.Parameter],
) -> list[SignatureChange]:
    """Detect removed required params and parameter kind regressions."""
    changes: list[SignatureChange] = []
    for name, param in params_before.items():
        if param.kind in (_PK.VAR_POSITIONAL, _PK.VAR_KEYWORD):
            continue
        if name not in params_after:
            if param.default is _PK.empty and param.kind is not _PK.POSITIONAL_ONLY:
                changes.append(SignatureChange(kind=ChangeKind.REMOVED_REQUIRED_PARAM, param_name=name))
            continue
        new_param = params_after[name]
        old_kind = param.kind
        new_kind = new_param.kind
        if param.default is not _PK.empty and new_param.default is _PK.empty:
            changes.append(SignatureChange(kind=ChangeKind.PARAM_BECAME_REQUIRED, param_name=name))
        if old_kind is _PK.POSITIONAL_OR_KEYWORD and new_kind is _PK.KEYWORD_ONLY:
            changes.append(SignatureChange(kind=ChangeKind.PARAM_BECAME_KEYWORD_ONLY, param_name=name))
        if old_kind in (_PK.POSITIONAL_OR_KEYWORD, _PK.KEYWORD_ONLY) and new_kind is _PK.POSITIONAL_ONLY:
            changes.append(SignatureChange(kind=ChangeKind.PARAM_BECAME_POSITIONAL_ONLY, param_name=name))
    return changes


def _check_added_required_params(
    params_before: dict[str, inspect.Parameter],
    params_after: dict[str, inspect.Parameter],
) -> list[SignatureChange]:
    """Detect newly added required parameters."""
    changes: list[SignatureChange] = []
    for name, param in params_after.items():
        if param.kind in (_PK.VAR_POSITIONAL, _PK.VAR_KEYWORD):
            continue
        if name not in params_before and param.default is _PK.empty:
            changes.append(SignatureChange(kind=ChangeKind.ADDED_REQUIRED_PARAM, param_name=name))
    return changes


def compare_signatures(
    before: object,
    after: object,
) -> list[SignatureChange]:
    """Compare two callable signatures and return breaking changes.

    Detects the following breaking changes:

    * Removing a required (no-default) parameter.
    * Adding a new required parameter.
    * Changing a parameter from optional (has default) to required.
    * Changing a positional-or-keyword parameter to keyword-only (breaks
      callers that pass it positionally).
    * Changing a positional-or-keyword or keyword-only parameter to
      positional-only (breaks callers that pass it by name).

    Non-breaking changes (not reported):

    * Adding an optional parameter.
    * Making a required parameter optional.
    * Renaming a positional-only parameter (callers cannot use the name).

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

    changes = _check_removed_or_changed_params(params_before, params_after)
    changes.extend(_check_added_required_params(params_before, params_after))
    return changes
