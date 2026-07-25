# Cluster deployment patterns

Bernstein's cluster mode ships two topologies, selected by
`cluster.topology` in `bernstein.yaml`:

| Topology | Shape | Arbiter |
|---|---|---|
| `star` (default) | One central server, N workers | The central server's node registry and `POST /cluster/steal` |
| `mesh` (opt-in) | Peers, no central node | A signed, Merkle-chained claim journal every node appends to |

**STAR** is what the three network patterns below are about. The central
server runs the orchestrator, the API, and the task store; workers register,
heartbeat, and pull tasks for the roles they advertise. Everything in this
page up to [MESH](#mesh-leaderless-topology) describes STAR.

**MESH** removes the central node. See
[MESH: leaderless topology](#mesh-leaderless-topology) below.

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

## Authentication

Cluster mode enables bearer auth on the central server's API, so a worker
must present a token to register, heartbeat, and pull tasks. **One token
covers all three** - the same value the central node accepts for its API also
authenticates node join.

Set the token on the central node with either of:

- `BERNSTEIN_AUTH_TOKEN` - the API bearer token, or
- `BERNSTEIN_CLUSTER_AUTH_SECRET` - a dedicated cluster secret.

Pass the **same value** to each worker with `--token` (or export
`BERNSTEIN_AUTH_TOKEN` in the worker's environment). Copy it to the worker
host out of band (scp, your secrets manager, etc.).

If you start the central node with `bernstein run --remote` and set neither,
a token is auto-generated and written, mode `0600`, to
`.sdd/runtime/auth.token`; read it from there and distribute it. The startup
log names that path but never prints the value.

Read-only status checks need the token too:

```bash
curl -H "Authorization: Bearer $BERNSTEIN_AUTH_TOKEN" \
    http://central:8052/cluster/status
```

A worker that omits the token, or presents one the central node does not
accept, now fails fast with an actionable error instead of retrying the same
rejected request every 5 s.

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

# Set the shared worker token (any strong random value) and enable cluster
# mode, then bind to all interfaces so workers can reach it.
export BERNSTEIN_AUTH_TOKEN="$(openssl rand -hex 32)"
BERNSTEIN_BIND_HOST=0.0.0.0 BERNSTEIN_CLUSTER_ENABLED=1 bernstein start
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
# --token is the same value set as BERNSTEIN_AUTH_TOKEN on the central node.
bernstein worker \
    --server https://central.internal:8052 \
    --roles backend \
    --token "$BERNSTEIN_AUTH_TOKEN"
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

**The worker's workspace must be a git checkout of the target repo.** Each
claimed task runs in a git worktree created under the workspace, so a worker
started against a non-git `/workspace` refuses to start (workspace preflight,
#3018) instead of claiming tasks it cannot run. The published image does *not*
ship a checkout at `/workspace`; bind-mount the repo (`-v /path/to/repo:/workspace`)
or clone it in an init step so `/workspace` is a git work tree with at least one
commit before `bernstein worker` starts. See
[`operations/cluster-mode.md`](../operations/cluster-mode.md#worker-setup) for
the full requirement.

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
# --token is the value the central node sets as BERNSTEIN_CLUSTER_AUTH_SECRET,
# copied to this host out of band.
bernstein worker \
    --server https://central.bernstein.example.com \
    --roles backend \
    --token "$BERNSTEIN_CLUSTER_AUTH_SECRET"
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
4. **Verify.** The cluster status snapshot lists the contractor's worker as
   `ONLINE`. The endpoint requires the bearer token:

   ```bash
   curl -H "Authorization: Bearer $BERNSTEIN_CLUSTER_AUTH_SECRET" \
       https://central.bernstein.example.com/cluster/status
   ```

   Revoking the service token in Cloudflare immediately blocks them at the
   edge - Bernstein doesn't need to know.

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

# --token is the value the central node sets as BERNSTEIN_CLUSTER_AUTH_SECRET,
# copied to this host out of band.
bernstein worker \
    --server http://bernstein-central.tailXXXXX.ts.net:8052 \
    --roles backend \
    --token "$BERNSTEIN_CLUSTER_AUTH_SECRET"
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

Under STAR, workers do not need to reach each other on the tailnet: the
topology routes everything through the central server, so peer-to-peer
reachability would just be attack surface. Under MESH the opposite holds -
peers must reach each other's `/cluster/claims/gossip`; see the ACL note in
[MESH: leaderless topology](#mesh-leaderless-topology).

---

## MESH: leaderless topology

STAR's central server is a single point of failure *and* a single point of
trust: every assignment decision passes through one node, and its assignment
log is private to it. MESH removes both. There is no central node, no
`NodeRegistry`, and no `POST /cluster/steal`.

The arbiter is a **signed, append-only, Merkle-chained claim journal**. Every
self-claim, release, renewal, expiry, supersession, and fork observation is a
hash-chained receipt, Ed25519-signed with the node's install identity and
anchored into the HMAC audit chain. Two nodes folding the same ordered receipt
set produce byte-identical state, so they agree on who holds what without
either one being in charge.

### Configuration

```yaml
cluster:
  enabled: true
  topology: mesh
  auth_token: "${BERNSTEIN_CLUSTER_SECRET}"
  gossip_peers:
    - https://node-b.internal:8052
    - https://node-c.internal:8052
  gossip_peer_keys:                 # required to fold any peer's receipts
    "8Qm...node-b-thumbprint": |
      -----BEGIN PUBLIC KEY-----
      MCowBQYDK2VwAyEA...
      -----END PUBLIC KEY-----
    "Lk4...node-c-thumbprint": "hDq7...base64url-x"
  claim_lease_ttl_s: 300            # lease granted to a successful self-claim
  claim_journal_path: null          # default: .sdd/cluster/claim_journal.jsonl
```

`gossip_peers`, `gossip_peer_keys`, and `claim_journal_path` are rejected at
seed-load time when `topology` is not `mesh`, so a half-migrated config fails at
boot rather than silently running STAR.

### Peer identity: pinning is required, not optional

**Default: a MESH node folds only receipts signed by a key it has pinned.** A
node with no `gossip_peer_keys` accepts gossip from nobody. This is the
deliberate default — the safe posture is the one you get without configuring
anything, and opening the boundary is an explicit act.

The reason is that the gossip route authenticates the *sender's cluster
credential*, and the receipt's Ed25519 signature authenticates the *bytes*.
Neither binds the `node_id` a receipt declares to the key that signed it.
Without a pin, any holder of the cluster token can gossip receipts naming any
node, and the journal — the arbiter the whole topology rests on — records a
well-formed claim attributed to a node that never issued it.

A pin is checkable rather than merely declared. A MESH `node_id` *is* the
RFC 7638 thumbprint of that node's claim-signing key, so the seed loader
recomputes the thumbprint of each pinned key and refuses to boot if it does not
reproduce the id it is filed under. You cannot pin the wrong key to a node id.

| Config | Result |
|---|---|
| No `gossip_peers`, no `gossip_peer_keys` | Single-node MESH. Self-claims work; inbound gossip is rejected. Logged at startup. |
| `gossip_peers` set, `gossip_peer_keys` empty | **Seed load fails.** Outbound gossip with nothing folded back is a silent one-way partition. |
| `gossip_peer_keys` whose thumbprint ≠ its `node_id` | **Seed load fails**, naming the identity the key actually has. |
| Both set and consistent | Receipts from pinned peers fold; everything else is rejected with `no trusted key pinned for node …`. |

Each node also pins **its own** key to its own `node_id`, always. A peer
therefore cannot gossip receipts forged in this node's name, and a node that
lost its journal can still fold its own history back from a peer.

Getting the values:

```bash
# On each peer - the id it will declare, and the key that proves it.
bernstein cluster claims head          # any receipt's node_id is the peer's id
cat .sdd/cluster/identity/claim_signing.pub
```

The key may be given as the SPKI PEM above, as an OKP JWK mapping, or as the
bare base64url `x` member — all three normalise to the same pin.

Pinning is symmetric: for A and B to converge, A must pin B's key *and* B must
pin A's. Rotating a node's claim-signing identity changes its `node_id`, so a
rotation is a config change on every peer, not a silent re-trust.

> **Upgrading an existing MESH fleet.** Before this default, gossip was
> accepted under whatever key a receipt carried. After it, an unpinned peer's
> receipts are rejected with a reason in the gossip response. Add
> `gossip_peer_keys` on every node before rolling out, or the fleet stops
> converging.

### How a claim resolves

1. A node appends a signed `claim` receipt for `(tracker, ticket_id, role)`.
2. It reconciles: any key with more than one live claim gets a `supersede`
   receipt naming the winner.
3. The winner is the claim with the **lexicographically lowest `entry_hash`**.

That rule is a total order over content hashes. It does not read a clock and
does not favour a node id, so every observer picks the same winner regardless
of the order receipts arrived in. The loser holds a chain-anchored `supersede`
receipt naming the winner, so "why did I not get this task" is answerable from
the journal alone.

### Gossip

Nodes push receipts to their peers with `POST /cluster/claims/gossip`. A
receiving node folds a receipt only after **all three** of the pinned peer key,
the Ed25519 signature, and the chain link verify. A receipt that does not extend
the local head is not merged: it produces a signed `fork` receipt carrying the
divergence entry index, which `verify` and the gossip response both surface. A
partition is reported, never silently reconciled.

Gossip rides the cluster transport and reuses the node-heartbeat auth scope,
so the same bearer credential a worker uses to join covers it. On a Tailscale
overlay, the peers ACL becomes symmetric:

```json
{ "action": "accept",
  "src":    ["tag:bernstein-mesh"],
  "dst":    ["tag:bernstein-mesh:8052"] }
```

### Reading and verifying the journal

```bash
bernstein cluster claims log      # every receipt, in chain order
bernstein cluster claims head     # current head hash + entry count
bernstein cluster claims verify   # offline replay
```

`verify` needs no live node. It replays the journal file, confirming every
`prev_entry_hash` link, every recomputed `entry_hash`, every Ed25519 node
signature, and every audit-chain anchor, then prints the head hash. A flipped
byte or an inserted receipt fails at the exact entry index.

| Exit code | Meaning |
|---|---|
| 0 | Intact, no fork |
| 1 | Integrity failure - the failing entry index is printed |
| 2 | Intact but forked - the divergence entry index is printed |

Use `--journal <path>` to verify a journal copied off a machine that is gone,
and `--no-check-anchors` when the audit chain is not available alongside it.

### Operational notes

- **Leases.** A hold whose `claim_lease_ttl_s` has elapsed can be retired by
  *any* node observing it - there is no central sweep. The `expire` receipt is
  signed by the observing node and names the retired claim as referenced data,
  so the `node_id` pinning above still finds every receipt signed by the node it
  says it is from.
- **STAR is unaffected.** A STAR deployment never materialises a journal, never
  provisions a MESH signing identity, and answers `409` on the gossip route.
- **Shared filesystem.** When peers share one filesystem, they can append to
  one journal file directly; cross-process appends serialise under an exclusive
  advisory lock, so the chain stays linear.

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

- **Peer-to-peer worker traffic under STAR.** The STAR topology routes
  through the central server; STAR workers don't talk to each other. This
  scopes to STAR only - under MESH, peers exchange signed claim receipts
  directly and there is no central server to route through.
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
