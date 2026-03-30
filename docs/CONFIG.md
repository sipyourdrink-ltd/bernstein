# Bernstein Configuration Reference

## Runtime Bridges

A **RuntimeBridge** connects Bernstein to an external execution environment
where CLI agents can run — cloud sandboxes, container runtimes, Kubernetes
pods, etc.  Bridges are configured in `.bernstein/config.toml` (or
`~/.bernstein/config.toml` for global defaults) under `[bridges.<name>]`
sections.

### BridgeConfig schema

| Field             | Type     | Required | Default   | Description                                                 |
|-------------------|----------|----------|-----------|-------------------------------------------------------------|
| `bridge_type`     | `str`    | yes      | —         | Bridge implementation key, e.g. `"openclaw"`, `"k8s"`       |
| `endpoint`        | `str`    | yes      | —         | Base URL or socket address of the runtime API               |
| `api_key`         | `str`    | no       | `""`      | Credential for authenticating with the runtime (never log)  |
| `timeout_seconds` | `int`    | no       | `30`      | Per-request HTTP timeout                                    |
| `max_log_bytes`   | `int`    | no       | `1048576` | Maximum bytes returned by a single `logs()` call (1 MiB)    |
| `extra`           | `dict`   | no       | `{}`      | Bridge-specific options (see per-bridge docs below)         |

### Example — OpenClaw bridge

```toml
[bridges.openclaw]
bridge_type     = "openclaw"
endpoint        = "https://api.openclaw.io"
api_key         = "${OPENCLAW_API_KEY}"   # resolved from env at runtime
timeout_seconds = 60
max_log_bytes   = 2097152                 # 2 MiB

[bridges.openclaw.extra]
region        = "us-east-1"
sandbox_class = "small"
pull_policy   = "if_absent"
```

### Environment variable substitution

Any field value of the form `"${VAR}"` is replaced with `os.environ["VAR"]`
at bridge construction time.  If the variable is unset and no default is
provided, bridge initialisation raises a `BridgeError`.

### OpenClaw extra options

| Key             | Type  | Default       | Description                                              |
|-----------------|-------|---------------|----------------------------------------------------------|
| `region`        | `str` | `"us-east-1"` | OpenClaw datacenter region                               |
| `sandbox_class` | `str` | `"small"`     | Compute tier: `"small"`, `"medium"`, `"large"`           |
| `pull_policy`   | `str` | `"if_absent"` | Image pull policy: `"always"` or `"if_absent"`           |

---

## Task Server

The internal task server listens on `http://127.0.0.1:8052` by default.
Override with `BERNSTEIN_SERVER_HOST` / `BERNSTEIN_SERVER_PORT` environment
variables.

| Env var                  | Default         | Description                    |
|--------------------------|-----------------|--------------------------------|
| `BERNSTEIN_SERVER_HOST`  | `127.0.0.1`     | Bind address                   |
| `BERNSTEIN_SERVER_PORT`  | `8052`          | Bind port                      |
| `BERNSTEIN_STATE_DIR`    | `.sdd/`         | Root for all file-based state  |
| `BERNSTEIN_LOG_LEVEL`    | `INFO`          | Python logging level           |
