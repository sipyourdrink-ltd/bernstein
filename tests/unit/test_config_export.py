"""Tests for bernstein.core.config_export (CFG-011)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from bernstein.core.config_export import (
    ExportMeta,
    export_config,
    import_config,
)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data), encoding="utf-8")


class TestExportConfig:
    def test_export_yaml(self, tmp_path: Path) -> None:
        src = tmp_path / "bernstein.yaml"
        _write_yaml(src, {"goal": "test", "max_agents": 4})
        out = tmp_path / "export.yaml"
        result = export_config(src, out)
        assert result == out
        assert out.exists()
        loaded = yaml.safe_load(out.read_text())
        assert loaded["goal"] == "test"
        assert "__bernstein_export__" in loaded

    def test_export_json(self, tmp_path: Path) -> None:
        src = tmp_path / "bernstein.yaml"
        _write_yaml(src, {"goal": "test"})
        out = tmp_path / "export.json"
        export_config(src, out, fmt="json")
        loaded = json.loads(out.read_text())
        assert loaded["goal"] == "test"

    def test_export_redacts_secrets(self, tmp_path: Path) -> None:
        src = tmp_path / "bernstein.yaml"
        _write_yaml(src, {"goal": "test", "auth_token": "super-secret"})
        out = tmp_path / "export.yaml"
        export_config(src, out)
        loaded = yaml.safe_load(out.read_text())
        assert loaded["auth_token"] == "<REDACTED>"

    def test_export_no_redaction(self, tmp_path: Path) -> None:
        src = tmp_path / "bernstein.yaml"
        _write_yaml(src, {"goal": "test", "auth_token": "visible"})
        out = tmp_path / "export.yaml"
        export_config(src, out, redact_secrets=False)
        loaded = yaml.safe_load(out.read_text())
        assert loaded["auth_token"] == "visible"

    def test_export_missing_file_raises(self, tmp_path: Path) -> None:
        out = tmp_path / "export.yaml"
        with pytest.raises(FileNotFoundError):
            export_config(tmp_path / "nonexistent.yaml", out)

    def test_export_meta_included(self, tmp_path: Path) -> None:
        src = tmp_path / "bernstein.yaml"
        _write_yaml(src, {"goal": "test"})
        out = tmp_path / "export.yaml"
        export_config(src, out)
        loaded = yaml.safe_load(out.read_text())
        meta = loaded["__bernstein_export__"]
        assert "exported_at" in meta
        assert "checksum" in meta


class TestImportConfig:
    def test_import_merge(self, tmp_path: Path) -> None:
        target = tmp_path / "bernstein.yaml"
        _write_yaml(target, {"goal": "original", "max_agents": 6})

        imp = tmp_path / "import.yaml"
        _write_yaml(imp, {"max_agents": 10, "model": "opus"})

        result = import_config(imp, target, mode="merge")
        assert result.success
        assert result.keys_imported == 2

        loaded = yaml.safe_load(target.read_text())
        assert loaded["goal"] == "original"
        assert loaded["max_agents"] == 10
        assert loaded["model"] == "opus"

    def test_import_replace(self, tmp_path: Path) -> None:
        target = tmp_path / "bernstein.yaml"
        _write_yaml(target, {"goal": "original", "max_agents": 6})

        imp = tmp_path / "import.yaml"
        _write_yaml(imp, {"goal": "new"})

        result = import_config(imp, target, mode="replace")
        assert result.success
        loaded = yaml.safe_load(target.read_text())
        assert loaded["goal"] == "new"
        assert "max_agents" not in loaded

    def test_import_skips_redacted(self, tmp_path: Path) -> None:
        target = tmp_path / "bernstein.yaml"
        _write_yaml(target, {"goal": "test"})

        imp = tmp_path / "import.yaml"
        _write_yaml(imp, {"goal": "new", "auth_token": "<REDACTED>"})

        result = import_config(imp, target, mode="merge")
        assert result.success
        assert result.keys_skipped > 0

    def test_import_missing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "bernstein.yaml"
        result = import_config(tmp_path / "nonexistent.yaml", target)
        assert not result.success
        assert "not found" in result.error

    def test_import_json_format(self, tmp_path: Path) -> None:
        target = tmp_path / "bernstein.yaml"
        _write_yaml(target, {"goal": "original"})

        imp = tmp_path / "import.json"
        imp.write_text(json.dumps({"goal": "from_json", "max_agents": 3}))

        result = import_config(imp, target, mode="merge")
        assert result.success
        loaded = yaml.safe_load(target.read_text())
        assert loaded["goal"] == "from_json"


class TestExportMeta:
    def test_round_trip(self) -> None:
        meta = ExportMeta(
            exported_at="2025-01-01T00:00:00Z",
            source_path="/test.yaml",
            checksum="abc123",
        )
        d = meta.to_dict()
        restored = ExportMeta.from_dict(d)
        assert restored.exported_at == meta.exported_at
        assert restored.checksum == meta.checksum
