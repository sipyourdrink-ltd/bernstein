"""Adapter security-floor refresh pipeline with signed update receipts (#2515).

The advisory floor map (:data:`bernstein.adapters.advisories.
ADAPTER_MIN_SAFE_VERSIONS`) is curated data. This module turns a floor bump
into a *reviewed, data-only diff accompanied by a signed update receipt*:
:func:`parse_feed` reads a machine-readable advisory feed, :func:`diff_floor_maps`
computes the change against the in-code map, :func:`render_advisory_block`
regenerates the map block as data (byte-identical to the current source when
the feed matches, so a bump is a one-line data edit and never a logic change),
and :func:`build_floor_update_receipt` seals a content-addressed receipt
binding the old and new floor-map content hashes and the diff.

Lever: the update receipt is anchored to the same content-hash scheme the
spawn-preflight and canary receipts pin, and (via
:func:`bernstein.core.security.audit_chain.record_adapter_floor_update_receipt`)
into the HMAC chain, so a floor bump is an attested event -- a reviewer can
prove offline which floor map was in force when a spawn decision was recorded.
"""

from __future__ import annotations

import hashlib
import json
import operator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS, AdapterAdvisory
from bernstein.adapters.security_floor import hash_floor_map

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "FLOOR_MAP_BEGIN",
    "FLOOR_MAP_END",
    "FLOOR_UPDATE_SCHEMA_VERSION",
    "FloorMapDiff",
    "build_floor_update_receipt",
    "diff_floor_maps",
    "parse_feed",
    "receipt_sha256",
    "render_advisory_block",
    "verify_floor_update_receipt",
    "write_advisory_block",
]

#: Schema version stamped into the update-receipt preimage.
FLOOR_UPDATE_SCHEMA_VERSION = 1

#: Markers delimiting the regenerated floor-map block inside ``advisories.py``.
FLOOR_MAP_BEGIN = "# floor-map:begin"
FLOOR_MAP_END = "# floor-map:end"


def _canonical_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def receipt_sha256(receipt: dict[str, Any]) -> str:
    """Content hash (identity) of a receipt's canonical bytes."""
    return hashlib.sha256(_canonical_bytes(receipt)).hexdigest()


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------


def parse_feed(data: dict[str, Any]) -> dict[str, AdapterAdvisory]:
    """Parse a machine-readable advisory feed into a floor map.

    Feed shape::

        {"schema_version": 1,
         "adapters": {
            "aider": {"min_safe_version": "0.60.0",
                      "advisory_id": "BSA-0001",
                      "note": "..."},
            ...}}

    Raises:
        ValueError: When the feed is malformed or an entry is missing a
            required field.
    """
    adapters = data.get("adapters") if isinstance(data, dict) else None
    if not isinstance(adapters, dict):
        raise ValueError("advisory feed must carry an 'adapters' object")
    mapping: dict[str, AdapterAdvisory] = {}
    for name, entry in adapters.items():
        if not isinstance(entry, dict):
            raise ValueError(f"feed entry for {name!r} must be an object")
        try:
            mapping[str(name)] = AdapterAdvisory(
                adapter=str(name),
                min_safe_version=str(entry["min_safe_version"]),
                advisory_id=str(entry["advisory_id"]),
                note=str(entry["note"]),
            )
        except KeyError as exc:
            raise ValueError(f"feed entry for {name!r} is missing field {exc}") from exc
    return mapping


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FloorMapDiff:
    """The change from one floor map to another.

    Attributes:
        added: Adapters present only in the new map.
        removed: Adapters present only in the old map.
        changed: Per-field changes for adapters present in both, each
            ``{"adapter", "field", "old", "new"}``.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when the two maps are identical."""
        return not self.added and not self.removed and not self.changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": sorted(self.added),
            "removed": sorted(self.removed),
            "changed": sorted(self.changed, key=operator.itemgetter("adapter", "field")),
        }


def diff_floor_maps(old: dict[str, AdapterAdvisory], new: dict[str, AdapterAdvisory]) -> FloorMapDiff:
    """Compute the data-only diff between two floor maps."""
    added = [name for name in new if name not in old]
    removed = [name for name in old if name not in new]
    changed: list[dict[str, str]] = []
    for name in sorted(set(old) & set(new)):
        old_adv, new_adv = old[name], new[name]
        for f in ("min_safe_version", "advisory_id", "note"):
            old_v, new_v = getattr(old_adv, f), getattr(new_adv, f)
            if old_v != new_v:
                changed.append({"adapter": name, "field": f, "old": old_v, "new": new_v})
    return FloorMapDiff(added=added, removed=removed, changed=changed)


# ---------------------------------------------------------------------------
# Source rendering (data-only block)
# ---------------------------------------------------------------------------


def render_advisory_block(mapping: dict[str, AdapterAdvisory]) -> str:
    """Render the ``ADAPTER_MIN_SAFE_VERSIONS`` dict literal as data.

    Deterministic and faithful: rendering the in-code map reproduces the
    current source block byte-for-byte (entries alphabetical), so a floor
    bump produced by regenerating from a feed is a data-only diff with no
    logic change. Content outside the block is never touched.
    """
    lines = ["ADAPTER_MIN_SAFE_VERSIONS: dict[str, AdapterAdvisory] = {"]
    for name in sorted(mapping):
        adv = mapping[name]
        lines.extend(
            (
                f'    "{name}": AdapterAdvisory(',
                f'        adapter="{adv.adapter}",',
                f'        min_safe_version="{adv.min_safe_version}",',
                f'        advisory_id="{adv.advisory_id}",',
                f'        note="{adv.note}",',
                "    ),",
            )
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_advisory_block(path: Path, mapping: dict[str, AdapterAdvisory]) -> None:
    """Regenerate the floor-map block between the markers in ``advisories.py``.

    Idempotent: regenerating with the same map leaves the file byte-identical.
    Content outside the markers is preserved verbatim.

    Raises:
        ValueError: If the file is missing either marker.
    """
    text = path.read_text(encoding="utf-8")
    begin = text.find(FLOOR_MAP_BEGIN)
    end = text.find(FLOOR_MAP_END)
    if -1 in (begin, end) or end < begin:
        raise ValueError(f"{path} is missing the floor-map markers")
    head = text[: begin + len(FLOOR_MAP_BEGIN)]
    tail = text[end:]
    block = render_advisory_block(mapping)
    path.write_text(f"{head}\n{block}{tail}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Signed update receipt
# ---------------------------------------------------------------------------


def build_floor_update_receipt(
    old: dict[str, AdapterAdvisory],
    new: dict[str, AdapterAdvisory],
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Bind a floor-map update into a canonical, content-addressed receipt.

    Determinism: a pure function of the two maps and the timestamp. The old
    and new floor-map content hashes let a verifier confirm exactly which map
    a spawn-preflight or version-posture receipt (which pin the same hash)
    referenced before and after the bump.
    """
    diff = diff_floor_maps(old, new)
    return {
        "schema_version": FLOOR_UPDATE_SCHEMA_VERSION,
        "kind": "adapter.floor_update_receipt",
        "old_floor_map_hash": hash_floor_map(old),
        "new_floor_map_hash": hash_floor_map(new),
        "diff": diff.to_dict(),
        "generated_at": generated_at,
    }


def verify_floor_update_receipt(doc: dict[str, Any]) -> bool:
    """Check a written update-receipt document against its content hash."""
    receipt = doc.get("receipt")
    recorded = doc.get("receipt_sha256")
    if not isinstance(receipt, dict) or not isinstance(recorded, str):
        return False
    return receipt_sha256(receipt) == recorded


def current_floor_map() -> dict[str, AdapterAdvisory]:
    """The in-code floor map (convenience for the refresh script)."""
    return dict(ADAPTER_MIN_SAFE_VERSIONS)
