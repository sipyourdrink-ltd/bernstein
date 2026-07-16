"""Doctor version-posture becomes a signed, chain-anchored receipt (#2515)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS
from bernstein.adapters.security_floor import floor_map_content_hash, receipt_sha256
from bernstein.cli.commands import doctor_cmd
from bernstein.core.security.audit_chain import (
    EVENT_ADAPTER_VERSION_POSTURE,
    AuditChainStore,
)

_ADAPTER = next(iter(ADAPTER_MIN_SAFE_VERSIONS))


def _fake_which(name: str) -> str | None:
    return f"/usr/bin/{name}" if name == _ADAPTER else None


class TestCollectPosture:
    def test_below_floor_entry(self) -> None:
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=_fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="0.0.1"),
        ):
            entries = doctor_cmd.collect_version_posture()
        assert len(entries) == 1
        assert entries[0]["adapter"] == _ADAPTER
        assert entries[0]["verdict"] == "below_floor"

    def test_console_rows_project_posture(self) -> None:
        # check_adapter_advisories is a projection of collect_version_posture.
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=_fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="0.0.1"),
        ):
            rows = doctor_cmd.check_adapter_advisories()
        assert len(rows) == 1
        assert rows[0]["status"] == "WARN"
        assert "below safe floor" in rows[0]["detail"]


class TestReceipt:
    def test_receipt_is_deterministic_modulo_timestamp(self) -> None:
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=_fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="0.0.1"),
        ):
            entries = doctor_cmd.collect_version_posture()
        a = doctor_cmd.build_version_posture_receipt(entries, generated_at="t")
        b = doctor_cmd.build_version_posture_receipt(entries, generated_at="t")
        assert a == b
        assert receipt_sha256(a) == receipt_sha256(b)
        assert a["floor_map_hash"] == floor_map_content_hash()

    def test_emit_anchors_receipt_into_chain(self, tmp_path: Path) -> None:
        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=_fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="0.0.1"),
        ):
            sealed = doctor_cmd.emit_version_posture_receipt(tmp_path)
        assert sealed["anchored"] is True
        assert sealed["entries"][0]["verdict"] == "below_floor"

        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        rows = chain.query(event_type=EVENT_ADAPTER_VERSION_POSTURE)
        assert len(rows) == 1
        assert rows[0].details["receipt_sha256"] == sealed["receipt_sha256"]
        assert rows[0].details["floor_map_hash"] == floor_map_content_hash()
        assert chain.verify()[0]

    def test_no_tracked_adapter_installed_seals_empty_but_verifies(self, tmp_path: Path) -> None:
        with patch.object(doctor_cmd.shutil, "which", return_value=None):
            sealed = doctor_cmd.emit_version_posture_receipt(tmp_path)
        assert sealed["entries"] == []
        assert sealed["anchored"] is True
