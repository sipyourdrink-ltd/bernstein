"""Import-time regression tests for the ``bernstein.core.server`` package.

These guard against two coupled failure modes surfaced when a re-export in
``server.py`` referenced a symbol that ``server_middleware.py`` had stopped
defining:

* ``import bernstein.core.server.server`` raised ``ImportError`` outright
  (the stale ``_PUBLIC_PATH_PREFIXES`` re-export);
* ``from bernstein.core.server import <submodule>`` (e.g. ``server_launch``)
  failed *in isolation* because the package ``__getattr__`` fallback loop
  imports ``bernstein.core.server.server`` and only caught ``AttributeError``,
  leaking the unrelated ``ImportError`` for any attribute miss.

The subprocess tests use a *fresh* interpreter on purpose: once a module is
cached in ``sys.modules`` a repeated ``import`` is a no-op and would mask the
regression. Only a cold import re-executes ``server.py`` top-to-bottom.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def _import_in_fresh_interpreter(statement: str) -> subprocess.CompletedProcess[str]:
    """Run *statement* in a cold interpreter and return the completed process."""
    script = f"import sys; sys.path.insert(0, {_SRC!r})\n{statement}\nprint('ok')"
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_server_module_in_fresh_interpreter() -> None:
    proc = _import_in_fresh_interpreter("import bernstein.core.server.server")
    assert proc.returncode == 0, "import bernstein.core.server.server failed:\n" + proc.stderr
    assert proc.stdout.strip() == "ok"


def test_from_server_import_submodule_in_fresh_interpreter() -> None:
    proc = _import_in_fresh_interpreter("from bernstein.core.server import server_launch")
    assert proc.returncode == 0, "from bernstein.core.server import server_launch failed:\n" + proc.stderr
    assert proc.stdout.strip() == "ok"


def test_getattr_tolerates_broken_fallback_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken fallback module must not corrupt unrelated attribute lookups.

    If any module in the ``__getattr__`` fallback loop fails to import, an
    attribute that is genuinely absent everywhere must still raise
    ``AttributeError`` - never leak the fallback module's ``ImportError``.
    """
    import bernstein.core.server as srv

    real_import_module = importlib.import_module

    def _fake_import_module(name: str, *args: object, **kwargs: object) -> object:
        if name == "bernstein.core.server.server":
            raise ImportError("simulated broken fallback")
        return real_import_module(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    with pytest.raises(AttributeError):
        _ = srv.this_attribute_does_not_exist_anywhere
