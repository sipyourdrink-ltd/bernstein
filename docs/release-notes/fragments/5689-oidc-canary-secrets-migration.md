## OIDC canary and first secrets-migration consumer

Added `.github/workflows/trusted-env-canary.yml`, a dispatch-only
workflow that proves the `trusted` deployment environment's branch
policy is actually enforcing by observing GitHub refuse a run when
dispatched from a non-default branch. Also moved
`.github/workflows/branch-protection-audit.yml` onto the `trusted`
environment, fetching `BRANCH_PROTECTION_AUDIT_TOKEN` from the
external secret store via Infisical OIDC instead of reading the stored
Actions secret. The canary requires the `trusted` environment and its
branch policy to exist before the canary can prove anything real.
