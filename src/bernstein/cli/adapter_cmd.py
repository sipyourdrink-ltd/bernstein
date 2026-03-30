"""One-shot adapter smoke command."""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

import click

from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.registry import get_adapter
from bernstein.cli.helpers import console
from bernstein.core.models import ModelConfig

_DEFAULT_SMOKE_MODELS: dict[str, str] = {
    "aider": "sonnet",
    "amp": "sonnet",
    "claude": "sonnet",
    "codex": "gpt-5.4-mini",
    "cursor": "sonnet",
    "gemini": "gemini-2.5-flash",
    "kiro": "sonnet",
    "kilo": "sonnet",
    "opencode": "gpt-5.4-mini",
    "qwen": "qwen-coder",
    "roo-code": "sonnet",
}


@click.command("test-adapter")
@click.argument("adapter_name")
@click.option("--model", default=None, help="Model to use for the smoke run.")
@click.option("--prompt", default="Reply with a one-line readiness confirmation.", show_default=True)
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Working directory for the spawned adapter.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def test_adapter(adapter_name: str, model: str | None, prompt: str, workdir: Path, as_json: bool) -> None:
    """Spawn a single headless adapter run and verify it stays alive past startup."""
    resolved_model = model or _DEFAULT_SMOKE_MODELS.get(adapter_name, "sonnet")
    adapter = get_adapter(adapter_name)
    session_id = f"{adapter_name}-smoke-{int(time.time())}"
    result: Any = None

    try:
        result = adapter.spawn(
            prompt=prompt,
            workdir=workdir.resolve(),
            model_config=ModelConfig(model=resolved_model, effort="medium"),
            session_id=session_id,
            timeout_seconds=30,
        )
        payload = {
            "ok": True,
            "adapter": adapter_name,
            "model": resolved_model,
            "session_id": session_id,
            "pid": result.pid,
            "log_path": str(result.log_path),
        }
        if as_json:
            click.echo(json.dumps(payload, indent=2))
        else:
            console.print(
                f"[green]Adapter OK:[/green] {adapter_name} model={resolved_model} pid={result.pid} "
                f"log={result.log_path}"
            )
    except Exception as exc:
        payload = {
            "ok": False,
            "adapter": adapter_name,
            "model": resolved_model,
            "session_id": session_id,
            "error": str(exc),
        }
        if as_json:
            click.echo(json.dumps(payload, indent=2))
        else:
            console.print(f"[red]Adapter failed:[/red] {adapter_name} model={resolved_model} error={exc}")
        raise SystemExit(1) from exc
    finally:
        if result is not None:
            CLIAdapter.cancel_timeout(result)
            with contextlib.suppress(Exception):
                adapter.kill(result.pid)
