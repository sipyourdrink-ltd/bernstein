"""Alias re-export for ``bernstein govern`` group.

The actual commands live in ``governance_cmd.py`` where they are registered
on the ``governance`` group. This module exists so that::

    from bernstein.cli.commands.govern_cmd import govern_group

resolves to the same group object that main.py registers under the ``govern``
CLI alias.
"""

from __future__ import annotations

from bernstein.cli.commands.governance_cmd import govern_group

__all__ = ["govern_group"]
