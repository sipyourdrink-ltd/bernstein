## Tailscale Worker Invocation (Issue #5048)

The Tailscale overlay example in `docs/cluster/deployment-patterns.md` now uses the correct `bernstein worker` invocation with `--roles backend` (plural) and `--token`, matching the top-level worker CLI surface. Previously the example referenced `bernstein cluster worker` with a `--config` flag.

The example now contains:

```
#   bernstein worker \
#       --server http://bernstein-central.tailXXXXX.ts.net:8052 \
#       --roles backend \
#       --token "$BERNSTEIN_CLUSTER_AUTH_SECRET"
```

The fragment describes the actual change made in this PR. (#5048)