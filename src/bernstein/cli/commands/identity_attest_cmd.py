"""``bernstein identity attest`` - run attestation receipt projection.

Subcommands:

* ``bernstein identity attest verify --run <id>`` - rebuild the run's
  attestation receipt from the audit chain, recompute the retained event
  range, verify the projection, and only then promote the receipt into the
  evidence directory.
* ``bernstein identity attest show --run <id>`` - project the same receipt
  without writing it, and print the two verdicts.

The projection lives in ``bernstein.core.security.run_attestation_receipt``.
Both verbs are a surface over it: no verdict is computed here, and no verdict
is read from a field. ``verify`` exits non-zero and names the failing entry
when the recomputed range no longer matches the signed subject.

Exit code ``1`` means the key, source chain, projection, or verification
failed. Exit code ``2`` means the command arguments are invalid.

``identity attest`` is a separate group from ``identity verify`` on purpose.
``identity verify`` checks an install-rev fingerprint token; these verbs check
a run's attestation evidence. They share a noun and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click

from bernstein.cli.helpers import console

EXIT_FAILURE = 1
EXIT_USAGE = 2

_SIGNING_OPTIONS = (
    click.option("--run", "run_id", required=True, help="Run id to project."),
    click.option("--signing-key-path", default=None, help="Path to an Ed25519 receipt signing key."),
    click.option(
        "--signing-env-var",
        default=None,
        help="Environment variable holding the Ed25519 receipt signing key.",
    ),
    click.option("--signing-key-id", default=None, help="Key id (kid) recorded in the receipt."),
    click.option("--workdir", default=".", help="Project directory containing .sdd/."),
)


def _signing_options[CommandCallback: Callable[..., object]](func: CommandCallback) -> CommandCallback:
    """Apply the shared signing/workdir options in declaration order."""
    for option in reversed(_SIGNING_OPTIONS):
        func = option(func)
    return func


def _resolve_audit_dir(workdir: str) -> Path:
    audit_dir = Path(workdir).resolve() / ".sdd" / "audit"
    if not audit_dir.is_dir():
        console.print(f"[red]Audit directory not found:[/red] {audit_dir}")
        console.print("[dim]Run [bold]bernstein run[/bold] first to generate audit events.[/dim]")
        raise SystemExit(EXIT_FAILURE)
    return audit_dir


def _prepare_output_dir(workdir: str, output: str | None) -> Path:
    output_dir = Path(output).resolve() if output else Path(workdir).resolve() / ".sdd" / "evidence"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[red]Failed to prepare receipt output directory:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None
    return output_dir


def _resolve_kms(signing_key_path: str | None, signing_env_var: str | None, signing_key_id: str | None):  # type: ignore[no-untyped-def]
    from bernstein.core.persistence.lineage_signer import LineageSignerError
    from bernstein.core.security.lineage_kms import EnvBasedKMSAdapter, FileBasedKMSAdapter, KMSAdapter

    if signing_key_path and signing_env_var:
        console.print("[red]--signing-key-path and --signing-env-var are mutually exclusive.[/red]")
        raise SystemExit(EXIT_USAGE)

    kms_adapter: KMSAdapter
    try:
        if signing_key_path:
            kms_adapter = FileBasedKMSAdapter(Path(signing_key_path), kid=signing_key_id)
        elif signing_env_var:
            kms_adapter = EnvBasedKMSAdapter(signing_env_var, kid=signing_key_id)
        else:
            console.print("[red]Provide either --signing-key-path or --signing-env-var (Ed25519 receipt key).[/red]")
            raise SystemExit(EXIT_USAGE)
    except (LineageSignerError, OSError, ValueError) as exc:
        console.print(f"[red]Failed to load receipt signing key: {exc}[/red]")
        raise SystemExit(EXIT_FAILURE) from None
    return kms_adapter


def _build(run_id: str, workdir: str, kms_adapter: object, *, write: bool, output_dir: Path | None = None):  # type: ignore[no-untyped-def]
    from bernstein.core.security.audit import AuditKeyMissingError, AuditKeyPermissionError, load_audit_key
    from bernstein.core.security.run_attestation_receipt import (
        RunAttestationReceiptError,
        build_run_attestation_receipt,
    )

    audit_dir = _resolve_audit_dir(workdir)
    try:
        audit_key = load_audit_key()
    except (AuditKeyMissingError, AuditKeyPermissionError, OSError) as exc:
        console.print(f"[red]Failed to load audit key:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None

    try:
        return build_run_attestation_receipt(
            audit_dir,
            run_id=run_id,
            key=audit_key,
            kms_adapter=kms_adapter,  # type: ignore[arg-type]
            output_dir=output_dir,
            write=write,
        )
    except RunAttestationReceiptError as exc:
        console.print(f"[red]Run attestation projection failed:[/red] {exc}")
        raise SystemExit(EXIT_FAILURE) from None
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(EXIT_USAGE) from None


@click.group("attest")
def attest_group() -> None:
    """Project and verify a run's attestation receipt.

    \b
      bernstein identity attest show --run r-1234 --signing-key-path key.pem
      bernstein identity attest verify --run r-1234 --signing-key-path key.pem
    """


@attest_group.command("show")
@_signing_options
def show_cmd(
    run_id: str,
    signing_key_path: str | None,
    signing_env_var: str | None,
    signing_key_id: str | None,
    workdir: str,
) -> None:
    """Project without writing; exit 1 on key or projection failure."""
    kms_adapter = _resolve_kms(signing_key_path, signing_env_var, signing_key_id)
    receipt = _build(run_id, workdir, kms_adapter, write=False)

    console.print(f"[bold]run[/bold]                {receipt.run_id}", soft_wrap=True)
    console.print(f"[bold]identity anchor[/bold]    {receipt.identity_anchor_hmac}", soft_wrap=True)
    console.print(f"[bold]through[/bold]            {receipt.through_hmac}", soft_wrap=True)
    console.print(f"[bold]dispatch evidence[/bold]  {receipt.dispatch_evidence_verdict.value}")
    console.print(f"[bold]whole run[/bold]          {receipt.whole_run_verdict.value}")
    console.print(f"[bold]receipt sha256[/bold]     {receipt.sha256}", soft_wrap=True)
    console.print(
        "\n[dim]Verdicts are recomputed from the retained range, never read from a field. "
        "Use [bold]identity attest verify[/bold] to re-verify and emit.[/dim]"
    )


@attest_group.command("verify")
@_signing_options
@click.option("--output", default=None, help="Directory to write the emitted receipt into.")
def verify_cmd(
    run_id: str,
    signing_key_path: str | None,
    signing_env_var: str | None,
    signing_key_id: str | None,
    workdir: str,
    output: str | None,
) -> None:
    """Rebuild, verify, and emit the run's attestation receipt.

    Exits non-zero and names the failing entry when the recomputed range no
    longer matches the signed subject. A receipt is promoted into the evidence
    directory only after semantic verification passes.
    """
    from tempfile import TemporaryDirectory

    from bernstein.core.security.run_attestation_receipt import verify_run_attestation_projection

    kms_adapter = _resolve_kms(signing_key_path, signing_env_var, signing_key_id)
    output_dir = _prepare_output_dir(workdir, output)
    with TemporaryDirectory(prefix=".attest-", dir=output_dir) as staging_dir:
        receipt = _build(run_id, workdir, kms_adapter, write=True, output_dir=Path(staging_dir))

        verification = verify_run_attestation_projection(receipt.receipt)
        if not verification.ok:
            console.print(f"[red]Run attestation projection failed for[/red] {run_id}")
            for error in verification.errors:
                console.print(f"  [red]![/red] {error}")
            raise SystemExit(EXIT_FAILURE)

        if receipt.receipt_path is None:  # pragma: no cover - builder contract guard
            console.print("[red]Run attestation projection produced no staged receipt.[/red]")
            raise SystemExit(EXIT_FAILURE)
        receipt_path = output_dir / receipt.receipt_path.name
        try:
            receipt.receipt_path.replace(receipt_path)
        except OSError as exc:
            console.print(f"[red]Failed to promote verified receipt:[/red] {exc}")
            raise SystemExit(EXIT_FAILURE) from None

    console.print(
        f"[green]OK[/green] run attestation projection verified for {verification.run_id}",
        soft_wrap=True,
    )
    console.print(f"  dispatch evidence  {verification.dispatch_evidence_verdict.value}")
    console.print(f"  whole run          {verification.whole_run_verdict.value}")
    console.print(f"  receipt            {receipt_path}", soft_wrap=True)
    console.print(f"  sha256             {receipt.sha256}", soft_wrap=True)
