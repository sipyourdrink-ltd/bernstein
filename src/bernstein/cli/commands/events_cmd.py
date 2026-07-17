"""Events CLI -- the chain-projected unified event feed (#2548).

The feed is not a sixth event system: it is the HMAC-chained audit log seen
through one canonical grammar. ``bernstein events query`` projects a contiguous
chain slice into a fence-posted window whose response embeds the from/to HMAC
fence-posts of the underlying slice, so the window is checkable offline for
completeness and order -- ordering disputes settle by chain position, not by
timestamps scattered across five files.

Commands:
  bernstein events query    Project a chain slice into a canonical feed window.
  bernstein events verify   Verify a saved window offline for completeness/order.

Grammar reference: docs/events/grammar.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import console

AUDIT_DIR = Path(".sdd/audit")


@click.group("events")
def events_group() -> None:
    """Unified event feed - a verifiable projection of the audit chain.

    See docs/events/grammar.md for the grammar reference and rule cookbook.
    """


@events_group.command("query")
@click.option(
    "--from",
    "from_hmac",
    default=None,
    help="Inclusive lower bound: HMAC of the first event to include. Omit to start at the earliest recorded event.",
)
@click.option(
    "--to",
    "to_hmac",
    default=None,
    help="Inclusive upper bound: HMAC of the last event to include. Omit to run through the latest event.",
)
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(dir_okay=False, writable=True, resolve_path=True),
    help="Write the canonical window to this file instead of stdout.",
)
@click.option("--jsonl", "as_jsonl", is_flag=True, help="Emit events as canonical JSONL instead of a JSON envelope.")
def query_cmd(from_hmac: str | None, to_hmac: str | None, output: str | None, as_jsonl: bool) -> None:
    """Project a chain slice into a canonical, fence-posted feed window.

    \b
    The default output is a canonical JSON envelope carrying the from/to HMAC
    fence-posts and the projected events. Two hosts holding the same chain bytes
    emit byte-identical output for the same window, so the result can be hashed,
    diffed, or shipped to a downstream verifier with no ambiguity.

    \b
    Examples:
      bernstein events query --from <hash> --to <hash>
      bernstein events query --to <hash> -o /tmp/window.json
      bernstein events query --jsonl
    """
    from bernstein.core.events.feed import project_window
    from bernstein.core.security.audit_slice import AuditSliceError, slice_audit_log

    if not AUDIT_DIR.is_dir():
        console.print(f"[red]Audit directory not found:[/red] {AUDIT_DIR}")
        raise SystemExit(1)

    try:
        slice_result = slice_audit_log(AUDIT_DIR, from_hmac=from_hmac, to_hmac=to_hmac)
    except AuditSliceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    window = project_window(slice_result)
    payload = window.to_jsonl() if as_jsonl else window.to_envelope_json()

    if output is not None:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + ("" if as_jsonl else "\n"), encoding="utf-8")
        console.print(f"[green]Wrote {window.count} events[/green] to {out_path}")
        console.print(f"  from={window.from_hmac or '(genesis)'} to={window.to_hmac or '(latest)'}")
    else:
        click.echo(payload)


@events_group.command("verify")
@click.argument("window_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
def verify_cmd(window_file: str) -> None:
    """Verify a saved feed window offline for completeness and order.

    \b
    Reads a JSON envelope written by ``events query`` and checks its internal
    prev_hmac linkage against the embedded fence-posts. No signing key is needed:
    deleting, inserting, or reordering any single event breaks the linkage or
    moves a fence-post and fails the check. Exits non-zero on any violation.
    """
    from bernstein.core.events.feed import FeedWindow, verify_window

    raw = Path(window_file).read_text(encoding="utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        console.print(f"[red]Malformed window file:[/red] {exc}")
        raise SystemExit(1) from None
    if not isinstance(envelope, dict) or "events" not in envelope:
        console.print("[red]Window file is not a feed envelope (missing 'events').[/red]")
        raise SystemExit(1)

    window = FeedWindow.from_envelope(cast("dict[str, Any]", envelope))
    ok, errors = verify_window(window, expect_from=window.from_hmac, expect_to=window.to_hmac)
    if ok:
        console.print(f"[green]Window verified:[/green] {window.count} events, order and completeness intact")
        return
    console.print("[bold red]Window verification FAILED[/bold red]")
    for err in errors:
        console.print(f"  [red]![/red] {err}")
    raise SystemExit(1)


__all__ = ["events_group"]
