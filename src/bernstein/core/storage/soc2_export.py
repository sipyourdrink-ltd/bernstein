"""Storage-agnostic SOC 2 evidence pack exporter.

Decouples the weekly SOC 2 evidence pack workflow from GitHub-hosted artifact
storage. When a destination sink is configured (via ``SOC2_EVIDENCE_SINK`` or
explicit argument), this module writes the rendered Markdown checklist and JSON
manifest into the registered :class:`~bernstein.core.storage.sink.ArtifactSink`.

Artifacts land under the canonical key layout:
``soc2/{period_label}/{run_id}/{filename}``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.storage.keys import soc2_evidence_key
from bernstein.core.storage.registry import get_sink

if TYPE_CHECKING:
    from bernstein.core.storage.sink import ArtifactSink

logger = logging.getLogger(__name__)


async def _export_soc2_evidence_pack_async(
    markdown_path: Path | None,
    manifest_path: Path | None,
    *,
    sink: ArtifactSink,
    sink_name: str,
    period_label: str,
    run_id: str,
) -> None:
    """Async worker for exporting evidence pack files to a sink."""
    targets = [p for p in (markdown_path, manifest_path) if p is not None]
    if not targets:
        return

    for target in targets:
        path = Path(target)
        data = path.read_bytes()
        key = soc2_evidence_key(period_label, run_id, path.name)
        content_type = (
            "text/markdown"
            if path.suffix.lower() == ".md"
            else ("application/json" if path.suffix.lower() == ".json" else None)
        )
        await sink.write(key, data, durable=True, content_type=content_type)
        logger.info(
            "Exported SOC 2 evidence artifact %s to sink %r at %s (%d bytes)",
            path.name,
            sink_name,
            key,
            len(data),
        )


def export_soc2_evidence_pack(
    markdown_path: Path | None,
    manifest_path: Path | None,
    *,
    sink_name: str | None,
    period_label: str,
    run_id: str,
) -> None:
    """Export SOC 2 evidence pack files to a configured artifact sink.

    When *sink_name* is ``None`` or empty, this operation is a no-op and logs
    an informational message. When set, the sink is looked up from the default
    storage registry and each non-None path is written under
    ``soc2/{period_label}/{run_id}/{path.name}``.

    Args:
        markdown_path: Path to the generated SOC 2 markdown checklist.
        manifest_path: Path to the companion JSON manifest.
        sink_name: Name of the registered sink backend (e.g. ``"s3"``,
            ``"local_fs"``). If ``None`` or empty, export is skipped.
        period_label: Human-readable reporting period (e.g. ``"weekly"``).
        run_id: Workflow or orchestrator run identifier.
    """
    if not sink_name or not sink_name.strip():
        logger.info("SOC 2 evidence export skipped: no sink_name configured")
        return

    canonical_sink_name = sink_name.strip()
    sink = get_sink(canonical_sink_name)

    coro = _export_soc2_evidence_pack_async(
        markdown_path,
        manifest_path,
        sink=sink,
        sink_name=canonical_sink_name,
        period_label=period_label,
        run_id=run_id,
    )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coro).result()


__all__ = ["export_soc2_evidence_pack"]
