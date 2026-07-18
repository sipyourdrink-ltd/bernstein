"""AI Bill of Materials (AI-BOM) export.

A Bernstein run touches many artefacts: model invocations, prompt
templates, adapter binaries, MCP tools and data sources. Operators and
auditors need a machine-readable manifest enumerating *which* exact
(model, prompt_sha, adapter, version, data_source) tuples shaped a given
run so that a downstream incident can be attributed to a specific tuple
and a release can be reproduced byte-for-byte.

This module is the projection over existing state described by issue
#1371. It cannot record new facts: every list element sources its hash
from the lineage v2 chain, the cost ledger, or the adapter contract
YAMLs. Re-running ``generate_bom(run_id)`` against the same inputs is
expected to yield the same bytes (deterministic encoding).

Public surface (mirrors ``article12.py``):

* :class:`AIBOM` -- the frozen dataclass shape.
* :class:`ModelEntry`, :class:`PromptEntry`, :class:`AdapterEntry`,
  :class:`ToolEntry`, :class:`DataSourceEntry` -- list element shapes.
* :func:`generate_bom` -- pure projection over a snapshot dict.
* :func:`encode_bom` -- format dispatcher (json | cyclonedx | spdx).
* :func:`verify_bom` -- structural + hash verifier.

The CycloneDX 1.5 encoder follows the AI/ML extension recommendations
documented at https://cyclonedx.org/capabilities/aibom/. The SPDX 2.3
encoder emits the SBOM subset relevant to model+package listing; AI-
specific fields cross-walk to ``annotations`` so a vanilla SPDX
validator accepts the document.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, cast

__all__ = [
    "AIBOM",
    "BOM_SCHEMA_URL",
    "BOM_SCHEMA_VERSION",
    "SUPPORTED_FORMATS",
    "AdapterEntry",
    "BOMError",
    "BOMVerificationReport",
    "DataSourceEntry",
    "ModelEntry",
    "PromptEntry",
    "ToolEntry",
    "encode_bom",
    "generate_bom",
    "verify_bom",
]


BOM_SCHEMA_VERSION = "1.0"
BOM_SCHEMA_URL = "https://bernstein.run/compliance/ai-bom/v1"

SUPPORTED_FORMATS: frozenset[str] = frozenset({"json", "cyclonedx", "spdx"})

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BOMError(ValueError):
    """Raised when BOM generation or verification fails."""


# ---------------------------------------------------------------------------
# Element shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """Single model invocation tuple.

    Hash is sourced from the lineage chain entry that recorded the model
    call (typically the adapter's tool-call artefact). It is never
    recomputed here.
    """

    name: str
    provider: str
    version: str
    sha256: str
    invocation_count: int

    def __post_init__(self) -> None:
        _require_sha(self.sha256, "ModelEntry.sha256")
        if self.invocation_count < 0:
            raise BOMError(f"ModelEntry.invocation_count must be >= 0, got {self.invocation_count}")


@dataclass(frozen=True, slots=True)
class PromptEntry:
    """Prompt template used during the run.

    ``sha256`` is the lineage-chain hash of the canonical template
    bytes; it doubles as the deduplication key.
    """

    name: str
    role: str
    sha256: str

    def __post_init__(self) -> None:
        _require_sha(self.sha256, "PromptEntry.sha256")


@dataclass(frozen=True, slots=True)
class AdapterEntry:
    """CLI adapter / agent binary."""

    name: str
    version: str
    sha256: str
    binary: str

    def __post_init__(self) -> None:
        _require_sha(self.sha256, "AdapterEntry.sha256")


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """MCP tool or other tool surface used by an agent."""

    name: str
    kind: str
    sha256: str

    def __post_init__(self) -> None:
        _require_sha(self.sha256, "ToolEntry.sha256")


@dataclass(frozen=True, slots=True)
class DataSourceEntry:
    """External data source referenced during the run."""

    uri: str
    kind: str
    sha256: str

    def __post_init__(self) -> None:
        _require_sha(self.sha256, "DataSourceEntry.sha256")


@dataclass(frozen=True, slots=True)
class AIBOM:
    """AI Bill of Materials root.

    Frozen + slots: the dataclass shape itself is canonical, so any
    surprise attribute would alter the deterministic JSON shape.

    Attributes:
        schema: schema URL constant used by external tooling.
        schema_version: integer-tagged schema version.
        run_id: opaque Bernstein run identifier.
        started_at: ISO-8601 UTC start timestamp.
        finished_at: ISO-8601 UTC finish timestamp.
        lineage_root_hash: ``sha256:...`` over the lineage v2 root.
        bernstein_version: package version recorded in the snapshot.
        models: deduped sorted list of model invocation entries.
        prompts: deduped sorted list of prompt entries.
        adapters: deduped sorted list of adapter entries.
        tools: deduped sorted list of tool entries.
        data_sources: deduped sorted list of data source entries.
    """

    schema: str
    schema_version: str
    run_id: str
    started_at: str
    finished_at: str
    lineage_root_hash: str
    bernstein_version: str
    models: tuple[ModelEntry, ...]
    prompts: tuple[PromptEntry, ...]
    adapters: tuple[AdapterEntry, ...]
    tools: tuple[ToolEntry, ...]
    data_sources: tuple[DataSourceEntry, ...]

    def __post_init__(self) -> None:
        if not self.run_id:
            raise BOMError("AIBOM.run_id must be non-empty")
        if self.schema_version != BOM_SCHEMA_VERSION:
            raise BOMError(
                f"AIBOM.schema_version must be {BOM_SCHEMA_VERSION!r}, got {self.schema_version!r}",
            )
        _require_sha(self.lineage_root_hash, "AIBOM.lineage_root_hash")


@dataclass(frozen=True, slots=True)
class BOMVerificationReport:
    """Result of :func:`verify_bom`.

    ``ok`` is true only when every check passes. ``errors`` enumerates
    each independent failure so an operator sees the full picture rather
    than the first error.
    """

    ok: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
    checked_count: int = 0


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_bom(snapshot: Mapping[str, Any]) -> AIBOM:
    """Project ``snapshot`` into an :class:`AIBOM`.

    The function is pure: it performs no I/O and does not recompute any
    hash. All hash fields are sourced from the caller-provided snapshot
    dict, which is the join between lineage v2 (#1377), the cost ledger
    (#1330), the decision log (#1351) and the adapter contract YAMLs.

    Args:
        snapshot: dict with the keys described in
            :class:`BOMSnapshotProtocol` (see module docs). Missing keys
            raise :class:`BOMError`.

    Returns:
        A deterministic :class:`AIBOM` whose element lists are sorted
        and deduplicated.
    """
    for required in (
        "run_id",
        "started_at",
        "finished_at",
        "lineage_root_hash",
    ):
        if required not in snapshot:
            raise BOMError(f"snapshot missing required key: {required!r}")

    models = tuple(sorted(_dedupe_models(snapshot.get("models", [])), key=_model_sort_key))
    prompts = tuple(sorted(_dedupe_prompts(snapshot.get("prompts", [])), key=_prompt_sort_key))
    adapters = tuple(sorted(_dedupe_adapters(snapshot.get("adapters", [])), key=_adapter_sort_key))
    tools = tuple(sorted(_dedupe_tools(snapshot.get("tools", [])), key=_tool_sort_key))
    data_sources = tuple(
        sorted(_dedupe_data_sources(snapshot.get("data_sources", [])), key=_data_source_sort_key),
    )

    return AIBOM(
        schema=BOM_SCHEMA_URL,
        schema_version=BOM_SCHEMA_VERSION,
        run_id=str(snapshot["run_id"]),
        started_at=str(snapshot["started_at"]),
        finished_at=str(snapshot["finished_at"]),
        lineage_root_hash=str(snapshot["lineage_root_hash"]),
        bernstein_version=str(snapshot.get("bernstein_version", "0+unknown")),
        models=models,
        prompts=prompts,
        adapters=adapters,
        tools=tools,
        data_sources=data_sources,
    )


# ---------------------------------------------------------------------------
# Encoding dispatch
# ---------------------------------------------------------------------------


def encode_bom(bom: AIBOM, fmt: str = "json") -> bytes:
    """Encode ``bom`` in ``fmt``.

    The dispatcher mirrors the encoder-registry pattern used by
    ``article12.py``: each encoder is its own module under
    ``ai_bom_encoders/`` so adding a new format is one file plus one
    registry entry. The output is deterministic: encoding the same
    :class:`AIBOM` twice yields byte-identical results.
    """
    if fmt not in SUPPORTED_FORMATS:
        raise BOMError(
            f"unsupported BOM format {fmt!r}; expected one of {sorted(SUPPORTED_FORMATS)}",
        )
    encoder = _ENCODERS[fmt]
    return encoder(bom)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_bom(payload: object) -> BOMVerificationReport:
    """Verify a serialised BOM document.

    Checks performed:

    1. Parse the JSON envelope and assert top-level keys + types.
    2. Every list element exposes a well-formed ``sha256:...`` string.
    3. The ``lineage_root_hash`` is well-formed.
    4. ``schema_version`` matches the implementation.
    5. Sort order is preserved (catches manual reordering attacks).

    The check is structural; cryptographic verification of the lineage
    root itself is delegated to ``lineage.verify(root_hash)`` which has
    its own keying and is intentionally out of scope here.
    """
    errors: list[str] = []
    checked = 0
    try:
        doc = _coerce_payload(payload)
    except BOMError as exc:
        return BOMVerificationReport(ok=False, errors=(str(exc),), checked_count=0)

    schema_version = doc.get("schema_version")
    if schema_version != BOM_SCHEMA_VERSION:
        errors.append(
            f"schema_version mismatch: got {schema_version!r}, expected {BOM_SCHEMA_VERSION!r}",
        )

    if not isinstance(doc.get("run_id"), str) or not doc["run_id"]:
        errors.append("run_id must be a non-empty string")

    root_hash = doc.get("lineage_root_hash")
    if not isinstance(root_hash, str) or not _SHA256_RE.match(root_hash):
        errors.append(f"lineage_root_hash is not a well-formed sha256: {root_hash!r}")

    list_keys: tuple[tuple[str, Callable[[dict[str, Any]], tuple[str, ...]]], ...] = (
        ("models", _model_dict_sort_key),
        ("prompts", _prompt_dict_sort_key),
        ("adapters", _adapter_dict_sort_key),
        ("tools", _tool_dict_sort_key),
        ("data_sources", _data_source_dict_sort_key),
    )
    for key, sort_key in list_keys:
        items_raw = doc.get(key, [])
        if not isinstance(items_raw, list):
            errors.append(f"{key} must be a list, got {type(items_raw).__name__}")
            continue
        items_list: list[Any] = cast("list[Any]", items_raw)
        last_key: tuple[str, ...] | None = None
        for index, item in enumerate(items_list):
            checked += 1
            if not isinstance(item, dict):
                errors.append(f"{key}[{index}] is not an object")
                continue
            item_d: dict[str, Any] = cast("dict[str, Any]", item)
            sha = item_d.get("sha256")
            if not isinstance(sha, str) or not _SHA256_RE.match(sha):
                errors.append(f"{key}[{index}].sha256 is not a well-formed sha256: {sha!r}")
                continue
            cur_key = sort_key(item_d)
            if last_key is not None and cur_key < last_key:
                errors.append(f"{key}[{index}] breaks deterministic ordering")
            last_key = cur_key

    ok = not errors
    return BOMVerificationReport(ok=ok, errors=tuple(errors), checked_count=checked)


# ---------------------------------------------------------------------------
# Internal helpers - dedupe + sort
#
# Invariant: every field of an entry dataclass is either part of that
# entry's sort key (which doubles as its dedupe identity) or folded by a
# commutative merge - today only ``ModelEntry.invocation_count``, which
# is summed. A field that is neither is silently dropped when two
# entries collide, and *which* value survives depends on the input
# ordering, so the BOM stops being a pure function of the run. Adding a
# field to an entry means adding it to the matching ``_*_sort_key`` and
# ``_*_dict_sort_key`` pair; the latter keeps ``verify_bom``'s ordering
# check total, so a reordering of two otherwise-equal entries is still
# detectable. ``TestDedupeIdentityCoverage`` enforces this per field.
# ---------------------------------------------------------------------------


def _coerce_payload(payload: object) -> dict[str, Any]:
    """Normalise BOM input to a dict.

    Accepts ``Mapping``, bytes, or str. Anything else raises a
    :class:`BOMError`. Typed as ``object`` so the verifier surface
    handles operator mistakes without TypeErrors leaking through.
    """
    if isinstance(payload, Mapping):
        return dict(cast("Mapping[str, Any]", payload))
    data: Any
    if isinstance(payload, (bytes, bytearray)):
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BOMError(f"payload is not valid UTF-8 JSON: {exc}") from None
    elif isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BOMError(f"payload is not valid JSON: {exc}") from None
    else:
        raise BOMError(f"unsupported payload type: {type(payload).__name__}")
    if not isinstance(data, dict):
        raise BOMError("BOM payload must decode to a JSON object")
    return cast("dict[str, Any]", data)


def _require_sha(value: str, label: str) -> None:
    if not _SHA256_RE.match(value):
        raise BOMError(f"{label} must be a well-formed sha256:<hex>, got {value!r}")


def _model_sort_key(entry: ModelEntry) -> tuple[str, str, str, str]:
    return (entry.provider, entry.name, entry.version, entry.sha256)


def _prompt_sort_key(entry: PromptEntry) -> tuple[str, str, str]:
    return (entry.role, entry.name, entry.sha256)


def _adapter_sort_key(entry: AdapterEntry) -> tuple[str, str, str, str]:
    return (entry.name, entry.version, entry.sha256, entry.binary)


def _tool_sort_key(entry: ToolEntry) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.sha256)


def _data_source_sort_key(entry: DataSourceEntry) -> tuple[str, str, str]:
    return (entry.kind, entry.uri, entry.sha256)


def _model_dict_sort_key(item: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(item.get("provider", "")),
        str(item.get("name", "")),
        str(item.get("version", "")),
        str(item.get("sha256", "")),
    )


def _prompt_dict_sort_key(item: dict[str, Any]) -> tuple[str, ...]:
    return (str(item.get("role", "")), str(item.get("name", "")), str(item.get("sha256", "")))


def _adapter_dict_sort_key(item: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(item.get("name", "")),
        str(item.get("version", "")),
        str(item.get("sha256", "")),
        str(item.get("binary", "")),
    )


def _tool_dict_sort_key(item: dict[str, Any]) -> tuple[str, ...]:
    return (str(item.get("kind", "")), str(item.get("name", "")), str(item.get("sha256", "")))


def _data_source_dict_sort_key(item: dict[str, Any]) -> tuple[str, ...]:
    return (str(item.get("kind", "")), str(item.get("uri", "")), str(item.get("sha256", "")))


def _dedupe_models(items: Iterable[Any]) -> list[ModelEntry]:
    seen: dict[tuple[str, ...], ModelEntry] = {}
    for raw in items:
        entry = _coerce_model(raw)
        key = (entry.provider, entry.name, entry.version, entry.sha256)
        if key in seen:
            # Sum invocations across duplicates so multiple lineage
            # entries for the same model collapse without losing count.
            prior = seen[key]
            seen[key] = ModelEntry(
                name=prior.name,
                provider=prior.provider,
                version=prior.version,
                sha256=prior.sha256,
                invocation_count=prior.invocation_count + entry.invocation_count,
            )
        else:
            seen[key] = entry
    return list(seen.values())


def _dedupe_prompts(items: Iterable[Any]) -> list[PromptEntry]:
    return list({_prompt_sort_key(_coerce_prompt(r)): _coerce_prompt(r) for r in items}.values())


def _dedupe_adapters(items: Iterable[Any]) -> list[AdapterEntry]:
    return list({_adapter_sort_key(_coerce_adapter(r)): _coerce_adapter(r) for r in items}.values())


def _dedupe_tools(items: Iterable[Any]) -> list[ToolEntry]:
    return list({_tool_sort_key(_coerce_tool(r)): _coerce_tool(r) for r in items}.values())


def _dedupe_data_sources(items: Iterable[Any]) -> list[DataSourceEntry]:
    return list(
        {_data_source_sort_key(_coerce_data_source(r)): _coerce_data_source(r) for r in items}.values(),
    )


def _coerce_model(raw: Any) -> ModelEntry:
    if isinstance(raw, ModelEntry):
        return raw
    if not isinstance(raw, Mapping):
        raise BOMError(f"model entry must be a mapping or ModelEntry, got {type(raw).__name__}")
    raw_m = cast("Mapping[str, Any]", raw)
    return ModelEntry(
        name=str(raw_m["name"]),
        provider=str(raw_m["provider"]),
        version=str(raw_m["version"]),
        sha256=str(raw_m["sha256"]),
        invocation_count=int(raw_m.get("invocation_count", 1)),
    )


def _coerce_prompt(raw: Any) -> PromptEntry:
    if isinstance(raw, PromptEntry):
        return raw
    if not isinstance(raw, Mapping):
        raise BOMError(f"prompt entry must be a mapping or PromptEntry, got {type(raw).__name__}")
    raw_m = cast("Mapping[str, Any]", raw)
    return PromptEntry(
        name=str(raw_m["name"]),
        role=str(raw_m["role"]),
        sha256=str(raw_m["sha256"]),
    )


def _coerce_adapter(raw: Any) -> AdapterEntry:
    if isinstance(raw, AdapterEntry):
        return raw
    if not isinstance(raw, Mapping):
        raise BOMError(f"adapter entry must be a mapping or AdapterEntry, got {type(raw).__name__}")
    raw_m = cast("Mapping[str, Any]", raw)
    return AdapterEntry(
        name=str(raw_m["name"]),
        version=str(raw_m["version"]),
        sha256=str(raw_m["sha256"]),
        binary=str(raw_m.get("binary", raw_m["name"])),
    )


def _coerce_tool(raw: Any) -> ToolEntry:
    if isinstance(raw, ToolEntry):
        return raw
    if not isinstance(raw, Mapping):
        raise BOMError(f"tool entry must be a mapping or ToolEntry, got {type(raw).__name__}")
    raw_m = cast("Mapping[str, Any]", raw)
    return ToolEntry(
        name=str(raw_m["name"]),
        kind=str(raw_m["kind"]),
        sha256=str(raw_m["sha256"]),
    )


def _coerce_data_source(raw: Any) -> DataSourceEntry:
    if isinstance(raw, DataSourceEntry):
        return raw
    if not isinstance(raw, Mapping):
        raise BOMError(f"data source entry must be a mapping, got {type(raw).__name__}")
    raw_m = cast("Mapping[str, Any]", raw)
    return DataSourceEntry(
        uri=str(raw_m["uri"]),
        kind=str(raw_m["kind"]),
        sha256=str(raw_m["sha256"]),
    )


# ---------------------------------------------------------------------------
# Canonical native encoder
# ---------------------------------------------------------------------------


def _encode_native_json(bom: AIBOM) -> bytes:
    """RFC 8785-ish canonical JSON.

    ``sort_keys=True`` + minimal separators + UTF-8 covers the subset
    relevant to flat objects of strings / ints / lists. We never put
    floats or None into a BOM, so the corner cases of the spec around
    ES6 number formatting and recursive ordering do not apply.
    """
    doc = asdict(bom)
    # asdict() turns tuples into lists, which is what JSON wants.
    return json.dumps(
        doc,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _encode_cyclonedx(bom: AIBOM) -> bytes:
    from bernstein.core.compliance.ai_bom_encoders.cyclonedx import encode_cyclonedx

    return encode_cyclonedx(bom)


def _encode_spdx(bom: AIBOM) -> bytes:
    from bernstein.core.compliance.ai_bom_encoders.spdx import encode_spdx

    return encode_spdx(bom)


_ENCODERS: dict[str, Callable[[AIBOM], bytes]] = {
    "json": _encode_native_json,
    "cyclonedx": _encode_cyclonedx,
    "spdx": _encode_spdx,
}


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def bom_content_hash(bom: AIBOM) -> str:
    """Return ``sha256:<hex>`` over the canonical JSON form of ``bom``.

    Operators use this to anchor the BOM in the decision log: the hash
    is stable across encoder format (since we always hash the native
    JSON), so a re-emit under a different ``--format`` flag still
    points at the same logical record.
    """
    canonical = _encode_native_json(bom)
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
