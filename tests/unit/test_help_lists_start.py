"""``bernstein --help`` lists the documented ``start`` command (issue #2807).

The cluster deployment doc starts the central node with ``bernstein start``, but
the curated ``--help`` omitted it, so an operator following the doc could not
discover it. It is registered and invocable; the curated help now surfaces it.
"""

from __future__ import annotations

import io

from rich.console import Console

from bernstein.cli import main as main_module


def test_help_lists_start() -> None:
    buf = io.StringIO()
    original = main_module.console
    main_module.console = Console(file=buf, force_terminal=False, width=200)
    try:
        main_module.print_rich_help()
    finally:
        main_module.console = original

    output = buf.getvalue()
    # Distinctive to the curated ``start`` row (avoids matching "quick start"
    # or the "--fresh ... start clean" option text).
    assert "spawn manager" in output
