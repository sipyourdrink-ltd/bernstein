"""Declare fixture chains explicitly so integrity/authority tests stay independent of close."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

from bernstein.core.config.manifest import RunManifest, save_manifest
from bernstein.core.identity import delegation


def verify_declared_chain(*, root, run_id, key, **kwargs):
    path = delegation.DelegationLedger(root, key).receipt_path(run_id)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    manifest = RunManifest(
        run_id=run_id,
        delegation_head_hmac=rows[-1]["hmac"] if rows else delegation.GENESIS_HMAC,
        delegation_hop_count=len(rows),
    )
    manifest = replace(manifest, delegation_seal=manifest.compute_delegation_seal(key))
    manifest = replace(manifest, manifest_hash=manifest.compute_hash())
    with patch("bernstein.core.config.manifest.load_manifest", return_value=manifest):
        return delegation.verify_run_chain(root=root, run_id=run_id, key=key, **kwargs)


def close_manifest(ledger, run_id):
    manifest = RunManifest(run_id=run_id)
    object.__setattr__(manifest, "manifest_hash", manifest.compute_hash())
    save_manifest(manifest, ledger.root.parent)
    ledger.finalize_manifest(ledger.root.parent, run_id)
