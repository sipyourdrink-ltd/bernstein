"""``bernstein govern inventory --render``: topology graph from the store.

Issue #5133. ``--render`` copies the ``click.Choice`` wiring of
``graph_cmd.graph_tasks`` (``--format`` at ``graph_cmd.py:129-141``). The
walk itself is the inventory store, not the task DAG.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

EXIT_OK = 0
EXIT_STORE = 1


@click.group("govern")
def govern_group() -> None:
    """Govern an enumerated agent surface.

    \b
      bernstein govern inventory --render mermaid|dot --store PATH
    """


@govern_group.command("inventory")
@click.option(
    "--render",
    "output_format",
    type=click.Choice(["mermaid", "dot"], case_sensitive=False),
    required=True,
    help="Emit the topology graph from the store as Mermaid or Graphviz DOT.",
)
@click.option(
    "--store",
    "store_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Inventory graph JSON (nodes + edges). Hand-written fixture until #5129.",
)
def govern_inventory_cmd(output_format: str, store_path: Path) -> None:
    """Print the inventory topology graph from the store.

    Exit codes: 0 = emitted, 1 = store unreadable or not a JSON object, 2 = usage.

    Output is the graph only (no Rich chrome), so two runs over the same
    store compare equal as bytes.
    """
    from bernstein.core.govern.inventory_render import load_inventory_store, render_inventory

    try:
        store = load_inventory_store(store_path)
        click.echo(render_inventory(store, output_format))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_STORE) from exc
    raise SystemExit(EXIT_OK)
