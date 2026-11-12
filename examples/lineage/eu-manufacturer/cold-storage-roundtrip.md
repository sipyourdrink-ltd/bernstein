# Cold-storage round-trip - 10-year retention path

## Why this matters

EU AI Act Article 12(3) requires the event log to be retained for a period
appropriate to the intended purpose, at least 6 months. For high-risk
machinery (Annex III §1(a)), notified bodies typically expect retention for
the lifetime of the equipment - 10+ years.

Hot storage (the live `.sdd/lineage/` directory) is unsuitable for that
horizon. The lineage v1 design exports a self-contained, signed bundle that
can sit on cold storage (tape, immutable S3, regulatory archive) and be
re-imported any time.

## Procedure

```bash
# 1. Export the live lineage state to a deterministic tar archive.
#    The archive is content-addressed: sha256(archive) is the retention key.
mkdir -p /tmp/cold-storage
tar --sort=name \
    --mtime='1980-01-01 00:00:00 UTC' \
    --owner=0 --group=0 --numeric-owner \
    -cf /tmp/cold-storage/eu-mfg-2026-03.tar \
    -C examples/lineage/eu-manufacturer fixtures

shasum -a 256 /tmp/cold-storage/eu-mfg-2026-03.tar | tee \
    /tmp/cold-storage/eu-mfg-2026-03.tar.sha256

# 2. Hand the .tar + .sha256 + the operator's signed manifest to your
#    retention provider (Glacier Deep Archive, tape vault, ...).

# 3. (Years later) Restore.
rm -rf examples/lineage/eu-manufacturer/fixtures   # simulate loss of hot copy
mkdir -p examples/lineage/eu-manufacturer
tar -xf /tmp/cold-storage/eu-mfg-2026-03.tar \
    -C examples/lineage/eu-manufacturer

# 4. Re-verify.
bernstein-verify chain config/robotics/safety_thresholds.yaml \
    --lineage-dir examples/lineage/eu-manufacturer/fixtures

bernstein-verify pack examples/lineage/eu-manufacturer/expected-pack.zip
```

## What round-tripping proves

| Property | How it's checked |
|---|---|
| Bit-exact restore | sha256 of restored tar matches the stored hash. |
| Chain integrity | Every entry's `parent_hashes` still resolves to entries in the restored log. |
| Signatures still verify | Ed25519 keys are embedded in `agent-cards/`; no external lookup required. |
| HMAC envelope intact | The operator HMAC head signature still matches the chain tail. |

## What round-tripping does NOT prove

- That the original operator HMAC key is still trusted. Verify its trust
  and rotation history separately.
- That the agent identities are still in use. Cards are historical evidence,
  not active credentials.
- That the underlying source code in `src/` still compiles. Lineage anchors
  content hashes, not source-tree state.

## CI assertion

The Makefile's `demo-eu-mfg` target runs:

```bash
diff <(zipinfo -1 expected-pack.zip | sort) \
     <(zipinfo -1 produced-pack.zip | sort)
```

A clean diff is the success criterion: the live `bernstein compliance pack`
command (built by Agent C) reproduces the committed reference bundle
byte-for-byte. The retention key shipped to the notified body is the
sha256 of either zip - they are identical.
