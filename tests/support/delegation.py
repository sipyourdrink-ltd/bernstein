"""Declare fixture chains explicitly so integrity/authority tests stay independent of close."""

from __future__ import annotations

import json

from bernstein.core.config.manifest import RunManifest, save_manifest
from bernstein.core.identity import delegation


def verify_declared_chain(*, root, run_id, key, **kwargs):
    path = delegation.DelegationLedger(root, key).receipt_path(run_id)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    return delegation.verify_run_chain(
        root=root,
        run_id=run_id,
        key=key,
        declared_head_hmac=rows[-1]["hmac"] if rows else delegation.GENESIS_HMAC,
        declared_hop_count=len(rows),
        **kwargs,
    )


def close_manifest(ledger, run_id):
    save_manifest(RunManifest(run_id=run_id), ledger.root.parent)
    ledger.finalize_manifest(ledger.root.parent, run_id)
