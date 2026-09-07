"""``bernstein identity review`` - chain-derived per-principal access review.

Verbs:

* ``bernstein identity review --since <date>`` - derive the review for a window
  from the delegation receipts and the grant chain, sign it, and print (or
  write) the envelope. ``--format csv`` renders the same rows for a
  spreadsheet; the signed artefact is always the JSON envelope.
* ``bernstein identity review sign-off --document <file> --reviewer <who>`` -
  append the reviewer's decision as an HMAC-chained event naming the digest of
  the reviewed bytes.
* ``bernstein identity review verify --document <file>`` - recompute that digest
  and report which sign-off, if any, covers exactly these bytes.

Every row is derived in :mod:`bernstein.core.identity.access_review`; nothing is
computed here and no verdict is read from a field. A review records a human
decision - these verbs never make one.

Exit codes: ``0`` verified / emitted, ``1`` verification or read failure,
``2`` invalid arguments.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.identity import access_review

EXIT_FAILURE = 1
EXIT_USAGE = 2

_ROOT_OPTION = click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=None,
    help="Audit root holding delegation/ and grants/ (default: .sdd/audit).",
)


def _resolve_root(root: Path | None) -> Path:
    return Path(root) if root is not None else access_review.DEFAULT_ROOT


def _bound(value: str | None, label: str) -> int | None:
    if value is None:
        return None
    try:
        return access_review.parse_timestamp(value)
    except access_review.AccessReviewError as exc:
        console.print(f"[red]Invalid --{label}:[/red] {exc}")
        raise SystemExit(EXIT_USAGE) from None


def _read_envelope(document: str) -> bytes:
    try:
        return Path(document).read_bytes()
    except OSError as exc:
        console.print(f"[red]Failed to read the review document:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None


@click.group("review", invoke_without_command=True, no_args_is_help=False)
@click.option("--since", "since", default=None, help="Inclusive window start (ISO date/datetime or epoch).")
@click.option(
    "--until",
    "until",
    default=None,
    help="Exclusive window end (ISO date/datetime or epoch). Open by default so two builds agree.",
)
@_ROOT_OPTION
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "csv"]),
    default="json",
    show_default=True,
    help="json emits the signed envelope; csv renders the same rows unsigned.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write to this file instead of stdout.",
)
@click.pass_context
def review_group(
    ctx: click.Context,
    since: str | None,
    until: str | None,
    root: Path | None,
    output_format: str,
    output: Path | None,
) -> None:
    """Derive a signed per-principal access review from the identity chains.

    \b
      bernstein identity review --since 2026-01-01
      bernstein identity review --since 2026-01-01 --until 2026-04-01 \\
          --output review.json
      bernstein identity review sign-off --document review.json \\
          --reviewer alex@example.com
      bernstein identity review verify --document review.json

    \b
    Every row names the principal whose access it describes, the principal that
    authorized it, and the chain event that records it. A run whose chain does
    not verify contributes no rows and is named under ``unverified`` instead.
    """
    if ctx.invoked_subcommand is not None:
        return

    try:
        review = access_review.build_default_review(
            root=_resolve_root(root),
            since=_bound(since, "since"),
            until=_bound(until, "until"),
        )
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Failed to derive the review:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None

    if output_format == "csv":
        from bernstein.core.compliance.regulator_renderers import render_access_review_csv

        payload = render_access_review_csv(review.document).encode()
    else:
        payload = review.envelope_bytes()

    if output is None:
        click.echo(payload.decode(), nl=False)
        return
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
    except OSError as exc:
        console.print(f"[red]Failed to write the review:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None
    console.print(f"[green]OK[/green] review written to {output}", soft_wrap=True)
    console.print(f"  rows    {review.document['row_count']}")
    console.print(f"  digest  {review.digest}", soft_wrap=True)


@review_group.command("sign-off")
@click.option(
    "--document",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="The signed review envelope the reviewer read.",
)
@click.option("--reviewer", required=True, help="Who reviewed. Recorded verbatim in the chain event.")
@click.option(
    "--decision",
    type=click.Choice(list(access_review.DECISIONS)),
    default="approved",
    show_default=True,
    help="The human decision this sign-off records.",
)
@click.option("--note", default="", help="Free-text context recorded alongside the decision.")
@_ROOT_OPTION
def signoff_cmd(document: str, reviewer: str, decision: str, note: str, root: Path | None) -> None:
    """Append the reviewer's decision over this document as a chain event.

    The document is verified before anything is appended: a review whose
    signature or digest does not check out is refused, so no sign-off can name
    bytes that were already broken.
    """
    try:
        review = access_review.AccessReview.from_envelope(_read_envelope(document))
    except access_review.AccessReviewError as exc:
        console.print(f"[red]Refusing to sign off on an unverified review:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None

    try:
        record = access_review.record_default_signoff(
            root=_resolve_root(root),
            reviewer=reviewer,
            review=review,
            decision=decision,
            note=note,
        )
    except access_review.AccessReviewError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(EXIT_USAGE) from None
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Failed to append the sign-off:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None

    console.print(f"[green]OK[/green] sign-off recorded by {record.reviewer}", soft_wrap=True)
    console.print(f"  decision      {record.decision}")
    console.print(f"  review digest {record.review_digest}", soft_wrap=True)
    console.print(f"  chain event   {record.hmac}", soft_wrap=True)


@review_group.command("verify")
@click.option(
    "--document",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="The review envelope whose sign-off is being checked.",
)
@click.option("--reviewer", default=None, help="Require the sign-off to name this reviewer.")
@_ROOT_OPTION
def verify_cmd(document: str, reviewer: str | None, root: Path | None) -> None:
    """Report which chain-anchored sign-off covers exactly these bytes.

    The digest is recomputed from the file, never read from it, so a document
    edited after its sign-off fails here instead of inheriting the approval.
    """
    try:
        verdict = access_review.verify_default_signoff(
            root=_resolve_root(root),
            envelope_bytes=_read_envelope(document),
            reviewer=reviewer,
        )
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Failed to load the audit key:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None
    if not verdict.ok or verdict.record is None:
        console.print(f"[red]NOT VERIFIED[/red] {document}")
        for error in verdict.errors:
            console.print(f"  [red]![/red] {error}")
        raise SystemExit(EXIT_FAILURE)

    console.print(f"[green]OK[/green] signed off by {verdict.record.reviewer}", soft_wrap=True)
    console.print(f"  decision     {verdict.record.decision}")
    console.print(f"  rows         {verdict.record.row_count}")
    console.print(f"  digest       {verdict.digest}", soft_wrap=True)
    console.print(f"  chain event  {verdict.record.hmac}", soft_wrap=True)
