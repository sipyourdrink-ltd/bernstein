"""Explicit composition export; exit 0 means written, never runtime verified."""

from __future__ import annotations

import json
from pathlib import Path

import click


@click.command("export-agent")
@click.option("--declarations", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--prompt", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--lineage-root", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option("--hop-id", required=True)
@click.option("--key-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
def export_agent_command(declarations: Path, prompt: Path, lineage_root: Path, hop_id: str, key_dir: Path) -> None:
    """Export a composition-only Agent Manifest with operator declarations.

    Exit 0: files written, not runtime artifact verification. Exit 1: invalid
    declarations, missing pinned extra, custody error or output collision.
    KEY_DIR is the installation AgentCardKeystore directory; key material is
    handled only by the custody adapter. PROMPT must be UTF-8 composition text.
    """
    from bernstein.core.security.agent_card_keystore import AgentCardKeystore
    from bernstein.core.security.agent_manifest_export import (
        CompositionDeclarations,
        CompositionManifestExporter,
        require_manifest_sdk,
    )

    try:
        declared = CompositionDeclarations.model_validate(json.loads(declarations.read_text(encoding="utf-8")))
        text = prompt.read_bytes().decode("utf-8")
        require_manifest_sdk()
        exporter = CompositionManifestExporter(lineage_root, lambda _hop: declared, AgentCardKeystore(key_dir).signer())
        references = exporter(hop_id, text)
    except (ValueError, OSError, ImportError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        json.dumps(
            {"status": "exported", "runtime_artifacts_verified": False, "references": references}, sort_keys=True
        )
    )
