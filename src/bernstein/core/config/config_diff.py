"""Redacted config snapshotting and diffing for bernstein.yaml reloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeGuard, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

_REDACTED = "<redacted>"
_SECRET_MARKERS = ("secret", "token", "password", "key", "credential", "cert")
_SNAPSHOT_FILE = "config_snapshot.json"

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class RedactedSnapshot(TypedDict):
    """Serialized secret placeholder that preserves change detection."""

    __redacted__: str
    __fingerprint__: str


@dataclass(frozen=True)
class ConfigChange:
    """A single human-readable config change."""

    kind: Literal["added", "removed", "changed"]
    path: str
    before: str = ""
    after: str = ""

    def to_dict(self) -> dict[str, str]:
        """Serialize the change to a JSON-safe dict."""

        return {
            "kind": self.kind,
            "path": self.path,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class ConfigDiffSummary:
    """Summary of a config reload delta."""

    changed: bool
    changes: list[ConfigChange] = field(default_factory=lambda: [])
    added: int = 0
    removed: int = 0
    modified: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize the summary to a JSON-safe dict."""

        return {
            "changed": self.changed,
            "changes": [change.to_dict() for change in self.changes],
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "truncated": self.truncated,
        }


def load_redacted_config(path: Path | None) -> JsonValue:
    """Load and redact a YAML config file for safe diffing.

    Args:
        path: Path to ``bernstein.yaml``.

    Returns:
        The redacted parsed YAML structure, or an empty dict when missing/empty.
    """

    if path is None or not path.exists():
        return {}
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _redact_value(loaded)


def diff_config_snapshots(previous: JsonValue, current: JsonValue, *, max_changes: int = 12) -> ConfigDiffSummary:
    """Compute a redacted config diff between two parsed snapshots."""

    changes: list[ConfigChange] = []
    added = 0
    removed = 0
    modified = 0

    def walk(path: str, before: JsonValue, after: JsonValue) -> None:
        nonlocal added, removed, modified

        before_dict = _as_plain_object(before)
        after_dict = _as_plain_object(after)
        if before_dict is not None and after_dict is not None:
            keys = sorted(set(before_dict) | set(after_dict))
            for key in keys:
                next_path = f"{path}.{key}" if path else str(key)
                before_has = key in before_dict
                after_has = key in after_dict
                if before_has and after_has:
                    walk(next_path, before_dict[key], after_dict[key])
                elif before_has:
                    removed += 1
                    changes.append(ConfigChange("removed", next_path, before=_summarize(before_dict[key])))
                else:
                    added += 1
                    changes.append(ConfigChange("added", next_path, after=_summarize(after_dict[key])))
            return

        if before != after:
            modified += 1
            changes.append(
                ConfigChange(
                    "changed",
                    path or "<root>",
                    before=_summarize(before),
                    after=_summarize(after),
                )
            )

    walk("", _normalize_root(previous), _normalize_root(current))
    truncated = len(changes) > max_changes
    return ConfigDiffSummary(
        changed=bool(changes),
        changes=changes[:max_changes],
        added=added,
        removed=removed,
        modified=modified,
        truncated=truncated,
    )


def read_config_snapshot(sdd_dir: Path) -> JsonValue:
    """Read the previous redacted config snapshot from disk."""

    path = sdd_dir / "runtime" / _SNAPSHOT_FILE
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return _coerce_json_value(loaded)
    except (OSError, json.JSONDecodeError):
        return {}


def write_config_snapshot(sdd_dir: Path, snapshot: JsonValue) -> Path:
    """Persist the current redacted config snapshot to disk."""

    runtime_dir = sdd_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / _SNAPSHOT_FILE
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _redact_value(value: object, *, key_name: str = "") -> JsonValue:
    if _looks_secret(key_name):
        return {
            "__redacted__": _REDACTED,
            "__fingerprint__": _fingerprint(value),
        }
    if isinstance(value, dict):
        raw_dict = cast("dict[object, object]", value)
        redacted: dict[str, JsonValue] = {}
        for raw_key, item in raw_dict.items():
            key = str(raw_key)
            redacted[key] = _redact_value(item, key_name=key)
        return redacted
    if isinstance(value, list):
        raw_list = cast("list[object]", value)
        return [_redact_value(item, key_name=key_name) for item in raw_list]
    return _coerce_scalar(value)


def _looks_secret(key_name: str) -> bool:
    lowered = key_name.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _summarize(value: JsonValue) -> str:
    if _is_redacted_snapshot(value):
        return _REDACTED
    if isinstance(value, dict):
        return f"object({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    text = str(value)
    return text if len(text) <= 80 else f"{text[:77]}..."


def _normalize_root(value: JsonValue) -> JsonValue:
    return value if isinstance(value, (dict, list)) else {}


def _as_plain_object(value: JsonValue) -> dict[str, JsonValue] | None:
    if isinstance(value, dict) and not _is_redacted_snapshot(value):
        return value
    return None


def _is_redacted_snapshot(value: JsonValue) -> TypeGuard[RedactedSnapshot]:
    return (
        isinstance(value, dict)
        and set(value) == {"__redacted__", "__fingerprint__"}
        and isinstance(value.get("__redacted__"), str)
        and isinstance(value.get("__fingerprint__"), str)
    )


def _coerce_json_value(value: object) -> JsonValue:
    if isinstance(value, dict):
        raw_dict = cast("dict[object, object]", value)
        coerced: dict[str, JsonValue] = {}
        for raw_key, item in raw_dict.items():
            coerced[str(raw_key)] = _coerce_json_value(item)
        return cast("JsonValue", coerced)
    if isinstance(value, list):
        raw_list = cast("list[object]", value)
        return [_coerce_json_value(item) for item in raw_list]
    return _coerce_scalar(value)


def _coerce_scalar(value: object) -> JsonScalar:
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def _fingerprint(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
