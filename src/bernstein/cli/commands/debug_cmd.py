"""debug command -- generate a diagnostic bundle for bug reports.

Collects logs, config (secrets redacted), and runtime state into a
zip file suitable for attaching to GitHub issues or discussions.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import click

from bernstein.cli.helpers import console

_COLLECTION_SUMMARY = """\
Bernstein Debug Bundle Generator

This will collect:
  - bernstein.yaml config (secrets will be REDACTED)
  - Server and orchestrator logs (last 1000 lines)
  - Last 5 agent session logs (last 500 lines each)
  - Task records and archive (last 100/50 entries)
  - Platform info (OS, Python, disk space)
  - Git status and worktree list

Secrets (API keys, tokens, passwords) are automatically redacted.
No source code is included."""

_NEXT_STEPS = """\
Next steps:
  1. Open a bug report: https://github.com/chernistry/bernstein/issues/new?template=bug_report.yml
  2. Drag and drop the zip file into the "Debug bundle" field
  3. Describe what went wrong"""


# ---------------------------------------------------------------------------
# Local protocol / dataclass types so pyright is satisfied even when the
# real ``bernstein.core.observability.debug_bundle`` module is absent.
# ---------------------------------------------------------------------------


@dataclass
class _FallbackBundleConfig:
    """Minimal stand-in for BundleConfig when the real module is absent."""

    workdir: Path
    output_path: Path | None = None
    extended: bool = False


class _BundleManifestLike(Protocol):
    """Structural type describing the manifest returned by create_debug_bundle."""

    @property
    def zip_path(self) -> Path: ...

    @property
    def size_human(self) -> str: ...


def _load_bundle_module() -> tuple[
    type[Any] | None,
    Any | None,
]:
    """Try to import the real bundle module; return (BundleConfig, create_fn) or (None, None).

    Returns:
        A 2-tuple of (BundleConfig class, create_debug_bundle callable),
        or (None, None) when the module is not installed.
    """
    try:
        mod = importlib.import_module("bernstein.core.observability.debug_bundle")
        return getattr(mod, "BundleConfig", None), getattr(mod, "create_debug_bundle", None)
    except ImportError:
        return None, None


@click.command("debug")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--output", "-o", type=click.Path(), default=None, help="Output zip path")
@click.option("--extended", is_flag=True, help="Include full logs (not truncated)")
def debug_cmd(yes: bool, output: str | None, extended: bool) -> None:
    """Generate a diagnostic bundle for bug reports.

    Collects logs, config (secrets redacted), and runtime state into a
    zip file you can attach to GitHub issues or discussions.
    """
    bundle_config_cls, create_fn = _load_bundle_module()
    if create_fn is None:
        console.print(
            "[red]Error:[/red] Debug bundle module not available. "
            "Install the full bernstein package or check your installation."
        )
        raise SystemExit(1)

    # 1. Print what will be collected
    console.print()
    console.print(_COLLECTION_SUMMARY)
    console.print()

    # 2. Ask for confirmation (unless --yes)
    if not yes:
        if not click.confirm("Generate debug bundle?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            raise SystemExit(0)
        console.print()

    # 3. Build config and collect
    workdir = Path.cwd()
    output_path = Path(output) if output else None

    config_cls = bundle_config_cls or _FallbackBundleConfig
    config = config_cls(
        workdir=workdir,
        output_path=output_path,
        extended=extended,
    )

    manifest: _BundleManifestLike = create_fn(config)

    # 4. Show progress summary
    console.print()
    console.print(f"Bundle saved to: [bold]{manifest.zip_path}[/bold] ({manifest.size_human})")
    console.print()
    console.print(_NEXT_STEPS)
    console.print()
