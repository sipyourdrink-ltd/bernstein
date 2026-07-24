"""CLI commands for Bernstein Cloud (hosted orchestration on Cloudflare).

Provides ``bernstein cloud`` subcommands for:
- login/logout to bernstein.run
- run orchestration in the cloud
- check status of cloud runs
- manage cloud configuration
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import click
import httpx

logger = logging.getLogger(__name__)

_DEFAULT_CLOUD_URL = "https://api.bernstein.run"
_CONFIG_DIR = Path.home() / ".config" / "bernstein"
_TOKEN_FILE = _CONFIG_DIR / "cloud-token.json"
_MSG_NOT_LOGGED_IN = "Not logged in."
_RUNS_PATH = "/runs"


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("cloud")
def cloud_group() -> None:
    """Manage Bernstein Cloud: hosted orchestration on Cloudflare."""


# ---------------------------------------------------------------------------
# cloud login
# ---------------------------------------------------------------------------


@cloud_group.command("login")
@click.option(
    "--api-key",
    envvar="BERNSTEIN_CLOUD_API_KEY",
    help="API key for bernstein.run",
)
@click.option(
    "--url",
    default=_DEFAULT_CLOUD_URL,
    help="Cloud API URL",
)
def cloud_login(api_key: str | None, url: str) -> None:
    """Authenticate with Bernstein Cloud."""
    if not api_key:
        api_key = click.prompt("Enter your Bernstein Cloud API key", hide_input=True)
    _save_token(api_key, url)
    click.echo("Authenticated with Bernstein Cloud.")
    click.echo(
        "Note: the hosted cloud service (api.bernstein.run) is experimental and "
        "may be unavailable.",
        err=True,
    )


# ---------------------------------------------------------------------------
# cloud logout
# ---------------------------------------------------------------------------


@cloud_group.command("logout")
def cloud_logout() -> None:
    """Remove stored cloud credentials."""
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()
        click.echo("Logged out from Bernstein Cloud.")
    else:
        click.echo(_MSG_NOT_LOGGED_IN)


# ---------------------------------------------------------------------------
# cloud run
# ---------------------------------------------------------------------------


@cloud_group.command("run")
@click.argument("goal")
@click.option("--max-agents", default=3, help="Max parallel agents")
@click.option("--model", default="auto", help="Model preference")
@click.option("--budget", default=10.0, help="Max cost in USD")
@click.option("--wait/--no-wait", default=True, help="Wait for completion")
def cloud_run(goal: str, max_agents: int, model: str, budget: float, *, wait: bool) -> None:
    """Run orchestration in Bernstein Cloud."""
    token = _load_token()
    if not token:
        click.echo("Not logged in. Run 'bernstein cloud login' first.", err=True)
        sys.exit(1)

    payload = {
        "goal": goal,
        "max_agents": max_agents,
        "model": model,
        "budget": budget,
    }
    resp = _cloud_request("POST", _RUNS_PATH, token, json=payload)
    resp.raise_for_status()
    run_id = resp.json().get("id", "unknown")
    click.echo(f"Started cloud run: {run_id}")

    if wait:
        click.echo("Waiting for completion...")
        poll_resp = _cloud_request("GET", f"/runs/{run_id}", token)
        poll_resp.raise_for_status()
        result = poll_resp.json()
        click.echo(f"Status: {result.get('status', 'unknown')}")


# ---------------------------------------------------------------------------
# cloud status
# ---------------------------------------------------------------------------


@cloud_group.command("status")
@click.argument("run_id", required=False)
def cloud_status(run_id: str | None) -> None:
    """Show status of cloud runs."""
    token = _load_token()
    if not token:
        click.echo(_MSG_NOT_LOGGED_IN, err=True)
        sys.exit(1)

    path = f"{_RUNS_PATH}/{run_id}" if run_id else _RUNS_PATH
    resp = _cloud_request("GET", path, token)
    resp.raise_for_status()
    click.echo(json.dumps(resp.json(), indent=2))


# ---------------------------------------------------------------------------
# cloud runs
# ---------------------------------------------------------------------------


@cloud_group.command("runs")
@click.option("--limit", default=10, help="Number of recent runs")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def cloud_runs(limit: int, *, output_json: bool) -> None:
    """List recent cloud runs."""
    token = _load_token()
    if not token:
        click.echo(_MSG_NOT_LOGGED_IN, err=True)
        sys.exit(1)

    resp = _cloud_request("GET", _RUNS_PATH, token, params={"limit": limit})
    resp.raise_for_status()
    data = resp.json()

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        runs = data if isinstance(data, list) else data.get("runs", [])
        for run in runs:
            click.echo(f"{run.get('id', '?')}  {run.get('status', '?')}  {run.get('goal', '')}")


# ---------------------------------------------------------------------------
# cloud cost
# ---------------------------------------------------------------------------


@cloud_group.command("cost")
@click.option("--period", default="current", help="Billing period (current, YYYY-MM)")
def cloud_cost(period: str) -> None:
    """Show cloud usage and costs."""
    token = _load_token()
    if not token:
        click.echo(_MSG_NOT_LOGGED_IN, err=True)
        sys.exit(1)

    resp = _cloud_request("GET", "/billing/usage", token, params={"period": period})
    resp.raise_for_status()
    data = resp.json()
    click.echo(f"Period: {data.get('period', period)}")
    click.echo(f"Total cost: ${data.get('total_cost', 0):.2f}")
    click.echo(f"Runs: {data.get('run_count', 0)}")


# ---------------------------------------------------------------------------
# cloud init
# ---------------------------------------------------------------------------

_WRANGLER_TOML_TEMPLATE = """\
name = "{worker_name}"
main = "src/index.js"
compatibility_date = "2024-01-01"

[vars]
BERNSTEIN_SERVER_URL = "http://127.0.0.1:8052"

# Free-tier compatible by default: no paid bindings are declared. Add your own
# below as needed. Note that Queues require a Workers Paid plan.
# [[kv_namespaces]]
# binding = "TASKS"
# id = "your-kv-namespace-id"
"""

# Minimal runnable worker so ``main`` resolves and ``wrangler deploy`` succeeds.
_WORKER_JS_TEMPLATE = """\
/**
 * Minimal Bernstein agent worker.
 *
 * Free-tier compatible: a single fetch handler with no paid bindings. Replace
 * the body with your agent logic. This file is the `main` entry point named in
 * wrangler.toml, so `wrangler deploy` resolves without an "entry-point not
 * found" error.
 */
export default {
  async fetch(request, env) {
    const body = {
      service: "bernstein-agent",
      server: env.BERNSTEIN_SERVER_URL ?? "",
      message: "Bernstein agent worker is running.",
    };
    return new Response(JSON.stringify(body), {
      headers: { "content-type": "application/json" },
    });
  },
};
"""


@cloud_group.command("init")
@click.option("--worker-name", default="bernstein-agent", show_default=True, help="Cloudflare Worker name.")
@click.option("--output", "-o", default="wrangler.toml", show_default=True, help="Output path for wrangler.toml.")
def cloud_init(worker_name: str, output: str) -> None:
    """Scaffold a deployable wrangler.toml and its worker entry point.

    \b
      bernstein cloud init                        # write wrangler.toml + src/index.js
      bernstein cloud init --output deploy/wrangler.toml
    """
    out_path = Path(output)
    if out_path.exists():
        click.echo(f"{output} already exists. Remove it first or use --output to specify a different path.", err=True)
        raise SystemExit(1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_WRANGLER_TOML_TEMPLATE.format(worker_name=worker_name), encoding="utf-8")
    click.echo(f"Created {output}")

    # Scaffold the worker named by ``main`` so the toml points at a real file.
    worker_path = out_path.parent / "src" / "index.js"
    if worker_path.exists():
        click.echo(f"{worker_path} already exists; left unchanged.")
    else:
        worker_path.parent.mkdir(parents=True, exist_ok=True)
        worker_path.write_text(_WORKER_JS_TEMPLATE, encoding="utf-8")
        click.echo(f"Created {worker_path}")

    click.echo("Next: set account_id in wrangler.toml, then deploy with 'npx wrangler deploy'.")


# ---------------------------------------------------------------------------
# cloud deploy
# ---------------------------------------------------------------------------


@cloud_group.command("deploy")
@click.option("--worker-name", default="bernstein-agent", help="Cloudflare Worker name")
def cloud_deploy(worker_name: str) -> None:
    """Deploy Bernstein agent Worker to your Cloudflare account."""
    click.echo(f"Deploying {worker_name}...")
    click.echo(f"Run: npx wrangler deploy --name {worker_name}")
    click.echo("Scaffold a deployable worker first with 'bernstein cloud init'.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_token(api_key: str, url: str) -> None:
    """Save cloud credentials to disk."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(
        json.dumps({"api_key": api_key, "url": url}),
        encoding="utf-8",
    )
    _TOKEN_FILE.chmod(0o600)


def _load_token() -> dict[str, str] | None:
    """Load cloud credentials from disk."""
    if not _TOKEN_FILE.exists():
        return None
    with suppress(json.JSONDecodeError, OSError):
        data: dict[str, str] = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        if "api_key" in data:
            return data
    return None


def _cloud_request(
    method: str,
    path: str,
    token: dict[str, str],
    **kwargs: Any,
) -> httpx.Response:
    """Make an authenticated request to the Bernstein Cloud API.

    Raises:
        click.ClickException: When the hosted service cannot be reached. The
            default ``api.bernstein.run`` host does not currently resolve, so a
            connection failure is reported cleanly instead of as a traceback.
    """
    url = f"{token['url']}{path}"
    headers = {
        "Authorization": f"Bearer {token['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=30) as client:
            return client.request(method, url, headers=headers, **kwargs)
    except httpx.RequestError as exc:
        raise click.ClickException(
            f"Bernstein Cloud hosted service ({token['url']}) is not reachable: {exc}. "
            "The hosted cloud API is experimental and not currently available."
        ) from exc
