"""``bernstein review-responder`` — manage the PR review responder daemon.

The CLI is intentionally thin.  Heavy logic lives in
:mod:`bernstein.core.review_responder`; this module just glues click
flags to the responder primitives and prints a status summary.

Subcommands:

* ``start`` — show the configuration the daemon would use, optionally
  arrange a tunnel via :mod:`bernstein.core.tunnels`, and (when run
  with ``--foreground``) actually serve the webhook listener.
* ``status`` — describe the persisted dedup queue for a given PR so an
  operator can see which comments have already been addressed.
* ``tick`` — run a single polling pass synchronously; useful in tests
  and as a last-resort manual trigger.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console
from bernstein.core.review_responder import (
    DedupQueue,
    PollingListener,
    ResponderConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.review_responder import ReviewComment


def _state_path() -> Path:
    """Return the dedup state path the daemon uses by default."""
    from bernstein.core.review_responder.dedup import DEFAULT_STATE_PATH

    return DEFAULT_STATE_PATH


@click.group("review-responder")
def review_responder_group() -> None:
    """Manage the PR review responder service."""


@review_responder_group.command("start")
@click.option("--repo", required=True, help="GitHub owner/repo to listen on.")
@click.option(
    "--tunnel",
    is_flag=True,
    default=False,
    help="Open a public tunnel via `bernstein tunnel start` before listening.",
)
@click.option(
    "--port",
    type=int,
    default=8053,
    show_default=True,
    help="Local TCP port the webhook listener binds to.",
)
@click.option(
    "--quiet-window",
    "quiet_window_s",
    type=float,
    default=90.0,
    show_default=True,
    help="Seconds of silence before a round is sealed.",
)
@click.option(
    "--cost-cap",
    "cost_cap_usd",
    type=float,
    default=2.50,
    show_default=True,
    help="Per-round cost cap in USD; breach posts a needs-human reply.",
)
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help="Run the FastAPI listener in the foreground (blocks).",
)
def start_cmd(
    repo: str,
    tunnel: bool,
    port: int,
    quiet_window_s: float,
    cost_cap_usd: float,
    foreground: bool,
) -> None:
    """Print the responder configuration and (optionally) serve the listener."""
    cfg = ResponderConfig(
        repo=repo,
        quiet_window_s=quiet_window_s,
        per_round_cost_cap_usd=cost_cap_usd,
        listen_port=port,
    )
    secret = os.environ.get(cfg.webhook_secret_env, "")
    console.print(f"[green]review-responder[/green] repo={repo} port={port}")
    console.print(
        f"  quiet_window={quiet_window_s:.0f}s  cost_cap=${cost_cap_usd:.2f}  "
        f"webhook_secret={'set' if secret else 'unset'}  tunnel={'yes' if tunnel else 'no'}"
    )
    if tunnel:
        console.print(
            "  [dim]hint: open the tunnel with `bernstein tunnel start "
            f"{port}` and copy the public URL into your GitHub webhook config.[/dim]"
        )
    if not foreground:
        console.print("[dim]Listener config printed; pass --foreground to actually serve.[/dim]")
        return

    if not secret:
        raise click.ClickException(f"Cannot serve without a webhook secret. Set ${cfg.webhook_secret_env}.")

    # uvicorn is imported lazily so dry-runs of this CLI don't pull it in.
    import uvicorn  # type: ignore[import-untyped]

    from bernstein.core.review_responder.webhook import WebhookListener

    queue: list[ReviewComment] = []

    def _on_payload(payload: dict[str, object]) -> None:  # pragma: no cover - exercised by integration
        from bernstein.core.review_responder import normalise_webhook_payload

        try:
            queue.append(normalise_webhook_payload(payload))
        except Exception as exc:  # pylint: disable=broad-except
            console.print(f"[yellow]Dropped malformed webhook: {exc}[/yellow]")

    listener = WebhookListener(secret=secret.encode(), on_comment=_on_payload)
    uvicorn.run(listener.app, host=cfg.listen_host, port=port, log_level="info")


@review_responder_group.command("status")
@click.option("--pr", "pr_number", type=int, default=None, help="Filter to a PR number.")
def status_cmd(pr_number: int | None) -> None:
    """Show persisted dedup state, optionally filtered by PR."""
    queue = DedupQueue(state_path=_state_path())
    rows: list[dict[str, str | int]] = []
    for cid in sorted({rec.comment_id for rec in queue._records.values()}):
        rec = queue.known(cid)
        if rec is None:
            continue
        rows.append(
            {
                "comment_id": rec.comment_id,
                "updated_at": rec.updated_at,
                "outcome": rec.outcome,
                "round_id": rec.round_id,
            }
        )
    if pr_number is not None:
        # Without storing pr_number in the dedup record we cannot filter
        # here directly — keep the option as a no-op but mention it.
        console.print(f"[dim]Note: dedup records do not carry pr_number; --pr {pr_number} is informational only.[/dim]")
    if not rows:
        console.print("[dim]No review-responder activity recorded yet.[/dim]")
        return
    console.print(json.dumps(rows, indent=2))


@review_responder_group.command("tick")
@click.option("--repo", required=True, help="GitHub owner/repo to poll.")
@click.option("--pr", "pr_numbers", multiple=True, type=int, help="Specific PR(s) to poll.")
def tick_cmd(repo: str, pr_numbers: tuple[int, ...]) -> None:
    """Run one polling pass synchronously and print the count of new comments."""
    received: list[int] = []

    def _capture(comment: ReviewComment) -> None:
        received.append(comment.comment_id)

    listener = PollingListener(
        repo=repo,
        pr_numbers=pr_numbers if pr_numbers else None,
        on_comment=_capture,
    )
    n = listener.tick()
    console.print(f"[green]review-responder tick[/green] new_comments={n}")
    if received:
        console.print(json.dumps(received))
