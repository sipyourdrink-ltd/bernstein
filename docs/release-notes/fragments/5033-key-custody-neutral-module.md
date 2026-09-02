## KMS adapters moved to a subsystem-neutral custody module

`KMSAdapter` and its backends (`FileBasedKMSAdapter`, `EnvBasedKMSAdapter`,
`HSMKMSAdapter`) are now defined in `bernstein.core.security.key_custody` and
re-exported from `bernstein.core.security.lineage_kms` for backward
compatibility. The neutral name makes the signing-key custody boundary
visible across the whole project, not just the lineage subsystem that originally
needed it. The `lineage_kms` import path continues to work unchanged.

A new guard test (`test_key_custody_boundary.py`) pins the boundary and
records the 26 files that currently hold raw private key material directly,
so the set can only shrink. (#5033)
