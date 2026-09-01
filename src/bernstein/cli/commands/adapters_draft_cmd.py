"""``bernstein adapters draft`` - draft a candidate profile from a live probe.

Closes the caller gap left by issue #3763: :func:`bernstein.adapters.draft.
draft_from_evidence` had no path from an installed CLI to a drafted profile
outside its own test file. This command supplies that path end to end - a
real probe, a draft, an operator confirmation naming the exact argv the
draft would invoke, and a persisted plain-YAML record a later step can read
back.

Nothing is written until the operator confirms (or passes ``--yes``); a
declined or refused draft leaves ``--out-dir`` untouched.

Exit codes:

* ``0`` - drafted and persisted.
* ``1`` - the operator declined the confirmation.
* ``2`` - drafting refused: a ``--require``d field had no evidence-backed
  value. The message names the missing field.

Refs: #3763.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
import yaml

from bernstein.adapters.draft import Draft, read_draft_document, write_draft_yaml
from bernstein.adapters.onboarding import draft_from_probe

if TYPE_CHECKING:
    from collections.abc import Callable

#: Default location for raw probe evidence, relative to the repo root.
DEFAULT_EVIDENCE_DIR = Path(".sdd") / "adapters" / "evidence"
#: Default location for persisted draft documents, relative to the repo root.
DEFAULT_DRAFTS_DIR = Path(".sdd") / "adapters" / "drafts"

#: Fields drafting can be asked to require; see ``draft_from_evidence``.
_DRAFT_REQUIRE_CHOICES = ("model_flag", "prompt_flag")


def _render_preview(draft: Draft, *, model: str, prompt: str) -> str:
    """Human-readable preview of the exact argv a drafted invocation would run.

    Shown to the operator before anything is persisted, per the issue's own
    "What should become true": *"The operator reviews one confirmation
    showing the exact argv the drafted profile will invoke before anything
    is accepted."*
    """
    invocation = draft.invocation
    argv = invocation.build_argv(prompt=prompt, model=model)
    lines = [
        f"binary:       {invocation.binary}",
        f"subcommands:  {' '.join(invocation.subcommands) or '(none)'}",
        f"model_flag:   {invocation.model_flag or '(none)'}",
        f"prompt_flag:  {invocation.prompt_flag or '(positional)'}",
        f"extra_args:   {' '.join(invocation.extra_args) or '(none)'}",
        f"argv:         {' '.join(argv)}",
    ]
    if draft.evidence_byte_range is not None:
        start, end = draft.evidence_byte_range
        lines.append(f"provenance:   model flag at evidence bytes [{start}, {end})")
    else:
        lines.append("provenance:   no evidence byte range recorded")
    return "\n".join(lines)


def _confirm_default(preview: str) -> bool:
    """Default confirmation gate: show the preview, then prompt."""
    click.echo(preview)
    return click.confirm("Persist this drafted profile?", default=False)


def _execute_draft(
    binary: str,
    *,
    evidence_dir: Path,
    out_dir: Path,
    required_fields: set[str],
    preview_model: str,
    preview_prompt: str,
    assume_yes: bool,
    confirm: Callable[[str], bool] = _confirm_default,
) -> tuple[int, Path | None]:
    """Draft, preview, confirm, and persist; return ``(exit_code, written_path)``.

    Split out from the Click command so tests exercise the full pipeline - a
    real probe subprocess and a real file write - without a TTY behind the
    confirmation prompt. ``out_dir`` is touched only on a confirmed,
    successful draft; a decline or a refusal leaves it exactly as found.
    """
    try:
        draft = draft_from_probe(binary, evidence_dir, required_fields=required_fields)
    except ValueError as exc:
        click.echo(f"drafting refused: {exc}", err=True)
        return 2, None

    preview = _render_preview(draft, model=preview_model, prompt=preview_prompt)
    if assume_yes:
        click.echo(preview)
    elif not confirm(preview):
        click.echo("Aborted; nothing written.")
        return 1, None

    target = out_dir / f"{Path(binary).name}.yaml"
    write_draft_yaml(draft, target)
    # Read the persisted document back rather than trusting the in-memory
    # draft, so the confirmation message reflects what actually landed on
    # disk - the artefact a later run/step reads, not what this process
    # merely held in memory.
    written = read_draft_document(target)
    flag_count = len(written.get("contract", {}).get("required_flags", []))
    click.echo(f"Wrote drafted profile to {target} ({flag_count} evidence-backed flag(s))")
    return 0, target


@click.command("draft")
@click.argument("binary")
@click.option(
    "--evidence-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Directory receiving the raw probe evidence (default: {DEFAULT_EVIDENCE_DIR}).",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Directory receiving the persisted draft YAML (default: {DEFAULT_DRAFTS_DIR}).",
)
@click.option(
    "--require",
    "required",
    multiple=True,
    type=click.Choice(_DRAFT_REQUIRE_CHOICES),
    help="Field drafting must resolve from evidence or refuse, by name. Repeatable.",
)
@click.option(
    "--preview-model",
    default="<model>",
    show_default=True,
    help="Placeholder model id shown in the argv preview.",
)
@click.option(
    "--preview-prompt",
    default="<prompt>",
    show_default=True,
    help="Placeholder prompt shown in the argv preview.",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt and persist immediately.",
)
def adapters_draft_cmd(
    binary: str,
    evidence_dir: Path | None,
    out_dir: Path | None,
    required: tuple[str, ...],
    preview_model: str,
    preview_prompt: str,
    assume_yes: bool,
) -> None:
    """Probe BINARY and draft a candidate capability profile plus contract.

    Runs a real probe against BINARY, drafts an InvocationSpec-shaped
    profile from its --help capture, and shows the operator the exact argv
    the draft would invoke. Nothing is written until the operator confirms
    (or --yes is passed).
    """
    rc, _target = _execute_draft(
        binary,
        evidence_dir=evidence_dir if evidence_dir is not None else DEFAULT_EVIDENCE_DIR,
        out_dir=out_dir if out_dir is not None else DEFAULT_DRAFTS_DIR,
        required_fields=set(required),
        preview_model=preview_model,
        preview_prompt=preview_prompt,
        assume_yes=assume_yes,
    )
    sys.exit(rc)


def register_adapters_draft(group: click.Group) -> None:
    """Attach ``draft`` and ``drafts`` commands to an existing ``adapters`` group."""
    group.add_command(adapters_draft_cmd, "draft")
    group.add_command(drafts_group)


@click.group("drafts")
def drafts_group() -> None:
    """List and inspect persisted draft profiles."""


@drafts_group.command("list")
@click.option(
    "--drafts-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Directory containing draft YAML files (default: {DEFAULT_DRAFTS_DIR}).",
)
def list_drafts(drafts_dir: Path | None) -> None:
    """List all persisted draft profiles in the drafts directory.

    Shows the binary name (used as the profile key) and the source file
    for each draft discovered in the drafts directory.
    """
    target_dir = drafts_dir if drafts_dir is not None else DEFAULT_DRAFTS_DIR

    if not target_dir.is_dir():
        click.echo(f"No drafts directory found at {target_dir}")
        return

    draft_files = sorted(target_dir.glob("*.yaml"))
    if not draft_files:
        click.echo(f"No draft profiles found in {target_dir}")
        return

    from rich.table import Table

    from bernstein.cli.helpers import console

    table = Table(title=f"Persisted Drafts ({len(draft_files)})", show_lines=False)
    table.add_column("binary", style="cyan", no_wrap=True)
    table.add_column("file", style="dim")

    for draft_file in draft_files:
        try:
            document = read_draft_document(draft_file)
            binary = document.get("invocation", {}).get("binary", "<unknown>")
            table.add_row(str(binary), draft_file.name)
        except (ValueError, KeyError):
            table.add_row("<malformed>", draft_file.name)

    console.print(table)


@drafts_group.command("show")
@click.argument("binary")
@click.option(
    "--drafts-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Directory containing draft YAML files (default: {DEFAULT_DRAFTS_DIR}).",
)
def show_draft(binary: str, drafts_dir: Path | None) -> None:
    """Show the full content of a persisted draft profile.

    BINARY is the adapter binary name (e.g., 'my-adapter') which maps to
    'my-adapter.yaml' in the drafts directory.
    """
    target_dir = drafts_dir if drafts_dir is not None else DEFAULT_DRAFTS_DIR
    draft_file = target_dir / f"{binary}.yaml"

    if not draft_file.is_file():
        click.echo(f"No draft found for binary {binary!r} at {draft_file}", err=True)
        raise SystemExit(1)

    try:
        document = read_draft_document(draft_file)
    except ValueError as exc:
        click.echo(f"Failed to read draft: {exc}", err=True)
        raise SystemExit(1) from None

    # Validate required sections exist
    missing_sections = [s for s in ("invocation", "provenance") if s not in document]
    if missing_sections:
        click.echo(
            f"Malformed draft: missing required section(s): {', '.join(missing_sections)}",
            err=True,
        )
        raise SystemExit(1)

    # Validate invocation has required fields
    invocation = document.get("invocation", {})
    if not isinstance(invocation, dict) or "binary" not in invocation:
        click.echo("Malformed draft: invocation section missing 'binary' field", err=True)
        raise SystemExit(1)

    # Display the document in a readable format
    click.echo(f"Draft profile: {binary}")
    click.echo(f"Source: {draft_file}")
    click.echo()

    # Show invocation section
    invocation = document.get("invocation", {})
    if invocation:
        click.echo("[invocation]")
        click.echo(yaml.dump(invocation, default_flow_style=False, sort_keys=False).rstrip())

    # Show contract section
    contract = document.get("contract", {})
    if contract:
        click.echo()
        click.echo("[contract]")
        click.echo(yaml.dump(contract, default_flow_style=False, sort_keys=False).rstrip())

    # Show provenance section
    provenance = document.get("provenance", {})
    if provenance:
        click.echo()
        click.echo("[provenance]")
        click.echo(yaml.dump(provenance, default_flow_style=False, sort_keys=False).rstrip())


__all__ = [
    "DEFAULT_DRAFTS_DIR",
    "DEFAULT_EVIDENCE_DIR",
    "adapters_draft_cmd",
    "drafts_group",
    "list_drafts",
    "register_adapters_draft",
    "show_draft",
]
