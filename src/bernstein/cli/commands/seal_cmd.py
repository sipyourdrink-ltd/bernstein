"""``bernstein seal`` - anchor a run's sealed head outside this install (#4205).

``bernstein replay <run> --verify`` already recomputes a run's Merkle head and
checks it against the seal the run wrote into its lineage spine. That answers
"do these artifacts still hash to what we sealed?" to whoever holds the audit
key. It cannot answer the two questions a reviewer asks next:

* *When* did this head exist? Timing is excluded from the chain by design.
* Would a rewrite even be visible? A key holder can edit the journal and
  re-seal it, and every internal check passes on the rewrite.

``seal publish`` submits the head digest to an RFC 3161 timestamping authority
and stores the reply beside the journal. ``seal verify`` re-checks that reply
offline, against TSA roots the operator pinned - never by calling the TSA back.

Both are opt-in. ``publish`` reaches the network only when the operator names
``--tsa-url``; with ``--token`` it reads a reply obtained elsewhere (an
air-gapped operator runs ``openssl ts -query -digest <head> -sha256 -cert``
on a connected host and carries the ``.tsr`` back). ``verify`` never opens a
socket at all.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import click
from rich.console import Console

from bernstein.core.replay.journal import (
    read_sealed_journal_head,
    run_journal_path,
    verify_journal,
)
from bernstein.core.security.path_containment import PathContainmentError, contained_path
from bernstein.core.security.seal_anchor import (
    ANCHOR_FILENAME,
    AnchorStatus,
    SealAnchorError,
    build_rfc3161_anchor,
    build_timestamp_request,
    load_anchor,
    verify_anchor,
    write_anchor,
)

console = Console()

#: Exit code for every verdict that is not a clean pass. One code keeps the
#: shell contract simple: zero means the anchor verified, anything else means
#: an operator has to look.
_FAILURE_EXIT = 1


@click.group(name="seal")
def seal_group() -> None:
    """Anchor a run's sealed head to an external timestamping authority."""


def _journal_path(sdd_dir: str, run_id: str) -> Path:
    """Return the run's journal path, resolved through the containment barrier.

    The run id reaches here straight from argv, and validating it as a safe
    segment is only half the check: an ordinary run directory can hold a
    ``journal.jsonl`` that is itself a symlink out of the runs root, so the
    whole path is resolved rather than joined onto a checked directory.
    Anchoring is where that half matters most - a redirected read would mint
    an external timestamp over a head this install never produced.
    """
    try:
        return run_journal_path(Path(sdd_dir), run_id)
    except PathContainmentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(_FAILURE_EXIT) from exc


def _anchor_path(sdd_dir: str, run_id: str) -> Path:
    """Return the run's anchor path, resolved through the same barrier.

    The anchor is the artifact ``seal verify`` trusts, so a symlinked
    ``seal_anchor.json`` would let a reply stored outside the runs root be
    read as this run's - and let ``publish`` write one there.
    """
    try:
        return contained_path(Path(sdd_dir) / "runs", run_id, ANCHOR_FILENAME, label="run id")
    except PathContainmentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(_FAILURE_EXIT) from exc


def _recomputed_head(run_id: str, sdd_dir: str) -> str:
    """Recompute the run's journal head, refusing anything not anchorable.

    A head is anchorable only when the journal chain is intact, every physical
    line reached the verifier, and - when the run recorded a seal - the
    recomputed head is the sealed one. Anchoring a head that already disagrees
    with the run's own seal would mint an external proof for a rewrite.
    """
    journal_path = _journal_path(sdd_dir, run_id)
    if not journal_path.is_file():
        console.print(f"[red]No journal for {run_id}:[/red] {journal_path}")
        raise SystemExit(_FAILURE_EXIT)

    result = verify_journal(journal_path)
    if not result.chain_consistent or result.discarded_line_indices or not result.head:
        console.print(f"[red]Refusing to anchor {run_id}: its journal chain does not verify.[/red]")
        console.print("Run [bold]bernstein replay <run> --verify[/bold] for the divergent step.")
        raise SystemExit(_FAILURE_EXIT)

    sealed = read_sealed_journal_head(run_id=run_id, sdd_dir=sdd_dir)
    if sealed is not None and sealed != result.head:
        console.print(
            f"[red]Refusing to anchor {run_id}: the journal recomputes to {result.head} "
            f"but the run sealed {sealed}.[/red]"
        )
        raise SystemExit(_FAILURE_EXIT)
    if sealed is None:
        console.print("[yellow]Note:[/yellow] this run carries no spine seal; anchoring the recomputed head.")
    return result.head


def _obtain_token(*, head: str, token: str | None, tsa_url: str | None, timeout: float) -> tuple[bytes, str]:
    """Return the DER timestamp reply plus the URL to record for it."""
    if token:
        token_path = Path(token).expanduser()
        if not token_path.is_file():
            console.print(f"[red]--token file not found: {token_path}[/red]")
            raise SystemExit(_FAILURE_EXIT)
        return token_path.read_bytes(), tsa_url or ""

    if not tsa_url:
        console.print(
            "[red]Nothing to anchor with.[/red] Pass [bold]--tsa-url[/bold] to request a "
            "timestamp over the head, or [bold]--token[/bold] to store a reply you obtained "
            "elsewhere. Neither is a default: this command makes no network call on its own."
        )
        raise SystemExit(_FAILURE_EXIT)

    # Resolved through the module so the network call has exactly one
    # binding site, and a test that forbids it can prove it never ran.
    from bernstein.core.security import seal_anchor

    request = build_timestamp_request(head, nonce=secrets.randbits(64))
    return seal_anchor.request_timestamp_token(tsa_url, request, timeout=timeout), tsa_url


@seal_group.command("publish")
@click.argument("run_id")
@click.option("--sdd-dir", "sdd_dir", default=".sdd", show_default=True, help="Path to the .sdd directory.")
@click.option(
    "--tsa-url",
    "tsa_url",
    default=None,
    help="RFC 3161 timestamping authority to ask for a token. The only option that opens a socket.",
)
@click.option(
    "--token",
    "token",
    default=None,
    type=click.Path(dir_okay=False),
    help="DER TimeStampResp/TimeStampToken obtained elsewhere, for hosts with no network.",
)
@click.option("--timeout", "timeout", default=30.0, show_default=True, help="TSA request timeout, seconds.")
@click.option("--json", "as_json", is_flag=True, help="Emit the stored anchor record as JSON.")
def seal_publish(
    run_id: str,
    sdd_dir: str,
    tsa_url: str | None,
    token: str | None,
    timeout: float,
    as_json: bool,
) -> None:
    """Anchor RUN_ID's sealed journal head to a timestamping authority.

    The TSA is shown only the head digest, never the journal. The reply is
    stored as seal_anchor.json next to the journal it witnesses.

      bernstein seal publish latest-run --tsa-url https://freetsa.org/tsr
      bernstein seal publish latest-run --token ./reply.tsr
    """
    head = _recomputed_head(run_id, sdd_dir)
    token_der, recorded_url = _obtain_token(head=head, token=token, tsa_url=tsa_url, timeout=timeout)

    try:
        anchor = build_rfc3161_anchor(
            run_id=run_id,
            head_sha256=head,
            token_der=token_der,
            tsa_url=recorded_url,
        )
    except SealAnchorError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(_FAILURE_EXIT) from exc

    anchor_path = _anchor_path(sdd_dir, run_id)
    write_anchor(anchor_path, anchor)

    if as_json:
        console.print_json(json.dumps(anchor.to_record()))
        return
    console.print(f"[green]ANCHORED[/green] {run_id} head [bold]{head}[/bold]")
    console.print(f"stored: {anchor_path}")
    console.print(
        f"Confirm it with: [bold]bernstein seal verify {run_id} --rfc3161-trusted-tsa-bundle <roots.pem>[/bold]"
    )


@seal_group.command("verify")
@click.argument("run_id")
@click.option("--sdd-dir", "sdd_dir", default=".sdd", show_default=True, help="Path to the .sdd directory.")
@click.option(
    "--rfc3161-trusted-tsa-bundle",
    "trust_bundle",
    default=None,
    type=click.Path(dir_okay=False),
    help="PEM/DER bundle of TSA roots you accept. Without it nothing is checked.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the verdict as JSON.")
def seal_verify(run_id: str, sdd_dir: str, trust_bundle: str | None, as_json: bool) -> None:
    """Check RUN_ID's stored anchor against the artifacts on disk, offline.

    Exits zero only on a verified anchor. A head that moved since the anchor
    was issued reports ``mismatched``; a missing trust bundle reports
    ``unverifiable`` rather than a pass.
    """
    from bernstein.core.security.rfc3161_verifier import load_trusted_tsa_certs

    anchor_path = _anchor_path(sdd_dir, run_id)
    if not anchor_path.is_file():
        console.print(f"[red]No anchor for {run_id}[/red] ({anchor_path} does not exist).")
        console.print("Create one with [bold]bernstein seal publish[/bold].")
        raise SystemExit(_FAILURE_EXIT)

    journal_path = _journal_path(sdd_dir, run_id)
    head = verify_journal(journal_path).head

    try:
        anchor = load_anchor(anchor_path)
    except SealAnchorError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(_FAILURE_EXIT) from exc

    trusted = []
    if trust_bundle:
        try:
            trusted = load_trusted_tsa_certs(Path(trust_bundle))
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(_FAILURE_EXIT) from exc

    result = verify_anchor(anchor, sealed_head=head, trusted_tsa_certs=trusted)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": result.status.value,
                    "head_sha256": head,
                    "anchored_head_sha256": anchor.head_sha256,
                    "gen_time": result.gen_time.isoformat() if result.gen_time else None,
                    "tsa_subject": result.tsa_subject,
                    "tsa_url": anchor.tsa_url,
                    "errors": result.errors,
                }
            )
        )
    else:
        colour = "green" if result.status is AnchorStatus.VERIFIED else "red"
        console.print(f"[{colour}]{result.status.value.upper()}[/{colour}] anchor for [bold]{run_id}[/bold]")
        if result.gen_time is not None:
            console.print(f"witnessed at {result.gen_time.isoformat()} by {result.tsa_subject}")
        for error in result.errors:
            console.print(f"  {error}")

    if result.status is not AnchorStatus.VERIFIED:
        raise SystemExit(_FAILURE_EXIT)


__all__ = ["seal_group"]
