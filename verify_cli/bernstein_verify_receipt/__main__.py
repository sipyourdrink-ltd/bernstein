"""bernstein-verify-receipt CLI entry point.

Usage::

    bernstein-verify-receipt verify <receipt_path> \\
        [--jwk JWK_PATH] [--public-key PEM_PATH] \\
        [--format cose|intoto|transparency|all]
        [--verbose]

Output convention (per ADR-009 §9.3):
  - Human summary on stdout (one-line PASS/FAIL + brief reasons).
  - Structured JSON on stderr for machine consumers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from bernstein_verify_receipt.verify import VerifyResult, run_verify


def _emit(result: VerifyResult, kind: str) -> int:
    """Write human summary to stdout + JSON to stderr; return exit code."""
    if result.ok:
        click.echo(f"PASS  {kind}: {result.stats}")
    else:
        click.echo(f"FAIL  {kind}: {len(result.errors)} error(s)")
        for err in result.errors[:10]:
            click.echo(f"  - {err}")
        if len(result.errors) > 10:
            click.echo(f"  ... and {len(result.errors) - 10} more")
    click.echo(
        json.dumps({"ok": result.ok, "kind": kind, "stats": result.stats, "errors": result.errors}),
        err=True,
    )
    return 0 if result.ok else 1


@click.group()
@click.version_option(package_name="bernstein-verify-receipt")
def cli() -> None:
    """Standalone auditor CLI for Bernstein audit receipts.

    Verifies receipts with cryptography + cbor2 + click only - no bernstein
    package installation required. Works offline (air-gap).
    """


@cli.command("verify")
@click.argument("receipt_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--jwk",
    "jwk_path",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="Optional trusted Ed25519 JWK (OKP) to pin; embedded key must match.",
)
@click.option(
    "--public-key",
    "public_key_path",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="Optional trusted Ed25519 public key PEM to pin; embedded key must match.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["cose", "intoto", "transparency", "all"]),
    default="all",
    show_default=True,
    help="Which format(s) to verify.",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Print PASS-line details.")
def verify_cmd(
    receipt_path: Path,
    jwk_path: Path | None,
    public_key_path: Path | None,
    fmt: str,
    verbose: bool,
) -> None:
    """Verify an audit receipt using the standalone verifier."""
    pinned_jwk: dict[str, Any] | None = None
    if jwk_path is not None:
        try:
            pinned_jwk = json.loads(jwk_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            click.echo(f"[red]Cannot read --jwk:[/red] {exc}", err=True)
            raise SystemExit(2) from exc
        if not isinstance(pinned_jwk, dict):
            click.echo("[red]--jwk must be a JSON object[/red]", err=True)
            raise SystemExit(2)

    pinned_pem: bytes | None = None
    if public_key_path is not None:
        try:
            pinned_pem = public_key_path.read_bytes()
        except OSError as exc:
            click.echo(f"[red]Cannot read --public-key:[/red] {exc}", err=True)
            raise SystemExit(2) from exc

    result = run_verify(
        receipt_path=receipt_path,
        which=fmt,
        pinned_jwk=pinned_jwk,
        pinned_pem=pinned_pem,
        verbose=verbose,
        stream=sys.stdout,
    )
    sys.exit(_emit(result, kind="verify"))


if __name__ == "__main__":
    cli()
