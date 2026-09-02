"""bernstein-verify-envelope CLI entry point.

Usage::

    bernstein-verify-envelope verify <envelope_path> \\
        [--jwk JWK_PATH | --public-key PEM_PATH] [--verbose]

Output convention (matching ``bernstein-verify-receipt``):
  - Human summary on stdout (one PASS/FAIL line per check, then OVERALL).
  - Structured JSON on stderr for machine consumers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from bernstein_verify_envelope.verify import (
    CONFLICTING_PINS,
    TRUST_EXPLANATIONS,
    VerifyResult,
    load_pinned_jwk,
    run_verify,
)


def _emit(result: VerifyResult, kind: str) -> int:
    """Write a human summary to stdout and JSON to stderr; return the exit code."""
    if result.ok:
        # The trust label, not the word "verified", is the headline: a pass
        # against the key the envelope carries proves less than a pass against
        # a pinned one, and the summary line has to say which happened.
        click.echo(f"PASS  {kind} [{result.trust}]: {result.stats}")
        click.echo(f"      {TRUST_EXPLANATIONS[result.trust]}")
    else:
        click.echo(f"FAIL  {kind} [{result.trust}]: {len(result.errors)} error(s)")
        for err in result.errors[:10]:
            click.echo(f"  - {err}")
        if len(result.errors) > 10:
            click.echo(f"  ... and {len(result.errors) - 10} more")
    click.echo(
        json.dumps(
            {
                "ok": result.ok,
                "kind": kind,
                "trust": result.trust,
                "stats": result.stats,
                "errors": result.errors,
            }
        ),
        err=True,
    )
    return 0 if result.ok else 1


@click.group()
@click.version_option(package_name="bernstein-verify-envelope")
def cli() -> None:
    """Standalone auditor CLI for Bernstein authority envelopes.

    Verifies envelopes with cryptography + click only - no bernstein package
    installation required. Works offline (air-gap).
    """


@cli.command("verify")
@click.argument("envelope_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--jwk",
    "jwk_path",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="Trusted Ed25519 JWK (OKP) to pin; an envelope signed by another key is rejected.",
)
@click.option(
    "--public-key",
    "public_key_path",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="Trusted Ed25519 public key PEM to pin; an envelope signed by another key is rejected.",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Print PASS-line details.")
def verify_cmd(
    envelope_path: Path,
    jwk_path: Path | None,
    public_key_path: Path | None,
    verbose: bool,
) -> None:
    """Verify an authority envelope using the standalone verifier.

    With no pin the envelope is checked against the key it carries and the
    result is reported as trust-on-first-use.
    """
    if jwk_path is not None and public_key_path is not None:
        click.echo(f"Conflicting pins: {CONFLICTING_PINS}", err=True)
        raise SystemExit(2)

    pinned_jwk: dict[str, Any] | None = None
    if jwk_path is not None:
        try:
            pinned_jwk = load_pinned_jwk(jwk_path)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2) from exc

    pinned_pem: bytes | None = None
    if public_key_path is not None:
        try:
            pinned_pem = public_key_path.read_bytes()
        except OSError as exc:
            click.echo(f"cannot read --public-key: {exc}", err=True)
            raise SystemExit(2) from exc

    result = run_verify(
        envelope_path=envelope_path,
        verbose=verbose,
        stream=sys.stdout,
        pinned_jwk=pinned_jwk,
        pinned_pem=pinned_pem,
    )
    sys.exit(_emit(result, kind="verify"))


if __name__ == "__main__":
    cli()
