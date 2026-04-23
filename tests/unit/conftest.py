"""Unit test conftest — ensure the local src/ takes priority on sys.path.

In git worktrees the parent project's venv may appear earlier on sys.path
than the worktree's own src/, causing new modules to be shadowed.  This
conftest inserts the worktree's src/ at position 0 so that imports always
resolve to the locally checked-out code.
"""

from __future__ import annotations

import sys
from pathlib import Path

# This file lives at tests/unit/conftest.py → parent.parent is the repo root
_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Re-export the shared adapter-test fixture so adapter test modules can opt
# in via ``pytestmark = pytest.mark.usefixtures("no_watchdog_threads")``.
# The helpers module keeps the ``make_popen_mock`` / ``inner_cmd`` callables
# test modules import directly.
from tests.unit._adapter_test_helpers import no_watchdog_threads  # noqa: F401
