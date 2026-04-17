# Cloud CLI

**Module:** `bernstein.cli.commands.cloud_cmd`

The `bernstein cloud` command group manages hosted orchestration on Cloudflare. It provides authentication, remote run management, cost reporting, and worker deployment.

---

## Commands

### `bernstein cloud login`

Authenticate with Bernstein Cloud (api.bernstein.run).

```bash
# Interactive prompt for API key
bernstein cloud login

# Pass key directly
bernstein cloud login --api-key YOUR_KEY

# Use environment variable
export BERNSTEIN_CLOUD_API_KEY="your-key"
bernstein cloud login

# Custom cloud API URL
bernstein cloud login --url https://custom.bernstein.example.com
```

Credentials are stored in `~/.config/bernstein/cloud-token.json` with mode `0600`.

---

### `bernstein cloud logout`

Remove stored cloud credentials.

```bash
bernstein cloud logout
```

---

### `bernstein cloud run`

Start an orchestration run in Bernstein Cloud.

```bash
bernstein cloud run "Add OAuth2 authentication to the API"

# With options
bernstein cloud run "Refactor the auth module" \
  --max-agents 5 \
  --model opus \
  --budget 25.00 \
  --no-wait
```

| Option | Default | Description |
|--------|---------|-------------|
| `GOAL` | (required, positional) | Task description |
| `--max-agents` | `3` | Maximum parallel agents |
| `--model` | `"auto"` | Model preference |
| `--budget` | `10.0` | Maximum cost in USD |
| `--wait / --no-wait` | `--wait` | Wait for completion or return immediately |

When `--wait` is active (default), the CLI polls for completion and prints the final status.

---

### `bernstein cloud status`

Show status of a specific cloud run or all runs.

```bash
# Status of a specific run
bernstein cloud status run-abc123

# Status of all runs
bernstein cloud status
```

Output is formatted as JSON.

---

### `bernstein cloud runs`

List recent cloud runs.

```bash
# Default: last 10 runs
bernstein cloud runs

# More runs, JSON output
bernstein cloud runs --limit 50 --json
```

| Option | Default | Description |
|--------|---------|-------------|
| `--limit` | `10` | Number of recent runs to show |
| `--json` | `False` | Output raw JSON instead of table |

---

### `bernstein cloud cost`

Show cloud usage and costs for a billing period.

```bash
# Current period
bernstein cloud cost

# Specific month
bernstein cloud cost --period 2026-04
```

| Option | Default | Description |
|--------|---------|-------------|
| `--period` | `"current"` | Billing period (`current` or `YYYY-MM`) |

Output includes total cost, run count, and period.

---

### `bernstein cloud deploy`

Deploy the Bernstein agent Worker to your Cloudflare account.

```bash
bernstein cloud deploy

# Custom worker name
bernstein cloud deploy --worker-name my-bernstein-worker
```

| Option | Default | Description |
|--------|---------|-------------|
| `--worker-name` | `"bernstein-agent"` | Cloudflare Worker script name |

!!! note "Manual step"
    This command prints the wrangler deploy command and points you to the deployment template. Run the printed command to complete deployment.

---

## Authentication flow

1. `bernstein cloud login` prompts for an API key (or reads from `--api-key` / `BERNSTEIN_CLOUD_API_KEY`).
2. The key and API URL are saved to `~/.config/bernstein/cloud-token.json`.
3. All subsequent `bernstein cloud` commands read the token from this file.
4. Requests are authenticated with `Authorization: Bearer <api_key>` headers.

---

## Cloud API base URL

The default cloud API is `https://api.bernstein.run`. Override it with:

```bash
bernstein cloud login --url https://your-instance.example.com
```

This is stored alongside the API key in the token file.
