"""CycloneDX 1.7 + ML-BOM encoder.

The output shape follows the public CycloneDX AI-BOM capability
overview: https://cyclonedx.org/capabilities/aibom/.

Specification pin: 1.7 (schema ``bom-1.7.schema.json``, from the
CycloneDX specification tag ``1.7.2``, released 2026-09-17). The pin is
deliberate rather than "latest at import time": a compliance document
has to name the specification its bytes validate against. 1.7 is the
first line the ML-BOM fields (``modelCard``) are current in -- they
landed in 1.6 -- and procurement questionnaires for agentic systems ask
for them by name. The vendored copy of the schema lives in
``tests/fixtures/cyclonedx/`` and the encoder's output is validated
against it in ``tests/unit/compliance/test_ai_bom_cyclonedx_schema.py``.

Notes on cross-walk:

* ``components`` carries one entry per model, prompt, adapter, tool and
  data source. Models use ``type=machine-learning-model``. Prompts and
  tools use ``type=data`` with a ``properties`` block tagging the role;
  this is the recommended idiom in the AI-BOM whitepaper for assets that
  do not have a first-class CycloneDX type yet.
* Each model component carries a ``modelCard`` whose fields are
  projections of facts the run recorded: ``modelParameters.modelArchitecture``
  is the model identifier string from the lineage spine. Fields the run
  does not record (``task``, ``architectureFamily``, ``datasets``,
  ``quantitativeAnalysis``) are omitted rather than guessed -- see
  ``docs/operations/run-bom.md`` for the emit/omit table.
* The Bernstein run-id is embedded under ``metadata.properties`` so an
  external tool can correlate the BOM with the recorder log without
  parsing free text. ``serialNumber`` is a UUID URN derived from the
  run-id (CycloneDX requires that shape), so it is stable per run and
  still one-to-one with the run.
* Lineage root hash is embedded as ``metadata.properties[bernstein:lineage_root_hash]``
  for the same reason.

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


CYCLONEDX_SPEC_VERSION = "1.7"
CYCLONEDX_BOM_FORMAT = "CycloneDX"
CYCLONEDX_SCHEMA_URL = "http://cyclonedx.org/schema/bom-1.7.schema.json"

#: Namespace for the per-run serial number. CycloneDX constrains
#: ``serialNumber`` to a UUID URN (``^urn:uuid:<uuid>$``), which the old
#: ``urn:uuid:bernstein-ai-bom:<run_id>`` form did not satisfy. The run-id
#: is hashed into the namespace instead of being pasted into the URN, so
#: the serial stays deterministic per run and remains one-to-one with it
#: (the readable run-id is still in ``metadata.properties``). The namespace
#: is a bernstein-owned URI with a version suffix, deliberately *not* the
#: CycloneDX schema URI: it is stable across installs (same run-id, same
#: serial, on every machine) and does not rotate every serial when the
#: specification bumps. Distinct documents get distinct namespaces --
#: the dependency SBOM in ``core/security/sbom.py`` owns its own.
_SERIAL_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://bernstein.run/compliance/ai-bom/v1")


def encode_cyclonedx(bom: AIBOM) -> bytes:
    """Return CycloneDX 1.7 (ML-BOM) JSON bytes."""
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
        "$schema": CYCLONEDX_SCHEMA_URL,
        "bomFormat": CYCLONEDX_BOM_FORMAT,
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "version": 1,
        "serialNumber": f"urn:uuid:{uuid.uuid5(_SERIAL_NAMESPACE, bom.run_id)}",
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
    component: dict[str, Any] = {
        "bom-ref": _bom_ref("model", model.sha256),
        "type": "machine-learning-model",
        "name": model.name,
        "version": model.version,
        "publisher": model.provider,
        "hashes": _hash_block(model.sha256),
        "properties": [
            {"name": "bernstein:invocation_count", "value": str(model.invocation_count)},
            {"name": "bernstein:role", "value": "model"},
        ],
    }
    card = _model_card(model)
    if card:
        component["modelCard"] = card
    return component


def _model_card(model: ModelEntry) -> dict[str, Any]:
    """Project the CycloneDX ML-BOM model card from recorded facts.

    ``modelParameters.modelArchitecture`` carries the model identifier the
    spine recorded (``"claude-3-7-sonnet"``, and so on). That is the field
    the specification points at for a specific model -- its own examples
    are ``GPT-1``, ``ResNet-50``, ``YOLOv3`` -- and the identifier is the
    only model fact a run records.

    The remaining model-card fields describe properties a run does not
    record, so they are omitted instead of inferred: no ``task``,
    ``architectureFamily``, ``approach`` or ``datasets`` (the run records
    neither the model's ML task nor which dataset trained it), and no
    ``quantitativeAnalysis.performanceMetrics`` (evaluation results are
    recorded against gates and suites, not against a model entry).

    Returns:
        A model card dict, or ``{}`` when the entry records no identifier
        at all -- an empty card would be a claim with no fact behind it.
    """
    if not model.name:
        return {}
    return {"modelParameters": {"modelArchitecture": model.name}}


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
