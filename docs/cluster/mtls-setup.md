# Cluster mTLS setup

Bernstein's cluster mode supports native mutual TLS on the node-to-node
transport. This is opt-in: existing plain-HTTP deployments keep working.
When TLS is configured, every registration, heartbeat, and task-steal call
between the central server and worker nodes goes over `https://` and the
TLS handshake validates the peer certificate before any HTTP byte is read.

mTLS authenticates the *channel*. The existing JWT bearer token still
authorises the *action* - both layers compose.

## When to turn it on

- The cluster is internet-facing or crosses untrusted networks.
- You're running a regulated workload and need encryption-in-transit
  evidence for audit.
- Bernstein is one of several services in a shared K8s namespace and you
  want to avoid wrapping every call in an Istio sidecar.

For a single-VPC, single-namespace deployment where all traffic is
already on a trusted backbone, plain HTTP plus the JWT token is still a
defensible default.

## What you need

Three artifacts on the central server, three on every worker:

| File         | Central       | Worker        | Purpose                       |
|--------------|---------------|---------------|-------------------------------|
| `ca.crt`     | yes           | yes           | Trust anchor                  |
| `server.crt` | yes           | no            | Central node identity         |
| `server.key` | yes (chmod 0600) | no         | Central node private key      |
| `node.crt`   | no            | yes           | Worker identity               |
| `node.key`   | no            | yes (chmod 0600) | Worker private key         |

The CA can be any PKI you already operate (step-ca, cert-manager,
HashiCorp Vault, your corporate CA). For a self-hosted internal cluster
where you don't have a CA yet, Bernstein ships a one-shot helper.

## Quick start: self-signed CA

```bash
bernstein cluster bootstrap-ca
```

Writes `ca.crt`, `ca.key`, and the server and node cert/key pairs to `~/.bernstein/cluster/`.
Keep `ca.key` on the operator machine only. Private keys are
chmod 0600. Pass `--out-dir` to override the destination, `--server-san`
(repeatable) to add DNS SANs to the server cert.

> **This is a self-signed CA.** It's appropriate for self-hosted
> internal clusters on infrastructure you control. For production
> deployments - anything customer-facing, anything regulated, anything
> where cert rotation needs to be automated - use your own CA.

## Wiring TLS into ClusterConfig

`ClusterConfig` gained a `tls` field (`TLSConfig | None`). When set, the
URL scheme derived for cluster traffic flips from `http` to `https`
automatically.

```python
from pathlib import Path
from bernstein.core.tasks.models import ClusterConfig
from bernstein.core.protocols.cluster.cluster_tls import TLSConfig

tls = TLSConfig(
    ca_file=Path("~/.bernstein/cluster/ca.crt"),
    cert_file=Path("~/.bernstein/cluster/server.crt"),
    key_file=Path("~/.bernstein/cluster/server.key"),
    verify_mode="required",
)
config = ClusterConfig(enabled=True, tls=tls, server_url="https://central.example.com:8052")
assert config.cluster_url_scheme == "https"
```

`verify_mode` accepts:

- `"required"` - full mTLS. Workers without a valid client cert are
  rejected at the TLS handshake. **Use this in production.**
- `"optional"` - TLS server auth is mandatory; client cert is requested
  but accepted if absent. Useful for staged rollouts.
- `"disabled"` - TLS is on, but no client cert verification. The cert
  chain is loaded for the encryption-in-transit guarantee only.

## Distributing keys to workers

`bootstrap-ca` writes both `server.*` and `node.*` to a single directory
on the operator's machine. Distribute the worker artifacts out-of-band
(scp, your secret manager, a config-management tool, K8s secret) - never
commit them to git, never push them through the cluster API itself.

A worker only needs `ca.crt`, `node.crt`, `node.key`. Drop them into the
worker's `~/.bernstein/cluster/` (or whatever path you prefer) and point
its `ClusterConfig.tls` at them.

## Rotation

Rotation is manual. Steps:

1. Generate a new server cert + key from the same CA. Replace the files
   on the central node. Restart the central server - the new cert is
   loaded at uvicorn boot.
2. For each worker, generate a new node cert + key from the same CA,
   replace the files, restart the worker. Workers pick up the new cert
   on the next heartbeat cycle.
3. To rotate the CA itself: cross-sign the old and new CA, distribute
   the bundle, then phase out the old root. This is operator
   responsibility - Bernstein loads whatever CA bundle you point it at.

For automated rotation (cert-manager hooks, ACME, etc.), run a sidecar
that writes the cert files into place and SIGHUP/restart the Bernstein
server when they change.

## Verifying it works

After bringing up a 2-node cluster with `tls.verify_mode=required`:

```bash
# On the central node
curl --cacert ~/.bernstein/cluster/ca.crt \
     --cert   ~/.bernstein/cluster/server.crt \
     --key    ~/.bernstein/cluster/server.key \
     -H "Authorization: Bearer $BERNSTEIN_AUTH_TOKEN" \
     https://localhost:8052/cluster/status
# expected: the JSON cluster summary (topology, node counts, ...)
# (or probe the public https://localhost:8052/health for a TLS-only check)

# Same call without --cert/--key should fail at the TLS handshake.
curl --cacert ~/.bernstein/cluster/ca.crt https://localhost:8052/cluster/status
# expected: SSL handshake error / 400 No required SSL certificate was sent.
```

## Troubleshooting

- **`ssl: SSL_ERROR_SSL`** on the worker: the worker's `ca.crt` doesn't
  include the issuer of the server cert. Check that both sides reference
  the same CA bundle.
- **`certificate verify failed: unable to get issuer certificate`** on
  the central node: a worker is presenting a cert signed by a different
  CA. Re-issue it from the cluster CA.
- **`PermissionError` on `*.key`**: keys must be readable by the
  Bernstein process user. `bootstrap-ca` writes 0600 - adjust ownership,
  not permissions.
- **Plain HTTP traffic is being rejected after enabling TLS**: this is
  expected. Workers built before TLS rollout have to be updated to set
  `ClusterConfig.tls` before they'll re-register. Stage the rollout via
  `verify_mode="optional"` to catch stragglers.

## Scope

- Cert rotation is manual; automate it via a sidecar that writes the
  files and SIGHUP/restarts the server.
- One CA per cluster.
- mTLS authenticates the channel; the JWT bearer token still
  authorises the action. They compose; neither is removed.
- `bootstrap-ca` produces a self-signed CA suitable for self-hosted
  internal clusters. For production-grade deployments use an external
  PKI (step-ca, cert-manager, Vault, your corporate CA).
- Plain-HTTP cluster mode remains supported; this feature is opt-in
  via `ClusterConfig.tls`.

## Related

- Source: `src/bernstein/core/protocols/cluster/cluster_tls.py`
- CLI: `bernstein cluster bootstrap-ca`
- [Cluster deployment patterns](deployment-patterns.md) - Cloudflare Tunnel + Tailscale + same-VPC
- [Cluster observability](../observability/cluster.md) - metrics + audit events for cert validation
- [Cluster end-to-end harness](../testing/cluster-e2e-harness.md) - mTLS-on parameterised scenarios
- PR #1019, ticket `2026-05-05-feat-cluster-mtls-transport.md`
