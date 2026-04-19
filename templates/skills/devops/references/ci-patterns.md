# CI patterns (GitHub Actions)

- Pin action versions by SHA, not tag, for supply-chain safety.
- Use `permissions:` on every workflow; default to `contents: read`.
- Cache `uv` / `pip` / `npm` artefacts by lockfile hash.
- Run lint / format / type checks in parallel with tests.
- Fail fast: mark non-essential jobs `continue-on-error: false` unless they
  really are optional.
- Upload artefacts (coverage, logs) with a short retention (7-14 days).

## Reusable workflows
- Factor shared steps into composite actions or reusable workflows.
- Keep matrices narrow — every combination costs CI minutes.

## Concurrency
- Use `concurrency: ${{ github.ref }}` with `cancel-in-progress: true` for
  PR workflows so pushes cancel stale runs.
- Never cancel-in-progress on `main` — partially-run deploys corrupt state.
