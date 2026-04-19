"""First-party sandbox backends.

Each backend module is self-contained. Optional backends (``e2b``,
``modal``) import their provider SDK lazily so importing this package
never pulls in heavyweight optional dependencies.
"""

from __future__ import annotations

__all__: list[str] = []
