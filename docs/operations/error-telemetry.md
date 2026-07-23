# Error telemetry and SBOM upload

Audience: SREs wiring Bernstein into an operator-managed error sink and
SBOM tracker.

## Error telemetry DSN flow

Bernstein ships a Sentry-protocol-compatible error sink wired through
`sentry-sdk`, with a dependency-free side-channel fallback for worker
subprocesses. Export the project DSN as `BERNSTEIN_TELEMETRY_DSN`;
when it is unset the sink is a no-op and `sentry-sdk` is never
imported, so minimal installs pay zero overhead. Sample rates are zero,
so events fire only on unhandled exceptions or explicit
`sentry_sdk.capture_*` calls -- no performance probes, no PII.

The telemetry contract (env-var resolution and the side-channel
transport) lives in
[the telemetry side channel](../observability/side-channel.md).

## SBOM upload to operator Dependency-Track

The `.github/workflows/sbom-upload.yml` workflow:

* triggers on `push` to `main` and on `release: published`,
* generates a CycloneDX SBOM via `cyclonedx-bom`,
* uploads the SBOM to a Dependency-Track instance addressed by
  `DT_API_URL` (required, no default; e.g. `https://dt.example.com`) using the
  `DT_API_KEY` secret.

The upload step is gated on both `DT_API_URL` and `DT_API_KEY` being
non-empty.  If either is unset the workflow generates the SBOM and exits
cleanly without uploading, so the pipeline is green even before the
operator has stood up Dependency-Track.
