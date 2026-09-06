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
* ``bernstein identity attest show|verify --run <id>`` - project or verify a
  run-attestation receipt without overloading install-rev verification.
* ``bernstein identity agents`` - list the agent principals the grant and
  delegation chains establish, with the capability ceiling in force now and
  the chain events behind each entry.  ``--verify <file>`` recomputes a stored
  projection from the chain and refuses any entry the chain does not establish.
* ``bernstein identity review --since <date>`` - derive a signed per-principal
  access review from the delegation and grant chains, and record a reviewer's
  sign-off as its own chain event.

The install-rev verbs are read-only and never open a network connection.  This
is the project's hard rule: no telemetry, ever.  The nested ``attest verify``
verb may write a verified receipt into the operator-selected evidence directory.

See ``docs/operations/install-fingerprint.md`` for the full operator
playbook (seed generation, storage, rotation, decode).
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.commands.identity_attest_cmd import attest_group
from bernstein.cli.commands.identity_review_cmd import review_group
from bernstein.core.identity import agent_registry
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
      bernstein identity attest show --run r-1234 \\
          --signing-key-path key.pem
      bernstein identity agents --json
      bernstein identity review --since 2026-01-01
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


@identity_group.command("export-verifier", hidden=True)
@click.option(
    "--target",
    type=click.Choice(["local", "server"], case_sensitive=False),
    default="local",
    help=(
        "Verifier file target. 'local' targets ~/.config/bernstein/verifier/local.json "
        "(operator workstation); 'server' targets ~/.config/bernstein/verifier/server.json "
        "(shared server filesystem)."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the destination path without writing anything.",
)
def export_verifier_cmd(target: str, dry_run: bool) -> None:
    """Write the install-identity JWKS to a per-platform verifier file.

    Writes the JWKS as canonical JSON and a ``.json.sha256`` sidecar. Skips the
    write when the key content is unchanged since the last run (hash compared
    against the sidecar); use ``--dry-run`` to print the destination without
    writing.

    Targets:

    \\b
      local  -> ~/.config/bernstein/verifier/local.json   (default, operator workstation)
      server -> ~/.config/bernstein/verifier/server.json  (shared server filesystem)

    This command mirrors the ``/.well-known/http-message-signatures-directory``
    JWKS endpoint but writes to a local file so a verifier can pin the trust
    anchor without a runtime fetch.
    """
    import hashlib
    import json
    from pathlib import Path

    from bernstein.core.identity import http_signing

    verifier_dir = Path.home() / ".config" / "bernstein" / "verifier"
    filename = f"{target}.json"
    dest = verifier_dir / filename
    sidecar = dest.with_name(f"{target}.json.sha256")

    keydir = http_signing.build_key_directory(http_signing.default_keystore())

    canonical = json.dumps(keydir, separators=(",", ":"), sort_keys=True)
    content_hash = hashlib.sha256(canonical.encode("ascii")).hexdigest()

    if dry_run:
        click.echo(str(dest))
        return

    if sidecar.exists() and sidecar.read_text().strip() == content_hash:
        click.echo(f"unchanged: {dest}")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(canonical, encoding="utf-8")
    sidecar.write_text(content_hash, encoding="utf-8")
    click.echo(f"wrote: {dest}")


@identity_group.command("disable")
def disable_cmd() -> None:
    """Print the environment line that suppresses every emit site.

    Operators / users who want to opt out can paste this line into their
    shell rc to make every yaml/trace/prompt emit fall back to the
    disabled sentinel without touching code.
    """
    click.echo("export BERNSTEIN_DISABLE_IDENTITY=1")


def _audit_key_for_read() -> bytes:
    """Return the install audit key, read-only.

    A verifier that minted its own key would fail every HMAC check against a
    chain written under the real one, so a missing key is an operator error
    rather than something to paper over with fresh key material -- the same
    rule ``bernstein spiffe verify-binding`` follows.
    """
    from bernstein.core.security.audit import load_audit_key

    return load_audit_key()


@identity_group.command("agents")
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Audit root holding grants/ and delegation/ (default: .sdd/audit).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the canonical JSON projection.")
@click.option(
    "--as-of",
    "as_of",
    type=int,
    default=None,
    help="Epoch second the capability ceiling is resolved at (default: now).",
)
@click.option("--trust-domain", "trust_domain", default=None, help="SPIFFE trust domain for id derivation.")
@click.option(
    "--install-key",
    "install_key",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Install public key PEM (SPKI Ed25519) for SPIFFE id derivation.",
)
@click.option(
    "--verify",
    "verify_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Recompute a stored projection from the chain instead of printing one.",
)
def agents_cmd(
    root: Path | None,
    as_json: bool,
    as_of: int | None,
    trust_domain: str | None,
    install_key: Path | None,
    verify_file: Path | None,
) -> None:
    """List the agent principals the audit chain establishes.

    The listing is a projection over the grant and delegation chains, not a
    stored directory: every entry names the chain events behind it, and the
    capability ceiling is the one in force at ``--as-of``, not the one the
    widest grant carried when it was issued.

    With ``--verify`` the stored projection is recomputed from the chain.
    Exit codes: 0 verified, 1 no records or unreadable input, 2 mismatch.
    """
    import time

    if (trust_domain is None) != (install_key is None):
        raise click.UsageError("--trust-domain and --install-key must be given together")

    audit_root = root if root is not None else Path(".sdd/audit")
    moment = int(as_of if as_of is not None else time.time())
    install_pem = install_key.read_bytes() if install_key is not None else None
    key = _audit_key_for_read()

    if verify_file is not None:
        verification = agent_registry.verify_registry(
            verify_file,
            root=audit_root,
            key=key,
            now=moment,
            trust_domain=trust_domain,
            install_public_key_pem=install_pem,
        )
        if verification.ok:
            click.echo(f"verified: {verification.reason}")
            raise SystemExit(0)
        click.echo(f"mismatch: {verification.reason}", err=True)
        raise SystemExit(2)

    projection = agent_registry.project_agents(
        root=audit_root,
        key=key,
        now=moment,
        trust_domain=trust_domain,
        install_public_key_pem=install_pem,
    )
    if as_json:
        click.echo(agent_registry.render_registry(projection))
        return

    if not projection.agents:
        click.echo("no agent principals: the chain under this root establishes none", err=True)
    for entry in projection.agents:
        ceiling = ", ".join(entry.capability_ceiling) or "(nothing in force)"
        click.echo(f"{entry.agent_id}\t{entry.spiffe_id or '-'}\t{ceiling}")
        click.echo(
            f"  grants={len(entry.grants)} delegations={len(entry.delegations)} events={len(entry.chain_events)}"
        )
    for err in projection.errors:
        click.echo(err, err=True)


# ``identity attest`` is a separate group rather than new verbs here because
# ``identity verify`` above checks an install-rev fingerprint token, which is a
# different object from a run's attestation evidence; sharing the verb would
# give one noun two meanings.
identity_group.add_command(attest_group, "attest")

# ``identity review`` is a third noun again: not an install-rev token and not a
# run's attestation evidence, but a windowed projection of who was granted what
# across runs. The verbs stay grouped so ``review verify`` cannot be confused
# with either of the other two ``verify`` verbs.
identity_group.add_command(review_group, "review")
