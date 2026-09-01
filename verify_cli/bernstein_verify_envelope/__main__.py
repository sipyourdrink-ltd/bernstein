"""bernstein-verify-envelope CLI entry point.

Usage::

    bernstein-verify-envelope verify <envelope_path> [--verbose]

Output convention (matching ``bernstein-verify-receipt``):
  - Human summary on stdout (one PASS/FAIL line per check, then OVERALL).
  - Structured JSON on stderr for machine consumers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from bernstein_verify_envelope.verify import VerifyResult, run_verify


def _emit(result: VerifyResult, kind: str) -> int:
    """Write a human summary to stdout and JSON to stderr; return the exit code."""
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
@click.version_option(package_name="bernstein-verify-envelope")
def cli() -> None:
    """Standalone auditor CLI for Bernstein authority envelopes.

    Verifies envelopes with cryptography + click only - no bernstein package
    installation required. Works offline (air-gap).
    """


@cli.command("verify")
@click.argument("envelope_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--verbose", "-v", is_flag=True, default=False, help="Print PASS-line details.")
def verify_cmd(envelope_path: Path, verbose: bool) -> None:
    """Verify an authority envelope using the standalone verifier."""
    result = run_verify(envelope_path=envelope_path, verbose=verbose, stream=sys.stdout)
    sys.exit(_emit(result, kind="verify"))


if __name__ == "__main__":
    cli()
