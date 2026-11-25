"""Operator-owned policy for executable task completion checks."""

from __future__ import annotations

import json
import os
import re


def approved_check(identifier: str) -> list[str] | None:
    """Return argv bound to an exact identifier in the operator environment."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", identifier) is None:
        return None
    try:
        policy = json.loads(os.environ.get("BERNSTEIN_COMPLETION_CHECKS", "{}"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(policy, dict):
        return None
    command = policy.get(identifier)
    if (
        not isinstance(command, list)
        or not command
        or any(type(arg) is not str or not arg or "\x00" in arg for arg in command)
    ):
        return None
    return command
