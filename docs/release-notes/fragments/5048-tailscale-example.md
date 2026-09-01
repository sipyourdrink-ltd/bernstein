## Tailscale Worker Invocation (Issue #5048)

The Tailscale overlay example in `docs/cluster/deployment-patterns.md` now uses the correct `bernstein worker` invocation with `--roles backend` (plural) and `--token`, matching the top-level worker CLI surface. Previously the example inadvertently referenced `bernstein cluster worker` with a `--config` flag. (#5048)
