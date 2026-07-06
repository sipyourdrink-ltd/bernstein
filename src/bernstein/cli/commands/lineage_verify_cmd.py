"""``bernstein lineage verify <run_id>`` -- spine chain verification.

Walks the run's lineage spine, recomputes the full Merkle hash chain and
every HMAC tag, and prints the head hash -- the run's artifact-provenance
identity. Exits 0 only when the chain is intact and non-empty.

An empty run reports a distinct ``no entries`` status and a non-zero
exit rather than passing trivially, so a run that emitted nothing cannot
be mistaken for a verified one (issue #2292).

When no spine exists for the run, falls back to the legacy persistence
lineage chain so pre-spine evidence packages still verify.

Designed for compliance-team ad-hoc use and CI: an auditor runs this
against a sealed evidence package to confirm nothing has been edited
between artefact handover and review.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.lineage.spine import LineageSpine, SpineStatus


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _spine_dir(workdir: str) -> Path:
    return Path(workdir).resolve() / ".sdd" / "lineage"


@click.command(name="verify")
@click.argument("run_id", required=True)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--public-key",
    "public_key_path",
    type=click.Path(dir_okay=False, exists=True),
    default=None,
    help="Customer Ed25519 public key (legacy chain only; re-verifies every customer_signature).",
)
def lineage_verify_cmd(run_id: str, workdir: str, public_key_path: str | None) -> None:
    """Verify the lineage spine for *run_id*.

    Exit codes: 0 = OK, 1 = no entries / bad input, 2 = tamper detected.
    """
    lineage_root = _spine_dir(workdir)
    spine_path = lineage_root / run_id / "spine.jsonl"

    if spine_path.exists():
        spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=_load_hmac_key())
        result = spine.verify()
        console.print()
        console.print(f"[bold]Lineage spine[/bold] run={run_id} entries={result.count} head={spine.head_hash()[:16]}")
        if result.status is SpineStatus.OK:
            console.print("[green]OK[/green] -- chain intact, all HMAC tags valid.")
            raise SystemExit(0)
        if result.status is SpineStatus.NO_ENTRIES:
            console.print("[yellow]NO ENTRIES[/yellow] -- run emitted no lineage.")
            raise SystemExit(1)
        console.print(f"[red]TAMPER DETECTED[/red] -- {len(result.errors)} error(s):")
        for err in result.errors[:50]:
            console.print(f"  - {err}")
        if len(result.errors) > 50:
            console.print(f"  ... ({len(result.errors) - 50} more)")
        raise SystemExit(2)

    # No spine for this run: distinct "no entries" unless a legacy chain exists.
    if not lineage_root.parent.is_dir():
        console.print(f"[red]No .sdd directory at[/red] {lineage_root.parent}")
        raise SystemExit(1)

    _verify_legacy(run_id, workdir, public_key_path, lineage_root)


def _verify_legacy(run_id: str, workdir: str, public_key_path: str | None, lineage_root: Path) -> None:
    from bernstein.core.persistence.lineage import verify_run_chain
    from bernstein.core.persistence.lineage_signer import (
        Ed25519PublicKeyVerifier,
        LineageSignerError,
    )

    sdd_dir = Path(workdir).resolve() / ".sdd"
    verifier = None
    if public_key_path is not None:
        try:
            verifier = Ed25519PublicKeyVerifier.from_path(Path(public_key_path))
        except LineageSignerError as exc:
            console.print(f"[red]Bad public key:[/red] {exc}")
            raise SystemExit(1) from exc

    result = verify_run_chain(sdd_dir, run_id, verifier=verifier)
    console.print()
    console.print(f"[bold]Lineage verification (legacy)[/bold] run={run_id} records={result.record_count}")
    if result.record_count == 0:
        console.print("[yellow]NO ENTRIES[/yellow] -- no lineage found for this run.")
        raise SystemExit(1)
    if result.ok:
        console.print("[green]OK[/green] -- chain intact, all signatures valid.")
        raise SystemExit(0)
    console.print(f"[red]TAMPER DETECTED[/red] -- {len(result.errors)} error(s):")
    for err in result.errors[:50]:
        console.print(f"  - {err}")
    if len(result.errors) > 50:
        console.print(f"  ... ({len(result.errors) - 50} more)")
    raise SystemExit(2)
