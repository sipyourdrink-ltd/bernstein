# Cluster deployment patterns

Bernstein's cluster mode is a STAR topology: one central server, N workers.
The central server runs the orchestrator, the API, and the task store.
Workers register, heartbeat, and pull tasks for the roles they advertise.

This page describes three deployment patterns for getting workers to reach
the central server across whatever network shape your environment imposes.
Pick one based on where the workers live relative to the central node.

| Pattern              | When to use                                                  | Complexity |
|----------------------|--------------------------------------------------------------|------------|
| Same-VPC mTLS        | Central + workers on the same trusted network                | Low        |
| Cloudflare Tunnel    | Workers on the public internet, central behind a NAT/firewall | Medium     |
| Tailscale overlay    | Workers on contractor laptops or a different cloud account    | Medium     |

> Tunnels and mTLS stack. The tunnel protects the network path; mTLS
> (see [`mtls-setup.md`](./mtls-setup.md)) authenticates the application.
> A regulated production deployment usually wants both.

---

## Pattern 1 - Same-VPC mTLS

Both the central server and the workers run inside one trusted network
(office VPC, single AWS VPC, single GKE namespace). No NAT to traverse.
Authenticity comes from mTLS at the transport layer plus the cluster JWT
at the application layer.

This is the simple case and is fully covered in
[`mtls-setup.md`](./mtls-setup.md). A condensed checklist:

```bash
# On the central server
bernstein cluster bootstrap-ca \
    --out-dir ~/.bernstein/cluster \
    --server-san central.internal

# Bind to all interfaces so workers can reach it.
BERNSTEIN_BIND_HOST=0.0.0.0 bernstein start
```

> **Containers:** `bernstein start` detaches the task server and returns, so
> inside a container (where the CLI is PID 1) it exits immediately and takes the
> server with it. Use the foreground command instead so PID 1 stays alive:
>
> ```bash
> docker run -e BERNSTEIN_BIND_HOST=0.0.0.0 -e BERNSTEIN_CLUSTER_ENABLED=1 \
>     -p 8052:8052 ghcr.io/sipyourdrink-ltd/bernstein serve
> ```
>
> `serve` is also the image's default `CMD`, so `docker run ... <image>` with no
> arguments starts the same long-lived coordinator whose `/health` endpoint the
> image `HEALTHCHECK` probes.

mTLS is not configured with CLI flags: wire the CA, server cert, and
server key into `ClusterConfig.tls` as shown in
[`mtls-setup.md`](./mtls-setup.md).

```bash
# On each worker
# (ca.crt + node.crt + node.key copied out-of-band into ~/.bernstein/cluster/)
bernstein worker \
    --server https://central.internal:8052 \
    --roles backend
```

The worker's CA, node cert, and node key are wired through
`ClusterConfig.tls` (see [`mtls-setup.md`](./mtls-setup.md)), not via CLI
flags.

That's it. No tunnel, no overlay. The network is trusted; mTLS makes it
auditable.

### Running workers in containers

A `bernstein worker` container does not serve the task-server HTTP port, so the
image's default `HEALTHCHECK` (which probes `http://127.0.0.1:8052/health`)
never succeeds and the container reports `(unhealthy)` forever. Disable it for
worker-only containers:

```bash
docker run --health-cmd=NONE \
    ghcr.io/sipyourdrink-ltd/bernstein \
    worker --server https://central.internal:8052 --roles backend
```

In compose, set `healthcheck: { disable: true }` on the worker service.

The image runs as `USER bernstein` (uid 1000). On macOS a bind-mounted host
workspace is owned by your host uid, which the in-container user cannot write,
so mounts fail with permission errors. Add `user: "0:0"` (compose) or
`--user 0:0` (`docker run`) to the worker service on macOS, or use a named
volume instead of a bind mount.

---

## Pattern 2 - Cloudflare Tunnel

The central server runs in your office or a private VPC, and you do not
control (or do not want to touch) the firewall it sits behind. Workers run
on the public internet - contractor laptops, a separate cloud account, a
build runner. A `cloudflared` sidecar opens an outbound connection from
the central server to Cloudflare's edge, and workers reach the central
server via a public hostname under your Cloudflare zone.

```
+------------------+                                +------------------+
|  Worker laptop   |                                |  Office VPC      |
|  bernstein       |  HTTPS  +--------------+       |  +----------+    |
|  cluster worker  +-------->| Cloudflare   |<------+--+ central  |    |
|  --server https://         |  edge        |  out  |  | server   |    |
|  central.example.com       +--------------+  bound|  +----------+    |
+------------------+                                |       ^          |
                                                    |       | local    |
                                                    |  +----+------+   |
                                                    |  | cloudflared|  |
                                                    |  | sidecar    |  |
                                                    |  +-----------+   |
                                                    +------------------+
```

### What you need

1. A Cloudflare account with a zone (`example.com`).
2. A tunnel created in the Zero Trust dashboard (or via `cloudflared
   tunnel create bernstein-central`). Copy the tunnel token.
3. A public hostname routed to the tunnel - e.g.
   `central.bernstein.example.com` → `http://bernstein-central:8052`.
4. (Recommended) A Cloudflare Access policy on that hostname so only
   identified workers/users can reach it. Service tokens work well for
   headless workers.

### Files

A complete copy-paste-runnable example lives at
[`examples/cluster/cloudflared/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/examples/cluster/cloudflared):

- `config.yml` - `cloudflared` ingress config
- `Dockerfile` - sidecar image (pinned `cloudflare/cloudflared:latest`)
- `docker-compose.yml` - central + sidecar wired together

### Bring it up

```bash
# On the host running the central server
export CF_TUNNEL_TOKEN="eyJhIjoi..."        # from Cloudflare dashboard
export BERNSTEIN_CLUSTER_AUTH_SECRET="$(openssl rand -hex 32)"
cd examples/cluster/cloudflared
docker compose up -d

# Verify the tunnel is healthy
curl -fsS https://central.bernstein.example.com/health
# {"status":"ok"}
```

### Worker config

Workers don't need to know anything about Cloudflare - they point at the
public hostname like any HTTPS server.

```bash
# On the worker (laptop, separate cloud, build runner)
bernstein worker \
    --server https://central.bernstein.example.com \
    --roles backend \
    --token "$BERNSTEIN_AUTH_TOKEN"
```

If you put Cloudflare Access in front of the hostname, set:

```bash
export CF_ACCESS_CLIENT_ID="<service-token-id>"
export CF_ACCESS_CLIENT_SECRET="<service-token-secret>"
```

…and add them to the worker's HTTP headers via your environment's standard
mechanism (Bernstein passes through `CF-Access-*` headers as-is).

### Customer scenario - contractor laptop

> **Scenario:** You have three contractors building a feature against
> your internal Bernstein. They are not on the corporate VPN, you are not
> giving them VPN access, you don't want to expose port 8052 to the
> internet, and you do want a per-contractor identity you can revoke.

End-to-end:

1. **Operator (central side).** Stand up the central server behind
   `cloudflared` using the compose file in
   `examples/cluster/cloudflared/`. Create a Cloudflare Access policy
   on `central.bernstein.example.com` that requires a service token.
2. **Operator (per contractor).** In the Cloudflare dashboard, mint one
   Access service token per contractor; revoke it when they leave.
3. **Contractor.** Install Bernstein, set the two `CF_ACCESS_*` env vars
   for their service token, run `bernstein worker --server
   https://central.bernstein.example.com --roles backend`. They never
   touch your VPN.
4. **Verify.** On the central node, the `GET /cluster/status` snapshot lists the
   contractor's worker as `ONLINE`. Revoking the service token in
   Cloudflare immediately blocks them at the edge - Bernstein doesn't
   need to know.

For a regulated workload, layer mTLS underneath the tunnel so the
encryption is end-to-end and the application can verify the worker's
identity independently of Cloudflare.

---

## Pattern 3 - Tailscale overlay

Both the central server and the workers join the same tailnet and
address each other on private MagicDNS hostnames. Tailscale handles NAT
traversal and identity; Bernstein sees a flat, private network.

This is the right shape when:

- Workers are on contractor laptops *and* you don't want to manage
  Cloudflare zones.
- You already use Tailscale for other internal services.
- You want identity-on-the-network - the tailnet ACL decides who can
  even reach Bernstein, before Bernstein evaluates a JWT.

```
+----------------+         tailnet        +----------------+
|   Worker       |    100.64.x.x          |  Central       |
|   (laptop)     |<---------------------->|  server        |
|   tailscaled   |                        |  tailscaled    |
+----------------+                        +----------------+
       ^                                          ^
       |  identity: contractor@example.com        |  identity: bernstein-central
       +------------------------------------------+
                   (Tailscale ACL)
```

### What you need

1. A Tailscale account (or Headscale, ZeroTier - same shape).
2. A tagged auth key for the central server (`tag:bernstein-central`)
   and one for workers (`tag:bernstein-worker`). Reusable, ephemeral
   keys are fine.
3. An ACL that allows `tag:bernstein-worker` to talk to
   `tag:bernstein-central` on TCP/8052 - see
   [`examples/cluster/tailscale/tailscale.json`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/examples/cluster/tailscale/tailscale.json).

### Files

A complete copy-paste-runnable example lives at
[`examples/cluster/tailscale/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/examples/cluster/tailscale):

- `tailscale.json` - tailnet ACL granting only worker→central on 8052
- `docker-compose.yml` - central + tailscaled sidecar
- `bernstein.yaml` - sample config showing the tailnet hostname

### Bring it up

```bash
# Central node
export TS_AUTHKEY="tskey-auth-..."          # tag:bernstein-central
export BERNSTEIN_CLUSTER_AUTH_SECRET="$(openssl rand -hex 32)"
cd examples/cluster/tailscale
docker compose up -d

# Tailscale will publish the central server as
#   bernstein-central.tailXXXXX.ts.net
# (MagicDNS) once it's up. Verify:
tailscale status | grep bernstein-central
```

### Worker config

```bash
# On the worker
sudo tailscale up --authkey="$TS_AUTHKEY_WORKER" --advertise-tags=tag:bernstein-worker

bernstein worker \
    --server http://bernstein-central.tailXXXXX.ts.net:8052 \
    --roles backend \
    --token "$BERNSTEIN_AUTH_TOKEN"
```

The traffic stays inside the tailnet; the URL is `http://` because the
encryption is handled by WireGuard at the network layer. If you also
want application-layer mTLS for audit purposes, follow
[`mtls-setup.md`](./mtls-setup.md) on top of this - they compose.

### ACL shape

The shipped ACL is intentionally minimal:

```jsonc
{
  "tagOwners": {
    "tag:bernstein-central": ["autogroup:admin"],
    "tag:bernstein-worker":  ["autogroup:admin"]
  },
  "acls": [
    { "action": "accept",
      "src":    ["tag:bernstein-worker"],
      "dst":    ["tag:bernstein-central:8052"] }
  ]
}
```

Workers cannot reach each other on the tailnet - Bernstein's STAR
topology routes everything through the central server, so peer-to-peer
reachability would just be attack surface.

---

## Picking a pattern

```
Workers and central on the same trusted network?
  yes -> Pattern 1 (Same-VPC mTLS)
  no  -> Do you already use Cloudflare for ingress?
           yes -> Pattern 2 (Cloudflare Tunnel)
           no  -> Pattern 3 (Tailscale overlay)
```

All three patterns work with the existing `bernstein worker
--server <url>` flag. Customers don't write Bernstein-specific
networking code; they pick the tunnel/overlay that fits their
operations and point the worker at the resulting hostname.

## Out of scope

- **Peer-to-peer worker traffic.** The STAR topology routes through the
  central server. Workers don't talk to each other.
- **ZeroTier / WireGuard / Headscale.** Same shape as Tailscale; adapt
  the example accordingly.
- **Automated cert rotation.** Rotation is manual today - see the
  rotation section of `mtls-setup.md`.
- **In-cluster service mesh.** If you're already running Istio/Linkerd,
  Bernstein's plain HTTP works fine behind the mesh; you don't need
  any of the patterns above.

## Related

- [`mtls-setup.md`](./mtls-setup.md) - application-layer mutual TLS
- [`examples/cluster/cloudflared/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/examples/cluster/cloudflared)
- [`examples/cluster/tailscale/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/examples/cluster/tailscale)
- [`tests/integration/cluster/test_cluster_tunnel_smoke.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/integration/cluster/test_cluster_tunnel_smoke.py)
  - CI smoke test for Pattern 2
