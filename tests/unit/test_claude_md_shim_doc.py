"""Guard against CLAUDE.md drift re: the core/ back-compat shim mechanism.

audit-012: CLAUDE.md used to claim that ``orchestrator.py``, ``spawner.py``,
``task_lifecycle.py`` were top-level shim files under ``src/bernstein/core/``.
They are not — back-compat is provided by a ``sys.meta_path`` finder
(``_CoreRedirectFinder``) driven by ``_REDIRECT_MAP`` inside
``src/bernstein/core/__init__.py``.

These tests lock the documentation and the mechanism together so the drift
cannot silently return.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from bernstein import core as core_pkg

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"
_CORE_DIR = _REPO_ROOT / "src" / "bernstein" / "core"


def _claude_md_text() -> str:
    """Return the CLAUDE.md contents (guard against missing file)."""
    assert _CLAUDE_MD.exists(), f"CLAUDE.md missing at {_CLAUDE_MD}"
    return _CLAUDE_MD.read_text(encoding="utf-8")


def test_no_physical_shim_files_for_documented_names() -> None:
    """The names previously called 'top-level shims' must stay virtual.

    If someone adds a physical ``orchestrator.py`` / ``spawner.py`` /
    ``task_lifecycle.py`` under ``src/bernstein/core/`` it will shadow the
    meta-path finder and drift from the sub-package source of truth.
    """
    for name in ("orchestrator", "spawner", "task_lifecycle"):
        path = _CORE_DIR / f"{name}.py"
        assert not path.exists(), (
            f"{path} exists as a physical shim; back-compat must go through "
            "_REDIRECT_MAP in src/bernstein/core/__init__.py instead."
        )


def test_redirect_map_covers_documented_names() -> None:
    """The redirect map must still handle the three documented legacy names."""
    redirect_map = core_pkg._REDIRECT_MAP
    for name in ("orchestrator", "spawner", "task_lifecycle"):
        assert name in redirect_map, (
            f"_REDIRECT_MAP is missing {name!r}; legacy import path "
            f"bernstein.core.{name} will break."
        )
        target = redirect_map[name]
        # Importing the target must succeed — the finder relies on it.
        importlib.import_module(target)


def test_legacy_import_paths_still_work() -> None:
    """``from bernstein.core.<old> import ...`` must still resolve."""
    # Import via the legacy path; the meta-path finder should redirect.
    orch = importlib.import_module("bernstein.core.orchestrator")
    spawn = importlib.import_module("bernstein.core.spawner")
    tlc = importlib.import_module("bernstein.core.task_lifecycle")
    # Verify they are real modules (have __name__).
    for mod in (orch, spawn, tlc):
        assert hasattr(mod, "__name__")


def test_claude_md_does_not_claim_physical_shims() -> None:
    """CLAUDE.md must not describe orchestrator.py/spawner.py/task_lifecycle.py
    as real files — that is the exact drift audit-012 flagged.
    """
    text = _claude_md_text()
    # The old drifted bullet — must be gone.
    assert "Top-level shims:" not in text, (
        "CLAUDE.md still uses the drifted 'Top-level shims:' phrasing. "
        "See audit-012: these files don't exist; back-compat is via "
        "_CoreRedirectFinder in core/__init__.py."
    )


def test_claude_md_documents_the_real_mechanism() -> None:
    """CLAUDE.md must point readers at the real back-compat mechanism."""
    text = _claude_md_text()
    # The replacement bullet must reference the finder + map by name so
    # engineers know where to add new aliases.
    assert "_CoreRedirectFinder" in text, (
        "CLAUDE.md should reference _CoreRedirectFinder so contributors "
        "can find the real back-compat mechanism."
    )
    assert "_REDIRECT_MAP" in text, (
        "CLAUDE.md should reference _REDIRECT_MAP so contributors know "
        "where to add new legacy aliases."
    )
