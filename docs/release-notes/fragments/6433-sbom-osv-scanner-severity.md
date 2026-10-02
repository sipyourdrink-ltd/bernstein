## The SBOM vulnerability gate now sees osv-scanner's severities

`SBOMVulnerabilityGate` blocks on a finding's severity, but the osv-scanner
parser looked for a list of `{"score": ...}` entries under
`database_specific.severity` and otherwise read the record's top-level
`severity` as a rating string. In real OSV records the first is a string
(`"CRITICAL"`) and the second is a list of CVSS vectors, so every
osv-scanner finding parsed as `unknown`. With osv-scanner installed it is
the scanner `SBOMGenerator.scan` tries first, so `POST /sbom/generate` with
`block_on_critical=true` answered `passed_gate: true` for a critical
advisory instead of 422.

A finding now takes the higher of osv-scanner's own group `max_severity`
(rated on the CVSS v3 scale) and the advisory's `database_specific.severity`,
with `MODERATE` read as medium. A record carrying neither stays `unknown`, as
before. A `database_specific` of `null` no longer aborts the parse. grype
parsing is unchanged (#6433).
