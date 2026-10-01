## Trajectory receipt filenames are Windows-safe

Trajectory receipts now store their canonical `sha256:<digest>` identity as
`<digest>.json`, avoiding the `:` character that Windows reserves in filenames.
Existing receipts written with the legacy prefixed filename remain readable on
platforms that support that spelling, while signed receipt identities are unchanged. Lineage `artifact_path` entries now use the bare digest filename. (#6149)
