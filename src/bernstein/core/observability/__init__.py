"""observability sub-package.

audit-173: the previous implementation exposed a ``__getattr__`` that walked
``pkgutil.iter_modules`` and lazy-imported every submodule on first attribute
access. That magic defeated static analysis tools (Pyright, Vulture, unimport)
because any attribute could be resolved at runtime, so dead submodules in this
package accreted undetected (see audit-170).

All production importers use fully-qualified submodule paths
(``from bernstein.core.observability.<submodule> import X``) or submodule-style
imports (``from bernstein.core.observability import <submodule>``), both of
which Python's native import machinery handles without any package-level
``__getattr__``. Legacy flat-path names are still served by the meta_path
finder in ``bernstein.core.__init__`` (``_REDIRECT_MAP``); we do NOT re-add the
``pkgutil`` walker here.

If new code needs a symbol re-exported at the package level, import it
explicitly and add it to ``__all__`` below.
"""

from __future__ import annotations

__all__: list[str] = []
