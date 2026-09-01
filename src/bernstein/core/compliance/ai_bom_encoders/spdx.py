"""SPDX 2.3 encoder for the AI-BOM.

SPDX 2.3 has no first-class AI/ML asset type. The cross-walk:

* Every model / prompt / adapter / tool / data source becomes an SPDX
  ``Package`` with a ``SHA256`` checksum sourced from the lineage chain.
* Bernstein-specific attributes (provider, role, invocation count) land
  in the package ``annotations`` list with ``annotationType=OTHER``.
* The document ``creationInfo`` records the Bernstein run-id under
  ``comment`` so external SPDX tooling sees it without crashing on an
  unknown field.

The encoder is deterministic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bernstein.core.security.canonical import canonical_bytes

if TYPE_CHECKING:
    from bernstein.core.compliance.ai_bom import (
        AIBOM,
        AdapterEntry,
        DataSourceEntry,
        ModelEntry,
        PromptEntry,
        ToolEntry,
    )

__all__ = ["encode_spdx"]


SPDX_VERSION = "SPDX-2.3"
DATA_LICENSE = "CC0-1.0"


def encode_spdx(bom: AIBOM) -> bytes:
    """Return SPDX 2.3 JSON bytes."""
    packages: list[dict[str, Any]] = [_model_package(model) for model in bom.models]
    for prompt in bom.prompts:
        packages.append(_prompt_package(prompt))
    for adapter in bom.adapters:
        packages.append(_adapter_package(adapter))
    for tool in bom.tools:
        packages.append(_tool_package(tool))
    for source in bom.data_sources:
        packages.append(_data_source_package(source))

    doc: dict[str, Any] = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "spdxVersion": SPDX_VERSION,
        "dataLicense": DATA_LICENSE,
        "name": f"bernstein-ai-bom-{bom.run_id}",
        "documentNamespace": f"https://bernstein.run/spdx/{bom.run_id}",
        "creationInfo": {
            "created": bom.finished_at,
            "creators": [f"Tool: bernstein-{bom.bernstein_version}"],
            "comment": f"bernstein run_id={bom.run_id} lineage_root={bom.lineage_root_hash}",
        },
        "packages": packages,
    }
    return canonical_bytes(doc)


def _checksum(sha256: str) -> dict[str, str]:
    return {"algorithm": "SHA256", "checksumValue": sha256.split(":", 1)[1]}


def _annotation(comment: str) -> dict[str, str]:
    return {
        "annotationType": "OTHER",
        "annotator": "Tool: bernstein",
        "comment": comment,
        "annotationDate": "1970-01-01T00:00:00Z",
    }


def _spdx_id(prefix: str, sha256: str) -> str:
    digest = sha256.split(":", 1)[1]
    return f"SPDXRef-{prefix}-{digest[:16]}"


def _model_package(model: ModelEntry) -> dict[str, Any]:
    return {
        "SPDXID": _spdx_id("model", model.sha256),
        "name": model.name,
        "versionInfo": model.version,
        "supplier": f"Organization: {model.provider}",
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "checksums": [_checksum(model.sha256)],
        "annotations": [
            _annotation("bernstein:role=model"),
            _annotation(f"bernstein:invocation_count={model.invocation_count}"),
        ],
    }


def _prompt_package(prompt: PromptEntry) -> dict[str, Any]:
    return {
        "SPDXID": _spdx_id("prompt", prompt.sha256),
        "name": prompt.name,
        "versionInfo": "NOASSERTION",
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "checksums": [_checksum(prompt.sha256)],
        "annotations": [
            _annotation("bernstein:asset_kind=prompt-template"),
            _annotation(f"bernstein:role={prompt.role}"),
        ],
    }


def _adapter_package(adapter: AdapterEntry) -> dict[str, Any]:
    return {
        "SPDXID": _spdx_id("adapter", adapter.sha256),
        "name": adapter.name,
        "versionInfo": adapter.version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "checksums": [_checksum(adapter.sha256)],
        "annotations": [
            _annotation("bernstein:role=adapter"),
            _annotation(f"bernstein:binary={adapter.binary}"),
        ],
    }


def _tool_package(tool: ToolEntry) -> dict[str, Any]:
    return {
        "SPDXID": _spdx_id("tool", tool.sha256),
        "name": tool.name,
        "versionInfo": "NOASSERTION",
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "checksums": [_checksum(tool.sha256)],
        "annotations": [
            _annotation("bernstein:role=tool"),
            _annotation(f"bernstein:tool_kind={tool.kind}"),
        ],
    }


def _data_source_package(source: DataSourceEntry) -> dict[str, Any]:
    return {
        "SPDXID": _spdx_id("source", source.sha256),
        "name": source.uri,
        "versionInfo": "NOASSERTION",
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "checksums": [_checksum(source.sha256)],
        "annotations": [
            _annotation("bernstein:role=data-source"),
            _annotation(f"bernstein:source_kind={source.kind}"),
        ],
    }
