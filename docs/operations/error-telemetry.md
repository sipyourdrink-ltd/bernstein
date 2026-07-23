# Error telemetry and SBOM

Audience: SREs wiring Bernstein into an operator-managed error sink and
consuming release SBOMs.

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

## Release SBOMs

The `.github/workflows/sbom.yml` workflow:

* triggers on `release: published` (and `workflow_dispatch`),
* generates SPDX and CycloneDX SBOMs for the release build,
* attaches both SBOMs as release assets and uploads them as workflow
  artifacts.

Downstream consumers can ingest these SBOMs into whatever SCA tooling
they run (Grype, Trivy, etc.). No SBOM data is sent anywhere by the
project itself.
