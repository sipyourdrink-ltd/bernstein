"""Per-platform chat drivers registered under the ``bernstein`` pluggy namespace.

Each driver lives in its own submodule so that an optional third-party
SDK (``python-telegram-bot``, ``discord.py``, ``slack-sdk``) is only
imported when the matching driver is actually instantiated.
"""

from __future__ import annotations

__all__: list[str] = []
