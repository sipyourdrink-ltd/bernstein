"""Tests for ``bernstein identity export-verifier``.

Covers issue #5115 CLI surface:
- each supported target writes the JWKS to its documented location
- re-running with the same key prints the unchanged message and does not rewrite
- --dry-run prints the destination without writing
- default target is local; unknown target is rejected
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.identity_cmd import identity_group
from bernstein.core.identity import http_signing


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestExportVerifierWritesToDocumentedLocationPerPlatform:
    @pytest.mark.parametrize("target", ["local", "server"])
    def test_writes_jwks_to_the_documented_path(
        self, runner: CliRunner, tmp_path: Path, monkeypatch, target: str
    ) -> None:
        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = runner.invoke(identity_group, ["export-verifier", f"--target={target}"])
        assert result.exit_code == 0, result.output
        assert f"wrote: {tmp_path}/.config/bernstein/verifier/{target}.json" in result.output

        dest = tmp_path / ".config" / "bernstein" / "verifier" / f"{target}.json"
        sidecar = tmp_path / ".config" / "bernstein" / "verifier" / f"{target}.json.sha256"

        assert dest.exists(), f"{dest} not found"
        assert sidecar.exists(), f"{sidecar} not found"

        payload = json.loads(dest.read_text(encoding="utf-8"))
        assert "keys" in payload
        assert payload["keys"]
        jwk = payload["keys"][0]
        assert jwk["crv"] == "Ed25519"
        assert jwk["kty"] == "OKP"

        sidecar_hash = sidecar.read_text().strip()
        computed = hashlib.sha256(dest.read_text(encoding="utf-8").encode("ascii")).hexdigest()
        assert sidecar_hash == computed


class TestExportVerifierRerunWithUnchangedKeyWritesNothingAndSaysSo:
    def test_rerun_is_a_noop_when_key_unchanged(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result1 = runner.invoke(identity_group, ["export-verifier"])
        assert result1.exit_code == 0, result1.output
        assert "wrote:" in result1.output

        dest = tmp_path / ".config" / "bernstein" / "verifier" / "local.json"
        sidecar = tmp_path / ".config" / "bernstein" / "verifier" / "local.json.sha256"
        mtime_before = dest.stat().st_mtime
        sidecar_mtime_before = sidecar.stat().st_mtime

        time.sleep(0.05)

        result2 = runner.invoke(identity_group, ["export-verifier"])
        assert result2.exit_code == 0, result2.output
        assert "unchanged:" in result2.output

        assert dest.stat().st_mtime == mtime_before, "dest was rewritten"
        assert sidecar.stat().st_mtime == sidecar_mtime_before, "sidecar was rewritten"

    def test_rerun_writes_when_key_changed(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result1 = runner.invoke(identity_group, ["export-verifier"])
        assert result1.exit_code == 0, result1.output

        dest = tmp_path / ".config" / "bernstein" / "verifier" / "local.json"

        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys2"))
        result2 = runner.invoke(identity_group, ["export-verifier"])
        assert result2.exit_code == 0, result2.output
        assert "wrote:" in result2.output

        assert dest.exists()


class TestExportVerifierDryRun:
    @pytest.mark.parametrize("target", ["local", "server"])
    def test_dry_run_does_not_touch_disk(
        self, runner: CliRunner, tmp_path: Path, monkeypatch, target: str
    ) -> None:
        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = runner.invoke(identity_group, ["export-verifier", f"--target={target}", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert f"{tmp_path}/.config/bernstein/verifier/{target}.json" in result.output

        dest = tmp_path / ".config" / "bernstein" / "verifier" / f"{target}.json"
        assert not dest.exists(), "dry-run wrote to disk"

    def test_dry_run_then_real_run_writes(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result_dry = runner.invoke(identity_group, ["export-verifier", "--dry-run"])
        assert result_dry.exit_code == 0

        result_real = runner.invoke(identity_group, ["export-verifier"])
        assert result_real.exit_code == 0, result_real.output
        assert "wrote:" in result_real.output

        dest = tmp_path / ".config" / "bernstein" / "verifier" / "local.json"
        assert dest.exists()


class TestExportVerifierTargetChoice:
    def test_default_target_is_local(self, runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = runner.invoke(identity_group, ["export-verifier"])
        assert result.exit_code == 0, result.output
        assert "local.json" in result.output

    def test_unknown_target_is_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(identity_group, ["export-verifier", "--target=unknown"])
        assert result.exit_code != 0
