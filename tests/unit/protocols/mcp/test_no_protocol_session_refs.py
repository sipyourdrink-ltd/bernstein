"""Repo-wide guard: no protocol session ids outside the compat shim.

Issue #2506, final acceptance criterion. The stateless MCP core anchors
cross-call continuity in the run journal and the audit chain; a protocol
session id must not reappear on any MCP wire path. The one sanctioned home
for the legacy token is the compat shim in ``stateless_core.py``, which
accepts (and ignores) the header for a bounded window.

The scan targets the wire-level tokens (header names and query parameters),
not the word "session" itself: auth sessions keyed by server name and
client-connection objects are not protocol sessions.
"""

from __future__ import annotations

import re
from pathlib import Path

import bernstein

_SRC_ROOT = Path(bernstein.__file__).resolve().parent

#: Every MCP wire-path package that must stay free of protocol session ids.
_SCANNED_DIRS = (
    _SRC_ROOT / "mcp",
    _SRC_ROOT / "core" / "protocols" / "mcp",
)

_SCANNED_FILES = (_SRC_ROOT / "core" / "protocols" / "protocol_negotiation.py",)

#: The compat shim is the only module allowed to name the legacy tokens.
_SHIM_MODULE = _SRC_ROOT / "core" / "protocols" / "mcp" / "stateless_core.py"

#: Wire-level protocol session id tokens, case-insensitive.
_TOKEN_PATTERN = re.compile(r"mcp-session-id|mcp_session_id|mcpsessionid|sessionid", re.IGNORECASE)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in _SCANNED_DIRS:
        files.extend(sorted(directory.rglob("*.py")))
    files.extend(_SCANNED_FILES)
    return files


def test_no_protocol_session_ids_outside_the_compat_shim() -> None:
    offenders: list[str] = []
    for path in _python_files():
        if path == _SHIM_MODULE:
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _TOKEN_PATTERN.search(line):
                offenders.append(f"{path.relative_to(_SRC_ROOT)}:{line_no}: {line.strip()}")
    assert not offenders, "protocol session id references outside the compat shim:\n" + "\n".join(offenders)


def test_scan_actually_covers_the_wire_paths() -> None:
    """Guard the guard: the scanned set must include the migrated modules."""
    names = {p.name for p in _python_files()}
    assert {"mcp_client.py", "remote_transport.py", "mcp_gateway.py", "protocol_negotiation.py"} <= names
