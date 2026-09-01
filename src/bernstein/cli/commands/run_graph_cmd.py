"""``bernstein worktrees graph`` - show one fan-out's sealed run graph.

A fan-out's branches are sealed into a single receipt under
``.sdd/run-graph/``, but until now nothing rendered one: an operator holding a
receipt hash had a file of hashes and no way to see which branches it covers
or whether they still agree with it.

Three things this command is careful about, because each has a wrong answer
that looks like a right one:

* **A fan-out is named by its receipt hash.** There is no separate id to
  invent - the receipt is content-addressed and its hash *is* the fan-out's
  identity, so a hand-edited fan-out is a different fan-out and says so. A
  unique prefix is accepted the way git accepts a short sha; an ambiguous one
  names its candidates instead of picking the first.

* **A missing input is not a failing branch.** A branch with no ``run_id`` in
  the supplied mapping cannot be paired with a spine, so nothing can be said
  about it. It renders as *unresolved*, never as failing, and it does not set
  the exit status. Reporting "cannot check" as "failed" would train an
  operator to ignore the one line that matters.

* **The receipt does not name its own branches.** It seals opaque node
  hashes, so the branch names have to come from re-deriving the graph off the
  tree. That is also what makes the rendering meaningful: what you see is the
  tree as it is now, checked against what was sealed, not a replay of the
  receipt's own fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.tree import Tree

from bernstein.cli.helpers import console

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.lineage.run_graph import RunGraph

__all__ = ["parse_run_ids", "resolve_receipt_path", "run_graph_cmd"]

#: Where ``build_run_graph_receipt`` writes each sealed fan-out.
RECEIPT_RELDIR = Path(".sdd") / "run-graph"

_HASH_PREFIX = "sha256:"


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def parse_run_ids(pairs: tuple[str, ...]) -> dict[str, str]:
    """Turn repeated ``SESSION=RUN`` options into the mapping the graph needs."""
    mapping: dict[str, str] = {}
    for pair in pairs:
        session, separator, run_id = pair.partition("=")
        if not separator or not session or not run_id:
            msg = f"expected SESSION=RUN, got {pair!r}"
            raise click.BadParameter(msg)
        mapping[session] = run_id
    return mapping


def resolve_receipt_path(receipt_dir: Path, fanout_id: str) -> Path:
    """Find the one receipt ``fanout_id`` names.

    Accepts the full receipt hash with or without its ``sha256:`` prefix, or
    any prefix of the hex that matches exactly one receipt. An ambiguous
    prefix is an error that lists what it matched: picking the first match
    would render a different fan-out than the operator asked for, and the
    output gives no hint that it happened.
    """
    if not receipt_dir.is_dir():
        msg = f"no sealed fan-outs: {receipt_dir} does not exist"
        raise click.ClickException(msg)

    wanted = fanout_id.removeprefix(_HASH_PREFIX)
    if not wanted:
        msg = "a fan-out id is required (the receipt hash, or a unique prefix of it)"
        raise click.ClickException(msg)

    matches = sorted(
        path for path in receipt_dir.glob("*.json") if path.stem.removeprefix(_HASH_PREFIX).startswith(wanted)
    )
    if not matches:
        msg = f"no sealed fan-out matches {fanout_id!r} under {receipt_dir}"
        raise click.ClickException(msg)
    if len(matches) > 1:
        listed = ", ".join(path.stem for path in matches)
        msg = f"{fanout_id!r} matches {len(matches)} fan-outs: {listed}"
        raise click.ClickException(msg)
    return matches[0]


def _branch_rows(graph: RunGraph, lineage_root: Path, hmac_key: bytes) -> list[tuple[str, str, str]]:
    """One ``(session_id, state, detail)`` row per branch, in graph order.

    ``state`` is one of ``ok``, ``FAILED`` or ``unresolved``. The walk is the
    point: a node hash carries the spine's *stored* head, and editing the
    journal rows does not rewrite that head, so a tampered branch still hashes
    the same. Only walking the chain sees it.
    """
    from bernstein.core.lineage.run_graph import RunGraphNodeStatus
    from bernstein.core.lineage.spine import LineageSpine

    rows: list[tuple[str, str, str]] = []
    for node in graph.nodes:
        head = "no head sha" if node.head_sha is None else node.head_sha[:12]
        if node.status is RunGraphNodeStatus.UNRESOLVED or node.run_id is None:
            rows.append((node.session_id, "unresolved", f"{head} - no run id supplied for this branch"))
            continue
        spine = LineageSpine(lineage_root, run_id=node.run_id, hmac_key=hmac_key)
        verified = spine.verify()
        state = "ok" if verified.ok else "FAILED"
        detail = f"{head} - run {node.run_id}"
        if not verified.ok:
            detail = f"{detail} - spine no longer verifies"
        rows.append((node.session_id, state, detail))
    return rows


_STATE_STYLE = {"ok": "green", "FAILED": "red", "unresolved": "yellow"}


def _render(receipt_path: Path, receipt_body: Mapping[str, object], rows: list[tuple[str, str, str]]) -> None:
    tree = Tree(f"[bold]{receipt_path.stem}[/bold]")
    tree.add(f"graph root  {receipt_body.get('graph_root_hash', '?')}")
    tree.add(f"anchored at {receipt_body.get('journal_entry_hash') or '[red]nothing[/red]'}")
    branches = tree.add(f"branches ({len(rows)} on the tree, {len(receipt_body.get('node_hashes', []) or [])} sealed)")
    for session_id, state, detail in rows:
        style = _STATE_STYLE[state]
        branches.add(f"[{style}]{state:<10}[/{style}] {session_id}  {detail}")
    console.print(tree)


@click.command("graph")
@click.argument("fanout_id")
@click.option(
    "--run-id",
    "run_id_pairs",
    multiple=True,
    metavar="SESSION=RUN",
    help="Pair a branch's session id with the run whose spine recorded it. Repeatable.",
)
@click.option("--verify", "do_verify", is_flag=True, help="Re-derive the whole receipt and report the verdict.")
@click.option("--json", "as_json", is_flag=True, help="Emit the signed receipt verbatim and nothing else.")
@click.option(
    "--public-key",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="PEM public key the receipt was signed with. Required by --verify.",
)
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Project root holding .sdd (default: the current directory).",
)
def run_graph_cmd(
    fanout_id: str,
    run_id_pairs: tuple[str, ...],
    do_verify: bool,
    as_json: bool,
    public_key: Path | None,
    workdir: Path | None,
) -> None:
    """Render one fan-out's sealed run graph, branch by branch.

    \b
      bernstein worktrees graph <fanout-id>
      bernstein worktrees graph <fanout-id> --run-id sess-a=run-a --verify --public-key key.pem
      bernstein worktrees graph <fanout-id> --json

    Exits non-zero when a branch's spine no longer verifies, or when --verify
    refuses the receipt. A branch with no --run-id is reported as unresolved,
    not as failing: nothing was checked, so nothing failed.
    """
    from bernstein.core.lineage.run_graph import build_run_graph, verify_run_graph_receipt

    root = Path.cwd() if workdir is None else workdir
    receipt_path = resolve_receipt_path(root / RECEIPT_RELDIR, fanout_id)

    if as_json:
        # Verbatim: this file is the signed artifact, and re-encoding it would
        # change the bytes the signature covers.
        click.echo(receipt_path.read_text(encoding="utf-8").strip())
        return

    try:
        receipt_body = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"{receipt_path.name} could not be read as a receipt: {exc}"
        raise click.ClickException(msg) from exc

    run_ids = parse_run_ids(run_id_pairs)
    hmac_key = _load_hmac_key()
    lineage_root = root / ".sdd" / "lineage"
    graph = build_run_graph(root, run_ids=run_ids, lineage_root=lineage_root, hmac_key=hmac_key)
    rows = _branch_rows(graph, lineage_root, hmac_key)
    _render(receipt_path, receipt_body, rows)

    failed = [session_id for session_id, state, _ in rows if state == "FAILED"]
    if failed:
        console.print(f"[red]{len(failed)} branch(es) failed:[/red] {', '.join(failed)}")

    if not do_verify:
        if failed:
            raise SystemExit(1)
        return

    if public_key is None:
        # Refusing beats verifying with no key and reporting the refusal as a
        # bad signature, which reads as tampering.
        msg = "--verify needs --public-key: the receipt's signature cannot be checked without it"
        raise click.ClickException(msg)

    result = verify_run_graph_receipt(
        receipt_path=receipt_path,
        repo_root=root,
        run_ids=run_ids,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        public_key_pem=public_key.read_bytes(),
    )
    style = "green" if result.ok else "red"
    console.print(f"[{style}]{result.status}[/{style}]: {result.reason}")
    if not result.ok or failed:
        raise SystemExit(1)
