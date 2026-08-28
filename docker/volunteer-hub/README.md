# Volunteer Hub

The volunteer hub is the HTTP surface where donor workers enroll, claim,
heartbeat, submit, and release volunteer tasks. It is a standalone FastAPI
app (`bernstein.core.volunteer.hub_app:app`) served by its own CLI subcommand,
`bernstein volunteer hub`, and runs behind a Caddy reverse proxy that
terminates TLS automatically.

## Architecture

```
  donor worker                     Caddy (TLS)         volunteer hub
        │                              │                    │
        │  POST /volunteer/enroll      │                    │
        └─────────────────────────────►│───────────────────►│  (port 8053, internal)
```

* **caddy** — public-facing HTTPS reverse proxy on ports 80/443. Handles TLS
  via Let's Encrypt (just point DNS at this server). Forwards
  `X-Forwarded-Proto: {scheme}` so the hub knows the original scheme.
* **bernstein-volunteer-hub** — the FastAPI app, bound to 127.0.0.1:8053
  inside the container. Never exposed on a host port directly.

## Setup

1. Copy the env file and fill in your domain:

   ```
   cp docker/volunteer-hub/.env.volunteer-hub.example docker/volunteer-hub/.env.volunteer-hub
   ```

   Edit `VOLUNTEER_HUB_DOMAIN` to the public hostname this hub will serve on.
   Caddy uses it for the TLS certificate. The example below uses a placeholder
   — replace it:

   ```
   VOLUNTEER_HUB_DOMAIN=volunteer.bernstein.test
   ```

2. Point that domain's DNS at this server. Caddy will obtain a certificate
   on first request.

3. Start the hub:

   ```
   docker compose -f docker/volunteer-hub/docker-compose.yaml \
     --env-file docker/volunteer-hub/.env.volunteer-hub up -d
   ```

4. Check it:

   ```
   curl -fsS https://volunteer.bernstein.test/healthz
   ```

## Public surface

* `GET /healthz` — liveness probe. Public, unauthenticated. Caddy serves it
  on the root path and the hub's healthcheck hits it internally.
* `POST /volunteer/enroll` — a worker submits its Ed25519 public key.
* `POST /volunteer/tasks/{task_id}/claim|heartbeat|submit|release` — worker
  operations, each gated by a scoped bearer token.

The full endpoint list lives in the docstring of
`bernstein.core.volunteer.hub_app`.

## Persistence

The lease store is a JSONL file on a named Docker volume
(`lease-store`, mounted at `/workspace/.sdd/runtime/volunteer`). Accepted
submissions survive `docker compose down && up` because the volume is not
removed on `down`.

The lease store is single-process only. Do not run multiple replicas or
`uvicorn --workers N>1`: leases are held in process state and an N>1
deploy would accept duplicate claims.

## Stopping

```
docker compose -f docker/volunteer-hub/docker-compose.yaml down
```

Lease data persists on the named volume; remove the volume to wipe it:

```
docker compose -f docker/volunteer-hub/docker-compose.yaml down -v
```

## Requirements

* Docker + docker compose
* A domain name (or `localhost` for local development — Caddy will issue a
  self-signed certificate for `localhost`)
* The published bernstein image (ghcr.io/sipyourdrink-ltd/bernstein). No
  Dockerfile is needed; the image ships `uvicorn` and the volunteer CLI.