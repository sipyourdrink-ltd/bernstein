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


def _load_hmac_key(key_path: Path | None = None) -> bytes:
    """Load the audit HMAC key read-only for a verify pass.

    ``verify`` only reads the chain, so it must never mint key material.
    ``load_or_create_audit_key`` would generate a fresh key on a machine that
    has none -- and that key cannot authenticate a chain written under the real
    key, so every HMAC tag would fail and a plain missing-key setup error would
    be misreported as tamper (issue #2639). Load read-only and fail closed with
    a clear "key missing" error and a distinct exit code instead.
    """
    from bernstein.core.security.audit import AuditKeyMissingError, load_audit_key

    try:
        return load_audit_key(key_path)
    except AuditKeyMissingError as exc:
        console.print()
        console.print(f"[yellow]CANNOT VERIFY[/yellow] -- {exc}")
        raise SystemExit(3) from exc


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
@click.option(
    "--receipt-hash",
    "receipt_hash",
    default=None,
    help="Recovery receipt spine entry hash to resolve against the run's spine (issue #2557).",
)
@click.option(
    "--receipt-file",
    "receipt_file",
    type=click.Path(dir_okay=False, exists=True),
    default=None,
    help="Recovery receipt artifact JSON; content-addresses it against the anchored entry.",
)
@click.option(
    "--key-path",
    "key_path",
    type=click.Path(dir_okay=False, exists=True),
    default=None,
    help="Audit HMAC key file the chain was written under (read-only; never mints a key). "
    "Defaults to $BERNSTEIN_AUDIT_KEY_PATH or the XDG state key.",
)
def lineage_verify_cmd(
    run_id: str,
    workdir: str,
    public_key_path: str | None,
    receipt_hash: str | None,
    receipt_file: str | None,
    key_path: str | None,
) -> None:
    """Verify the lineage spine for *run_id*.

    With ``--receipt-hash`` (optionally ``--receipt-file``) it instead confirms
    a recovery task's embedded failure-receipt hash resolves to a valid,
    Merkle-chained, HMAC-tagged spine entry.

    Exit codes: 0 = OK, 1 = no entries / seal-only (no artifact provenance) /
    bad input, 2 = tamper detected, 3 = cannot verify (audit key missing).
    """
    key_path_resolved = Path(key_path) if key_path is not None else None
    lineage_root = _spine_dir(workdir)
    spine_path = lineage_root / run_id / "spine.jsonl"

    if receipt_hash is not None:
        _verify_receipt(run_id, lineage_root, spine_path, receipt_hash, receipt_file, key_path_resolved)

    if spine_path.exists():
        spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=_load_hmac_key(key_path_resolved))
        result = spine.verify()
        console.print()
        console.print(f"[bold]Lineage spine[/bold] run={run_id} entries={result.count} head={spine.head_hash()[:16]}")
        if result.status is SpineStatus.OK:
            console.print("[green]OK[/green] -- chain intact, all HMAC tags valid.")
            raise SystemExit(0)
        if result.status is SpineStatus.NO_ENTRIES:
            console.print("[yellow]NO ENTRIES[/yellow] -- run emitted no lineage.")
            raise SystemExit(1)
        if result.status is SpineStatus.SEAL_ONLY:
            console.print(
                "[yellow]SEAL ONLY[/yellow] -- chain intact but records only the journal-head "
                "seal; no produced-artifact provenance was captured for this run."
            )
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


def _verify_receipt(
    run_id: str,
    lineage_root: Path,
    spine_path: Path,
    receipt_hash: str,
    receipt_file: str | None,
    key_path: Path | None = None,
) -> None:
    """Resolve a recovery receipt hash against the run's spine (issue #2557).

    Exit codes: 0 = receipt resolves on an intact chain, 2 = it does not,
    3 = cannot verify (audit key missing).
    """
    from bernstein.core.planning.recovery_receipt import resolve_receipt_on_spine

    if not spine_path.exists():
        console.print()
        console.print(f"[red]No spine for run[/red] {run_id} -- cannot resolve receipt {receipt_hash}.")
        raise SystemExit(2)

    spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=_load_hmac_key(key_path))
    content = Path(receipt_file).read_bytes() if receipt_file is not None else None
    resolution = resolve_receipt_on_spine(spine, entry_hash=receipt_hash, receipt_content=content)

    console.print()
    console.print(f"[bold]Recovery receipt[/bold] run={run_id} entry={receipt_hash[:16]}")
    if resolution.ok:
        detail = "content matches anchored entry" if content is not None else "chain-anchored"
        console.print(f"[green]OK[/green] -- receipt resolves to a valid spine entry ({detail}).")
        raise SystemExit(0)
    console.print(f"[red]RECEIPT VERIFICATION FAILED[/red] -- {len(resolution.errors)} error(s):")
    for err in resolution.errors[:50]:
        console.print(f"  - {err}")
    raise SystemExit(2)


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
