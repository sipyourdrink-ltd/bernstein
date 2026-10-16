"""CycloneDX 1.6 AI/ML BOM encoder.

The output shape follows the CycloneDX AI-BOM capability overview:
https://cyclonedx.org/capabilities/aibom/.

Notes on cross-walk:

* ``components`` carries one entry per model, prompt, adapter, tool and
  data source. Models use ``type=machine-learning-model`` with a
  ``modelCard`` block (first-class in 1.6) carrying ML-BOM fields
  sourced from run facts. Prompts and tools use ``type=data`` with a
  ``properties`` block tagging the role.
* The Bernstein run-id is embedded under ``metadata.properties`` so an
  external tool can correlate the BOM with the recorder log without
  parsing free text.
* Lineage root hash is embedded as ``metadata.properties[bernstein:lineage_root_hash]``
  for the same reason.
* ``serialNumber`` is a deterministic UUID5 derived from the run-id
  under a fixed Bernstein namespace, so it is RFC 4122-compliant and
  stable across re-derives of the same run.

The encoder is deterministic: identical :class:`AIBOM` -> identical bytes.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.compliance.ai_bom import (
        AIBOM,
        AdapterEntry,
        DataSourceEntry,
        ModelEntry,
        PromptEntry,
        ToolEntry,
    )

__all__ = ["encode_cyclonedx"]


CYCLONEDX_SPEC_VERSION = "1.6"
CYCLONEDX_BOM_FORMAT = "CycloneDX"

# Fixed UUID5 namespace for Bernstein AI-BOM serial numbers.
# Changing this would invalidate all previously issued serial numbers.
_BERNSTEIN_BOM_NS = uuid.UUID("b3c71b3b-3d48-5c83-9a5a-4e8c49a29d7a")


def _serial_number(run_id: str) -> str:
    """Return a deterministic RFC 4122 UUID5 serial number for ``run_id``."""
    return f"urn:uuid:{uuid.uuid5(_BERNSTEIN_BOM_NS, run_id)}"


def encode_cyclonedx(bom: AIBOM) -> bytes:
    """Return CycloneDX 1.6 AI/ML BOM JSON bytes."""
    components: list[dict[str, Any]] = [_model_component(model) for model in bom.models]
    for prompt in bom.prompts:
        components.append(_prompt_component(prompt))
    for adapter in bom.adapters:
        components.append(_adapter_component(adapter))
    for tool in bom.tools:
        components.append(_tool_component(tool))
    for source in bom.data_sources:
        components.append(_data_source_component(source))

    doc: dict[str, Any] = {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": CYCLONEDX_BOM_FORMAT,
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "version": 1,
        "serialNumber": _serial_number(bom.run_id),
        "metadata": {
            "timestamp": bom.finished_at,
            "tools": [
                {
                    "vendor": "bernstein",
                    "name": "bernstein",
                    "version": bom.bernstein_version,
                },
            ],
            "properties": [
                {"name": "bernstein:run_id", "value": bom.run_id},
                {"name": "bernstein:started_at", "value": bom.started_at},
                {"name": "bernstein:finished_at", "value": bom.finished_at},
                {"name": "bernstein:lineage_root_hash", "value": bom.lineage_root_hash},
                {"name": "bernstein:schema", "value": bom.schema},
                {"name": "bernstein:schema_version", "value": bom.schema_version},
            ],
        },
        "components": components,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash_block(sha256: str) -> list[dict[str, str]]:
    """Convert ``sha256:<hex>`` to the CycloneDX ``hashes`` shape."""
    _, _, digest = sha256.partition(":")
    return [{"alg": "SHA-256", "content": digest}]


def _bom_ref(prefix: str, sha256: str) -> str:
    digest = sha256.split(":", 1)[1]
    return f"bernstein:{prefix}:{digest[:16]}"


def _model_component(model: ModelEntry) -> dict[str, Any]:
    return {
        "bom-ref": _bom_ref("model", model.sha256),
        "type": "machine-learning-model",
        "name": model.name,
        "version": model.version,
        "publisher": model.provider,
        "hashes": _hash_block(model.sha256),
        "modelCard": {
            "properties": [
                {"name": "bernstein:invocation_count", "value": str(model.invocation_count)},
            ],
        },
        "properties": [
            {"name": "bernstein:role", "value": "model"},
        ],
    }


def _prompt_component(prompt: PromptEntry) -> dict[str, Any]:
    return {
        "bom-ref": _bom_ref("prompt", prompt.sha256),
        "type": "data",
        "name": prompt.name,
        "hashes": _hash_block(prompt.sha256),
        "properties": [
            {"name": "bernstein:role", "value": prompt.role},
            {"name": "bernstein:asset_kind", "value": "prompt-template"},
        ],
    }


def _adapter_component(adapter: AdapterEntry) -> dict[str, Any]:
    return {
        "bom-ref": _bom_ref("adapter", adapter.sha256),
        "type": "application",
        "name": adapter.name,
        "version": adapter.version,
        "hashes": _hash_block(adapter.sha256),
        "properties": [
            {"name": "bernstein:binary", "value": adapter.binary},
            {"name": "bernstein:role", "value": "adapter"},
        ],
    }


def _tool_component(tool: ToolEntry) -> dict[str, Any]:
    return {
        "bom-ref": _bom_ref("tool", tool.sha256),
        "type": "application",
        "name": tool.name,
        "hashes": _hash_block(tool.sha256),
        "properties": [
            {"name": "bernstein:tool_kind", "value": tool.kind},
            {"name": "bernstein:role", "value": "tool"},
        ],
    }


def _data_source_component(source: DataSourceEntry) -> dict[str, Any]:
    return {
        "bom-ref": _bom_ref("source", source.sha256),
        "type": "data",
        "name": source.uri,
        "hashes": _hash_block(source.sha256),
        "properties": [
            {"name": "bernstein:source_kind", "value": source.kind},
            {"name": "bernstein:role", "value": "data-source"},
        ],
    }
