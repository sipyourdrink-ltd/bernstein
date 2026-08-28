from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PackEntry:
    path: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class TaskContextPack:
    entries: list[PackEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [entry.to_dict() for entry in self.entries]}

    def canonical_bytes(self) -> bytes:
        """
        Serializes the pack into deterministic bytes.
        Duplicate paths are rejected and raise a ValueError.
        """
        seen_paths = set()
        for entry in self.entries:
            if entry.path in seen_paths:
                raise ValueError(f"Duplicate path detected: {entry.path}")
            seen_paths.add(entry.path)

        # Sort deterministically by path before JSON serialization
        sorted_entries = sorted(self.entries, key=lambda e: e.path)

        return json.dumps(
            TaskContextPack(entries=sorted_entries).to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
