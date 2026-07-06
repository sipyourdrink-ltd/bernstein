"""CLI command group: ``bernstein credential`` -- C2PA content credentials.

Two subcommands project the lineage spine into a signed C2PA manifest
and verify it back against the artifact and the install identity:

* ``bernstein credential emit <artifact>`` -- project the artifact's
  lineage-spine subtree into a C2PA 2.2 manifest, sign it with the
  install-identity Ed25519 key, and write it next to the artifact as
  ``<artifact>.c2pa.json``.
* ``bernstein credential verify <artifact>`` -- re-read the manifest and
  confirm the hard-binding hash matches the artifact bytes and the
  signature chains to the install identity.

The manifest is a *projection* of the spine (issue #2303): with no
lineage entry for the artifact there is nothing to project, so ``emit``
fails rather than fabricating an unsigned label. See
:mod:`bernstein.core.lineage.c2pa` for the projection primitives.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console
from bernstein.core.lineage.c2pa import (
    ManifestError,
    ManifestIdentity,
    manifest_from_dict,
    manifest_to_dict,
    project_manifest,
    sign_manifest,
    verify_manifest,
)
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger(__name__)

INSTALL_SIGNING_KEY_ENV = "BERNSTEIN_CREDENTIAL_SIGNING_KEY"
DEFAULT_INSTALL_SIGNING_KEY = ".sdd/runtime/credential/install.key"
MANIFEST_SUFFIX = ".c2pa.json"


@click.group("credential")
def credential_group() -> None:
    """Emit and verify C2PA content credentials from the lineage spine.

    \b
    Examples:
      bernstein credential emit out/report.md --run-id run-1
      bernstein credential verify out/report.md
    """


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


@credential_group.command("emit")
@click.argument("artifact", required=True, type=click.Path(dir_okay=False))
@click.option(
    "--run-id",
    "run_id",
    required=True,
    help="Lineage run id whose spine the manifest is projected from.",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the manifest to stdout as JSON instead of writing a file.",
)
def credential_emit(artifact: str, run_id: str, workdir: str, as_json: bool) -> None:
    """Project ``ARTIFACT``'s lineage subtree into a signed C2PA manifest.

    Exit codes: 0 = written, 1 = no lineage / bad input.
    """
    root = Path(workdir).resolve()
    artifact_path = _repo_relative(root, artifact)
    # Read the artifact to fail fast when it is missing; the manifest's
    # content hash comes from the spine entry, not this read.
    _read_artifact(root, artifact_path)

    entries = list(_load_spine(root, run_id).iter_entries())
    identity = ManifestIdentity(
        install_rev=_load_install_rev(),
        keyid=_keyid(root),
        run_id=run_id,
    )
    try:
        manifest = project_manifest(
            artifact_path=artifact_path,
            entries=entries,
            identity=identity,
        )
    except ManifestError as exc:
        raise click.ClickException(sanitize_log(str(exc))) from exc

    signed = sign_manifest(manifest, signing_key=_load_or_create_install_key(_signing_key_path(root)))
    doc = json.dumps(manifest_to_dict(signed), sort_keys=True, indent=2)

    if as_json:
        click.echo(doc)
        return

    dest = (root / artifact_path).with_name(Path(artifact_path).name + MANIFEST_SUFFIX)
    dest.write_text(doc, encoding="utf-8")
    console.print(
        f"[green]OK[/green] -- wrote content credential {sanitize_log(str(dest))} "
        f"(entry {signed.lineage_entry_hash[:16]})"
    )


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@credential_group.command("verify")
@click.argument("artifact", required=True, type=click.Path(dir_okay=False))
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Manifest path (defaults to <artifact>.c2pa.json).",
)
def credential_verify(artifact: str, workdir: str, manifest_path: str | None) -> None:
    """Verify ``ARTIFACT``'s content credential against the artifact bytes.

    Confirms the hard-binding hash matches the artifact and the signature
    chains to the install identity.

    Exit codes: 0 = OK, 1 = bad input, 2 = verification failed.
    """
    root = Path(workdir).resolve()
    artifact_path = _repo_relative(root, artifact)
    content = _read_artifact(root, artifact_path)

    if manifest_path is not None:
        mpath = Path(manifest_path)
    else:
        mpath = (root / artifact_path).with_name(Path(artifact_path).name + MANIFEST_SUFFIX)
    if not mpath.exists():
        console.print(f"[red]No content credential at[/red] {sanitize_log(str(mpath))}")
        raise SystemExit(1)

    try:
        payload = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        console.print(f"[red]Cannot read manifest:[/red] {sanitize_log(str(exc))}")
        raise SystemExit(1) from exc

    try:
        manifest = manifest_from_dict(payload)
    except ManifestError as exc:
        console.print(f"[red]Malformed manifest:[/red] {sanitize_log(str(exc))}")
        raise SystemExit(1) from exc

    public_key = _load_or_create_install_key(_signing_key_path(root)).public_key()
    result = verify_manifest(manifest, content, public_key)

    console.print()
    console.print(
        f"[bold]Content credential[/bold] artifact={sanitize_log(artifact_path)} "
        f"entry={manifest.lineage_entry_hash[:16]}"
    )
    if result.ok:
        console.print("[green]OK[/green] -- hard binding matches, signature chains to install identity.")
        raise SystemExit(0)
    console.print(f"[red]VERIFICATION FAILED[/red] -- {len(result.errors)} error(s):")
    for err in result.errors:
        console.print(f"  - {sanitize_log(err)}")
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_relative(root: Path, artifact: str) -> str:
    """Return ``artifact`` as a repo-relative POSIX path under ``root``."""
    candidate = Path(artifact)
    abs_candidate = candidate if candidate.is_absolute() else (root / candidate)
    try:
        rel = abs_candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise click.ClickException(
            f"artifact {sanitize_log(artifact)} is outside the workspace root",
        ) from exc
    return rel.as_posix()


def _read_artifact(root: Path, artifact_path: str) -> bytes:
    path = root / artifact_path
    if not path.exists():
        raise click.ClickException(f"artifact not found: {sanitize_log(artifact_path)}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise click.ClickException(f"cannot read artifact {sanitize_log(artifact_path)}: {exc}") from exc


def _load_spine(root: Path, run_id: str) -> LineageSpine:
    lineage_root = root / ".sdd" / "lineage"
    return LineageSpine(lineage_root, run_id=run_id, hmac_key=_load_hmac_key())


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _signing_key_path(root: Path) -> Path:
    override = os.environ.get(INSTALL_SIGNING_KEY_ENV)
    if override:
        return Path(override).expanduser()
    return root / DEFAULT_INSTALL_SIGNING_KEY


def _keyid(root: Path) -> str:
    from bernstein.core.security.audit_dsse import keyid_from_public_key

    key = _load_or_create_install_key(_signing_key_path(root))
    return keyid_from_public_key(key.public_key())


def _load_or_create_install_key(path: Path) -> Ed25519PrivateKey:
    """Load or generate the install Ed25519 signing key at ``path``.

    Reuses an existing 32-byte seed when present; generates a fresh
    keypair otherwise and persists the raw seed with mode 0600. The same
    key anchors the install identity so the manifest signature and the
    identity share one attestation root (AC5).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if path.exists():
        try:
            raw = path.read_bytes().strip()
        except OSError as exc:
            raise click.ClickException(f"cannot read signing key {sanitize_log(str(path))}: {exc}") from exc
        if len(raw) != 32:
            raise click.ClickException(
                f"install signing key {sanitize_log(str(path))} is not 32 raw bytes; refusing to use it",
            )
        return Ed25519PrivateKey.from_private_bytes(raw)

    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    priv = Ed25519PrivateKey.generate()
    raw_bytes = priv.private_bytes_raw()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw_bytes)
    finally:
        os.close(fd)
    path.chmod(0o600)
    return priv


def _load_install_rev() -> str:
    """Return the passive install fingerprint (best-effort, never raises)."""
    try:
        from bernstein.core.identity.install_rev import get_install_rev
    except ImportError:
        logger.warning("install_rev module unavailable; credential identity tokens will be empty")
        return ""
    try:
        return get_install_rev()
    except Exception:  # pragma: no cover - defensive
        logger.exception("install_rev lookup failed during credential emit; using empty fingerprint")
        return ""
