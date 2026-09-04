"""Agent identity lives in exactly one module namespace (issue #5097).

Two production modules used to be named ``agent_identity.py`` - one under
``core/agents/`` holding the JWT-backed identity, one under ``core/security/``
holding the Ed25519-signed card - and they carried unrelated types that both
answered "who is this agent". These guards keep that from coming back: no two
modules of the three identity-bearing packages may share a basename, and the
two retired import paths must fail loudly with their successor named rather
than silently resolving to something.
"""

from __future__ import annotations

import importlib
import sys
from collections import defaultdict
from pathlib import Path

import pytest

import bernstein

#: Packages whose own modules must not collide by basename. Nested
#: sub-packages (``security/vault``, ``identity/spiffe``) are deliberately
#: separate namespaces and are not part of this comparison.
SCANNED_PACKAGES: tuple[str, ...] = ("core/identity", "core/security", "core/agents")

#: Retired module path -> the module that now owns its contents.
TOMBSTONED_IMPORT_PATHS: dict[str, str] = {
    "bernstein.core.agents.agent_identity": "bernstein.core.identity.agent_jwt",
    "bernstein.core.security.agent_identity": "bernstein.core.identity.agent_card",
}


def _modules_by_basename() -> dict[str, list[str]]:
    package_root = Path(bernstein.__file__).resolve().parent
    found: dict[str, list[str]] = defaultdict(list)
    for relative in SCANNED_PACKAGES:
        for path in sorted((package_root / relative).glob("*.py")):
            if path.name == "__init__.py":
                continue
            found[path.name].append(f"{relative}/{path.name}")
    return found


def test_no_shared_basename_under_identity_security_agents() -> None:
    """No basename appears in more than one of the identity-bearing packages."""
    duplicates = {name: paths for name, paths in _modules_by_basename().items() if len(paths) > 1}
    assert duplicates == {}, f"modules sharing a basename across identity packages: {duplicates}"


@pytest.mark.parametrize(("retired", "successor"), sorted(TOMBSTONED_IMPORT_PATHS.items()))
def test_old_agent_identity_import_paths_raise_with_successor_named(retired: str, successor: str) -> None:
    """Importing a retired identity module fails and names where the contents went."""
    sys.modules.pop(retired, None)
    with pytest.raises(ImportError) as excinfo:
        importlib.import_module(retired)
    assert successor in str(excinfo.value)
