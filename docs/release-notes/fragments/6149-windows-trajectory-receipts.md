## Trajectory receipt filenames are Windows-safe

Trajectory receipts now store their canonical `sha256:<digest>` identity as
`<digest>.json`, avoiding the `:` character that Windows reserves in filenames.
Existing receipts written with the legacy prefixed filename remain readable on
platforms that support that spelling, while signed receipt identities are unchanged. (#6149)
