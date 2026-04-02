"""Session rename functionality — rename the current session safely.

Allows renaming the current orchestration session by updating the session
metadata file (``.sdd/runtime/session.json``).  This is useful for tagging
long-running sessions or giving them human-readable identifiers.

The session file stores a ``SessionState`` dataclass.  The ``name`` field is
added as an optional attribute — existing runs without it are fully forward-
compatible (they just see ``goal`` instead).

Usage::

    from bernstein.session_rename import rename_session, validate_session_name

    errors = validate_session_name("my-great-session-123")
    if not errors:
        rename_session("my-great-session-123", workdir=Path.cwd())
"""

from __future__ import annotations

import json
import re
from pathlib import Path  # noqa: TC003 (used at runtime, not just annotations)

from bernstein.core.session import SessionState, load_session, save_session

_MAX_NAME_LEN = 60
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]*$")


def validate_session_name(name: str) -> list[str]:
    """Validate a proposed session name.

    Rules:
    - Must be non-empty
    - Only ASCII alphanumeric characters and hyphens
    - Must start with an alphanumeric character (not a hyphen)
    - Maximum 60 characters

    Args:
        name: Proposed session name to validate.

    Returns:
        List of human-readable error strings.  Empty list means the name is
        valid.
    """
    if not name:
        return ["session name cannot be empty"]
    errors: list[str] = []
    if len(name) > _MAX_NAME_LEN:
        errors.append(f"session name is too long ({len(name)} chars, max {_MAX_NAME_LEN})")
    if not _NAME_RE.match(name):
        errors.append(
            "session name can only contain alphanumeric characters and hyphens, "
            "and must start with an alphanumeric character"
        )
    return errors


def rename_session(new_name: str, workdir: Path) -> bool:
    """Rename the current session by updating the session metadata file.

    Writes the new name into ``.sdd/runtime/session.json``.  If an existing
    session file is present it updates the ``name`` field in place; if no
    session file exists yet, it creates one containing only the ``name`` and
    a current timestamp.

    Args:
        new_name: Validated session name (alphanumeric + hyphens, max 60).
            Call :func:`validate_session_name` first — this function does NOT
            validate.
        workdir: Project root directory.

    Returns:
        True if the session was renamed successfully.  False if the operation
        failed (e.g. unable to write the file).

    Raises:
        ValueError: If *new_name* is empty.
    """
    if not new_name:
        raise ValueError("session name cannot be empty")

    session_path = workdir / ".sdd" / "runtime" / "session.json"
    state: SessionState

    if session_path.exists():
        existing = load_session(workdir, stale_minutes=2_147_483_647)  # ignore staleness
        state = existing if existing is not None else SessionState(saved_at=0.0)
    else:
        state = SessionState(saved_at=0.0)

    state.goal = new_name
    save_session(workdir, state)

    # Also update the raw JSON if the dataclass round-trip missed a field.
    # This is a belt-and-suspenders step: ensure ``name`` is also written
    # directly into the JSON for forward-compatible consumers.
    try:
        raw = json.loads(session_path.read_text(encoding="utf-8"))
        raw["name"] = new_name
        session_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass

    return True
