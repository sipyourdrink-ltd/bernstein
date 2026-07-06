"""``bernstein identity`` - operator-side install-rev fingerprint commands.

Subcommands:

* ``bernstein identity show`` - print the current install's token (or the
  disabled sentinel when emission is off / kill switch is set).  Used to
  let users see exactly what string lands in their public artefacts.
* ``bernstein identity decode <token>`` - alias for ``verify``.  Confirms
  a discovered token came from a real Bernstein install.  Requires the
  operator's seed in ``BERNSTEIN_IDENTITY_SEED`` (hex-encoded 32 bytes).
* ``bernstein identity verify <token> [--nonce HEX] [--version-major N]``
  - same as ``decode`` but accepts an optional debug-bundle nonce for
  full HMAC-strength verification.
* ``bernstein identity disable`` - print the env-var line the user can
  paste into their shell to suppress all emit sites.

The CLI is read-only and never opens a network connection.  This is the
project's hard rule: no telemetry, ever.

See ``docs/operations/install-fingerprint.md`` for the full operator
playbook (seed generation, storage, rotation, decode).
"""

from __future__ import annotations

import click

from bernstein.core.identity import install_rev as _identity
from bernstein.core.identity.install_rev import (
    DISABLED_SENTINEL,
    NONCE_BYTES,
    InvalidTokenError,
    SeedNotConfiguredError,
    get_install_rev,
    verify_token,
    verify_with_nonce,
)


@click.group(name="identity")
def identity_group() -> None:
    """Operator-side install-rev fingerprint helpers.

    \b
    The install-rev token is a 16-character base32 string embedded in
    artefacts the user voluntarily publishes (yaml configs, trace JSONL,
    role-prompt md footers).  No network egress, ever - operator-side
    discovery uses public GitHub code search (``gh search code
    'bernstein-rev:'``).

    \b
    Examples:
      bernstein identity show
      bernstein identity decode c4j2k7n8p3q5r9s7
      bernstein identity verify c4j2k7n8p3q5r9s7 \\
          --nonce 0123456789abcdef0123 --version-major 1
      bernstein identity disable
    """


@identity_group.command("show")
def show_cmd() -> None:
    """Print the current install's token (or the disabled sentinel)."""
    token = get_install_rev()
    click.echo(token)
    if not _identity.IDENTITY_EMISSION_ENABLED:
        click.echo(
            "(emission disabled - set IDENTITY_EMISSION_ENABLED=True after "
            "operator seed is in place; users do not need this)",
            err=True,
        )
    elif token == DISABLED_SENTINEL:
        click.echo(
            "(token is the disabled sentinel - kill switch is set, or BERNSTEIN_IDENTITY_SEED is unset/malformed)",
            err=True,
        )


def _verify_impl(token: str, nonce_hex: str | None, version_major: int | None) -> int:
    """Shared verification body for ``decode`` and ``verify``.

    Returns the click-style exit code (0 = valid, 1 = invalid, 2 = unable
    to decide because the seed isn't configured).
    """
    try:
        if nonce_hex is None:
            ok = verify_token(token)
        else:
            try:
                nonce_bytes = bytes.fromhex(nonce_hex)
            except ValueError as exc:
                click.echo(f"invalid --nonce hex: {exc}", err=True)
                return 1
            if len(nonce_bytes) != NONCE_BYTES:
                click.echo(
                    f"--nonce must be {NONCE_BYTES} bytes ({NONCE_BYTES * 2} hex chars), got {len(nonce_bytes)}",
                    err=True,
                )
                return 1
            ok = verify_with_nonce(token, nonce_bytes, version_major)
    except SeedNotConfiguredError as exc:
        click.echo(f"seed missing: {exc}", err=True)
        return 2
    except InvalidTokenError as exc:
        click.echo(f"invalid token: {exc}", err=True)
        return 1

    if ok:
        click.echo("valid")
        return 0
    click.echo("invalid")
    return 1


@identity_group.command("decode")
@click.argument("token")
def decode_cmd(token: str) -> None:
    """Confirm a token came from a real install (shape + sentinel check).

    Exits 0 when the token is shape-valid and not the disabled sentinel,
    1 when invalid, 2 when ``BERNSTEIN_IDENTITY_SEED`` is not configured.
    """
    raise SystemExit(_verify_impl(token, nonce_hex=None, version_major=None))


@identity_group.command("verify")
@click.argument("token")
@click.option(
    "--nonce",
    "nonce_hex",
    default=None,
    help=(
        f"Hex-encoded {NONCE_BYTES}-byte nonce from the user's install (when "
        "available via a debug bundle).  Enables full HMAC-strength verification."
    ),
)
@click.option(
    "--version-major",
    type=int,
    default=None,
    help="Optional major-version cohort byte; defaults to the running package version.",
)
def verify_cmd(token: str, nonce_hex: str | None, version_major: int | None) -> None:
    """Verify a token at HMAC strength when the operator has the user's nonce.

    Without ``--nonce``, behaviour matches ``decode`` (shape + sentinel
    rejection).  With ``--nonce``, the operator's seed plus the supplied
    nonce reproduces the token exactly via constant-time compare.
    """
    raise SystemExit(_verify_impl(token, nonce_hex=nonce_hex, version_major=version_major))


@identity_group.command("keydir")
def keydir_cmd() -> None:
    """Print the install-identity key directory (JWKS) as JSON.

    This is the local view of what the server publishes at
    ``/.well-known/http-message-signatures-directory`` - the Ed25519 public
    key(s) a verifier uses to validate the HTTP Message Signatures Bernstein
    places on its outbound agent-facing requests (issue #2305). Each key is
    advertised under its RFC 7638 thumbprint, which is the ``keyid`` those
    signatures carry.
    """
    import json

    from bernstein.core.identity import http_signing

    keydir = http_signing.build_key_directory(http_signing.default_keystore())
    click.echo(json.dumps(keydir, indent=2, sort_keys=True))


@identity_group.command("disable")
def disable_cmd() -> None:
    """Print the environment line that suppresses every emit site.

    Operators / users who want to opt out can paste this line into their
    shell rc to make every yaml/trace/prompt emit fall back to the
    disabled sentinel without touching code.
    """
    click.echo("export BERNSTEIN_DISABLE_IDENTITY=1")
