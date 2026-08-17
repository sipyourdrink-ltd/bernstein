"""``bernstein receipt``: create and offline-verify result receipt bundles (#3870).

A result receipt bundle is the portable unit of trust for a worker submission:
the patch, the gate results, the task reference, the sandbox selection, and the
worker's DSSE-signed attestation over all of it, linked into the worker's
receipt chain. ``bernstein receipt verify`` checks it with no network:

    bernstein receipt verify bundle.json                 # trust the embedded key (TOFU)
    bernstein receipt verify bundle.json --pubkey k.pem  # pin the worker key
    bernstein receipt create spec.json --signing-key worker.pem -o bundle.json

Provenance is only established when ``--pubkey`` pins the worker key. Without it
the bundle's embedded key is trusted on first use -- the bytes are authenticated
against a key the worker supplied, not against a key the operator pinned -- and
the command says so on success, mirroring ``bernstein a2a verify``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


def _fail(message: str) -> None:
    click.echo(f"✗ {message}", err=True)
    raise SystemExit(1)


@click.group("receipt")
def receipt_group() -> None:
    """Create and offline-verify result receipt bundles."""


@receipt_group.command("verify")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--pubkey",
    "pubkey_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Pin the worker key: verify against this Ed25519 public key (PEM).",
)
@click.option("--prev-digest", default=None, help="Expected predecessor bundle digest (chain continuity).")
@click.option(
    "--expected-manifest-digest",
    default=None,
    help="Expected volunteer-manifest digest: ties the run to a declared policy, not one the worker chose.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def verify_cmd(
    bundle_path: Path,
    pubkey_path: Path | None,
    prev_digest: str | None,
    expected_manifest_digest: str | None,
    as_json: bool,
) -> None:
    """Verify a result receipt bundle offline.

    Exits non-zero on any failure -- a bad signature, a tampered patch or gate
    log, an inconsistent digest, a broken chain link, or a manifest digest that
    is not the one you expected -- naming the exact field that diverged.

    Without ``--expected-manifest-digest`` the bundle's ``manifest_sha256`` is
    carried but never compared, and the command says so rather than letting a
    bare ``✓`` read as a policy check.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from bernstein.core.security.result_receipt_bundle import load_bundle, verify_result_bundle

    try:
        envelope = load_bundle(bundle_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(f"could not parse bundle: {exc}")
        return

    # Resolve the verifying key: pinned PEM, else the bundle's embedded worker
    # key (trust on first use).
    pinned = pubkey_path is not None
    if pinned:
        public_key = serialization.load_pem_public_key(pubkey_path.read_bytes())
        if not isinstance(public_key, Ed25519PublicKey):
            _fail("pinned key is not an Ed25519 public key")
            return
    else:
        try:
            payload = json.loads(envelope.payload_bytes)
            pem = payload["predicate"]["bundle"]["worker"]["public_key_pem"].encode("ascii")
            public_key = serialization.load_pem_public_key(pem)
        except (KeyError, ValueError) as exc:
            _fail(f"bundle carries no usable worker public key: {exc}")
            return

    result = verify_result_bundle(
        envelope,
        public_key,
        expected_prev_digest=prev_digest,
        expected_manifest_sha256=expected_manifest_digest,
    )

    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": result.ok,
                    "keyid": result.keyid,
                    "digest": result.digest,
                    "pinned_key": pinned,
                    "manifest_digest_checked": result.manifest_digest_checked,
                    "errors": [{"field": e.field, "message": e.message} for e in result.errors],
                },
                indent=2,
            )
        )
    else:
        if result.ok:
            trust = "pinned key" if pinned else "embedded key (trust on first use)"
            click.echo(f"✓ bundle verifies against {trust}")
            click.echo(f"  keyid:  {result.keyid}")
            click.echo(f"  digest: {result.digest}")
            if result.manifest_digest_checked:
                click.echo(f"  manifest: checked against {expected_manifest_digest}")
            else:
                click.echo("  manifest: carried, NOT checked")
            if not pinned:
                click.echo("  note: provenance requires pinning the worker key with --pubkey", err=True)
            if not result.manifest_digest_checked:
                click.echo(
                    "  note: the run is not tied to a declared policy without --expected-manifest-digest",
                    err=True,
                )
        else:
            click.echo("✗ bundle verification failed:", err=True)
            for e in result.errors:
                click.echo(f"    {e.field}: {e.message}", err=True)

    if not result.ok:
        sys.exit(1)


@receipt_group.command("create")
@click.argument("spec_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--signing-key",
    "signing_key_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Worker Ed25519 private key (PEM) to sign the bundle.",
)
@click.option("-o", "--output", "output_path", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--manifest-repo",
    "manifest_repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repository root whose .bernstein/volunteer.json supplies manifest_sha256, overriding the spec.",
)
def create_cmd(spec_path: Path, signing_key_path: Path, output_path: Path, manifest_repo: Path | None) -> None:
    """Build and sign a result receipt bundle from a JSON spec.

    The spec provides task, patch, gates, manifest hash, adapter/model ids,
    sandbox profile and selection receipt, timestamp, and the chain link
    (``anchor`` + ``length``). The worker identity is derived from the signing
    key, so it cannot disagree with the signature.

    ``--manifest-repo`` does the same for the manifest digest: the value is
    read out of the project's declared manifest at its canonical path rather
    than taken from the spec, so ``manifest_sha256`` stops being a string the
    submitter typed. It takes a repository root, not a manifest file, because a
    loose JSON file the submitter points at is the same unvalidated value in a
    new costume. Without the flag the command behaves exactly as before.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key
    from bernstein.core.security.result_receipt_bundle import (
        ChainLink,
        GateResult,
        ResultBundle,
        TaskRef,
        build_result_bundle,
        bundle_with_manifest_digest,
        write_bundle,
    )
    from bernstein.core.volunteer.manifest import VolunteerManifestError, load_manifest_from_repo

    key = serialization.load_pem_private_key(signing_key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        _fail("signing key is not an Ed25519 private key")
        return
    pub = key.public_key()

    manifest = None
    if manifest_repo is not None:
        try:
            manifest = load_manifest_from_repo(manifest_repo)
        except (FileNotFoundError, VolunteerManifestError) as exc:
            _fail(f"could not load manifest from {manifest_repo}: {exc}")
            return

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        task = spec["task"]
        chain = spec["chain"]
        bundle = ResultBundle(
            task=TaskRef(repo=task["repo"], commit_sha=task["commit_sha"], issue_number=task.get("issue_number")),
            patch=spec["patch"],
            gates=tuple(
                GateResult(command=g["command"], exit_code=g["exit_code"], log=g["log"]) for g in spec.get("gates", [])
            ),
            # Read from the spec only when no manifest was supplied; the helper
            # below discards this value outright when one was.
            manifest_sha256=spec["manifest_sha256"] if manifest is None else "",
            adapter_id=spec["adapter_id"],
            model_id=spec["model_id"],
            sandbox_profile=spec["sandbox_profile"],
            selection_receipt=spec["selection_receipt"],
            created_at=spec["created_at"],
            worker_keyid=keyid_from_public_key(pub),
            worker_public_key_pem=export_public_key_pem(pub).decode("ascii"),
            chain=ChainLink(anchor=chain["anchor"], length=int(chain["length"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _fail(f"invalid spec: {exc}")
        return

    if manifest is not None:
        bundle = bundle_with_manifest_digest(bundle, manifest)

    envelope = build_result_bundle(bundle, signing_key=key)
    write_bundle(envelope, output_path)
    click.echo(f"✓ wrote signed bundle to {output_path}")
    click.echo(f"  digest: {bundle.digest}")
    if manifest is not None:
        click.echo(f"  manifest: {bundle.manifest_sha256} (derived from {manifest_repo})")
