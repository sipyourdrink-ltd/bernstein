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
from pathlib import Path
from typing import Any

import click
import httpx

logger = logging.getLogger(__name__)

_DEFAULT_CLOUD_URL = "https://api.bernstein.run"
_CONFIG_DIR = Path.home() / ".config" / "bernstein"
_TOKEN_FILE = _CONFIG_DIR / "cloud-token.json"


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("cloud")
def cloud_group() -> None:
    """Manage Bernstein Cloud — hosted orchestration on Cloudflare."""


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
        click.echo("Not logged in.")


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
    resp = _cloud_request("POST", "/runs", token, json=payload)
    resp.raise_for_status()
    data = resp.json()
    run_id = data.get("id", "unknown")
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
        click.echo("Not logged in.", err=True)
        sys.exit(1)

    path = f"/runs/{run_id}" if run_id else "/runs"
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
        click.echo("Not logged in.", err=True)
        sys.exit(1)

    resp = _cloud_request("GET", "/runs", token, params={"limit": limit})
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
        click.echo("Not logged in.", err=True)
        sys.exit(1)

    resp = _cloud_request("GET", "/billing/usage", token, params={"period": period})
    resp.raise_for_status()
    data = resp.json()
    click.echo(f"Period: {data.get('period', period)}")
    click.echo(f"Total cost: ${data.get('total_cost', 0):.2f}")
    click.echo(f"Runs: {data.get('run_count', 0)}")


# ---------------------------------------------------------------------------
# cloud deploy
# ---------------------------------------------------------------------------


@cloud_group.command("deploy")
@click.option("--worker-name", default="bernstein-agent", help="Cloudflare Worker name")
def cloud_deploy(worker_name: str) -> None:
    """Deploy Bernstein agent Worker to your Cloudflare account."""
    click.echo(f"Deploying {worker_name}...")
    click.echo(f"Run: npx wrangler deploy --name {worker_name}")
    click.echo("See templates/bernstein-cloud/wrangler.toml for the deployment template.")


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
    try:
        data: dict[str, str] = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        if "api_key" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _cloud_request(
    method: str,
    path: str,
    token: dict[str, str],
    **kwargs: Any,
) -> httpx.Response:
    """Make authenticated request to Bernstein Cloud API."""
    url = f"{token['url']}{path}"
    headers = {
        "Authorization": f"Bearer {token['api_key']}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30) as client:
        return client.request(method, url, headers=headers, **kwargs)
