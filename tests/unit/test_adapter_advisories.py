"""Tests for adapter minimum-safe-version advisories and the doctor check."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.adapters.advisories import (
    ADAPTER_MIN_SAFE_VERSIONS,
    AdapterAdvisory,
    check_adapter_version,
)


class TestCheckAdapterVersion:
    """Version comparison against the curated safe floor."""

    def test_below_floor_returns_advisory(self) -> None:
        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))
        floor = ADAPTER_MIN_SAFE_VERSIONS[name].min_safe_version
        below = "0.0.1"
        assert below != floor
        advisory = check_adapter_version(name, below)
        assert advisory is not None
        assert advisory.adapter == name

    def test_at_floor_returns_none(self) -> None:
        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))
        floor = ADAPTER_MIN_SAFE_VERSIONS[name].min_safe_version
        assert check_adapter_version(name, floor) is None

    def test_above_floor_returns_none(self) -> None:
        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))
        assert check_adapter_version(name, "999.0.0") is None

    def test_unknown_adapter_returns_none(self) -> None:
        assert check_adapter_version("no-such-adapter", "0.0.1") is None

    def test_none_version_returns_none(self) -> None:
        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))
        assert check_adapter_version(name, None) is None

    def test_unparseable_version_returns_none(self) -> None:
        """A garbled version string is treated as unknown, not below-floor."""
        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))
        assert check_adapter_version(name, "not-a-version") is None

    def test_advisory_carries_id_and_note(self) -> None:
        for advisory in ADAPTER_MIN_SAFE_VERSIONS.values():
            assert isinstance(advisory, AdapterAdvisory)
            assert advisory.advisory_id
            assert advisory.note


class TestDoctorAdapterAdvisories:
    """The doctor surface formats a warning row for a below-floor adapter."""

    def test_below_floor_adapter_produces_warn_row(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}" if binary == name else None

        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="0.0.1"),
        ):
            rows = doctor_cmd.check_adapter_advisories()

        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == f"Adapter version: {name}"
        assert row["status"] == "WARN"
        assert "below safe floor" in row["detail"]
        assert ADAPTER_MIN_SAFE_VERSIONS[name].advisory_id in row["detail"]

    def test_above_floor_adapter_produces_pass_row(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}" if binary == name else None

        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="999.0.0"),
        ):
            rows = doctor_cmd.check_adapter_advisories()

        assert len(rows) == 1
        assert rows[0]["status"] == "PASS"

    def test_unknown_version_produces_warn_row(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        name = next(iter(ADAPTER_MIN_SAFE_VERSIONS))

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}" if binary == name else None

        with (
            patch.object(doctor_cmd.shutil, "which", side_effect=fake_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value=None),
        ):
            rows = doctor_cmd.check_adapter_advisories()

        assert len(rows) == 1
        assert rows[0]["status"] == "WARN"
        assert "version unknown" in rows[0]["detail"]

    def test_uninstalled_adapters_omitted(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        with patch.object(doctor_cmd.shutil, "which", return_value=None):
            rows = doctor_cmd.check_adapter_advisories()

        assert rows == []
