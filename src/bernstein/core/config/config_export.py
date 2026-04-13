"""CFG-011: Config import/export for team sharing."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)
_REDACTED_MARKER = "<REDACTED>"
_SECRET_PATTERNS = ("secret", "token", "password", "key", "credential", "cert")
_EXPORT_META_KEY = "__bernstein_export__"


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


@dataclass(frozen=True, slots=True)
class ExportMeta:
    exported_at: str
    source_path: str
    checksum: str
    format_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "exported_at": self.exported_at,
            "source_path": self.source_path,
            "checksum": self.checksum,
            "format_version": self.format_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExportMeta:
        return cls(
            exported_at=str(data.get("exported_at", "")),
            source_path=str(data.get("source_path", "")),
            checksum=str(data.get("checksum", "")),
            format_version=int(data.get("format_version", 1)),
        )


@dataclass(frozen=True, slots=True)
class ImportResult:
    success: bool
    keys_imported: int = 0
    keys_skipped: int = 0
    warnings: list[str] = field(default_factory=list[str])
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "keys_imported": self.keys_imported,
            "keys_skipped": self.keys_skipped,
            "warnings": self.warnings,
            "error": self.error,
        }


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(pat in lowered for pat in _SECRET_PATTERNS)


def _redact_value(data: object, *, key_name: str = "") -> object:
    if _looks_secret(key_name) and isinstance(data, str) and data:
        return _REDACTED_MARKER
    if isinstance(data, dict):
        raw = cast(_CAST_DICT_STR_ANY, data)
        return {k: _redact_value(v, key_name=k) for k, v in raw.items()}
    if isinstance(data, list):
        raw_list = cast("list[Any]", data)
        return [_redact_value(item, key_name=key_name) for item in raw_list]
    return data


def _compute_checksum(data: dict[str, Any]) -> str:
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def export_config(
    config_path: Path, output_path: Path, *, fmt: Literal["yaml", "json"] = "yaml", redact_secrets: bool = True
) -> Path:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}
    config = cast(_CAST_DICT_STR_ANY, raw)
    checksum = _compute_checksum(config)
    if redact_secrets:
        config = cast(_CAST_DICT_STR_ANY, _redact_value(config))
    from datetime import UTC, datetime

    meta = ExportMeta(exported_at=datetime.now(tz=UTC).isoformat(), source_path=str(config_path), checksum=checksum)
    config[_EXPORT_META_KEY] = meta.to_dict()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output_path.write_text(json.dumps(config, indent=2, default=str), encoding="utf-8")
    else:
        output_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return output_path


def import_config(import_path: Path, target_path: Path, *, mode: Literal["replace", "merge"] = "merge") -> ImportResult:
    if not import_path.exists():
        return ImportResult(success=False, error=f"Import file not found: {import_path}")
    try:
        raw_text = import_path.read_text(encoding="utf-8")
        try:
            imported = json.loads(raw_text)
        except json.JSONDecodeError:
            imported = yaml.safe_load(raw_text)
        if not isinstance(imported, dict):
            return ImportResult(success=False, error="Imported file must be a YAML/JSON mapping")
        imported_config = cast(_CAST_DICT_STR_ANY, imported)
    except Exception as exc:
        return ImportResult(success=False, error=f"Failed to parse import file: {exc}")
    imported_config.pop(_EXPORT_META_KEY, None)
    warnings: list[str] = []
    skipped = 0

    def _count_redacted(data: object, path: str = "") -> object:
        nonlocal skipped
        if isinstance(data, str) and data == _REDACTED_MARKER:
            skipped += 1
            warnings.append(f"Skipped redacted value at '{path}'")
            return None
        if isinstance(data, dict):
            result: dict[str, Any] = {}
            for k, v in cast(_CAST_DICT_STR_ANY, data).items():
                cleaned = _count_redacted(v, f"{path}.{k}" if path else k)
                if cleaned is not None or not isinstance(v, str) or v != _REDACTED_MARKER:
                    result[k] = cleaned if cleaned is not None else v
            return result
        if isinstance(data, list):
            return [_count_redacted(item, f"{path}[{i}]") for i, item in enumerate(cast("list[Any]", data))]
        return data

    cleaned = _count_redacted(imported_config)
    if isinstance(cleaned, dict):
        imported_config = cast(_CAST_DICT_STR_ANY, cleaned)
    if mode == "replace":
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(yaml.dump(imported_config, default_flow_style=False, sort_keys=False), encoding="utf-8")
        keys_imported = len(imported_config)
    else:
        existing: dict[str, Any] = {}
        if target_path.exists():
            loaded = yaml.safe_load(target_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = cast(_CAST_DICT_STR_ANY, loaded)
        existing.update(imported_config)
        keys_imported = len(imported_config)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(yaml.dump(existing, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return ImportResult(success=True, keys_imported=keys_imported, keys_skipped=skipped, warnings=warnings)
