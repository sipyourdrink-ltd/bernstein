"""Tests for feed-pinned normalization of recorded Trivy SARIF."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters._contract import ScannerDeterminism, scanner_determinism, scanner_pinned_inputs
from bernstein.adapters.scanner import DeterminismTier, OutputFormat, ScannerCategory, ScanScope
from bernstein.adapters.scanner_conformance import ScannerConformanceHarness, load_scanner_golden_transcripts
from bernstein.adapters.scanner_registry import get_scanner
from bernstein.adapters.trivy import (
    TrivyAdapter,
    TrivyError,
    TrivyNotInstalledError,
    _db_identity,
    _invocation_argv_hash,
    _resolve_cache_dir,
    parse_trivy_sarif,
)

_FIXTURE = Path("tests/fixtures/scanners/trivy/trivy-0.74.0.sarif")
_FIXTURE_DIR = _FIXTURE.parent
_TARGET = _FIXTURE_DIR / "target"
_DB_IDENTITY = "sha256:633b3bacaa4a03f502faef8f94ce2d20d96a8499405d266ed7f67879109ec80d"
_OTHER_DB_IDENTITY = "sha256:" + "a" * 64


def _fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _scope(db_pin: str = _DB_IDENTITY) -> ScanScope:
    return ScanScope(roots=(_TARGET,), config={"db_pin": db_pin})


def _fake_trivy_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if argv[1:] == ["--version"]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Version: 0.74.0\nVulnerability DB:\n  Version: 2\n",
            stderr="",
        )
    report_path = Path(argv[argv.index("--output") + 1])
    report_path.write_text(_fixture_text(), encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_real_trivy_sarif_is_normalized_to_findings() -> None:
    findings = parse_trivy_sarif(_fixture_text())

    assert len(findings) == 5
    assert findings[0].rule == "CVE-2021-23337"
    assert findings[0].path == "package-lock.json"
    assert findings[0].severity == "high"
    assert "lodash" in findings[0].summary


def test_two_parses_of_the_same_recorded_run_have_identical_hashes() -> None:
    first = [finding.finding_hash() for finding in parse_trivy_sarif(_fixture_text())]
    second = [finding.finding_hash() for finding in parse_trivy_sarif(_fixture_text())]

    assert first == second


def test_cosmetic_line_shift_does_not_change_the_finding_hash() -> None:
    original = json.loads(_fixture_text())
    shifted = copy.deepcopy(original)
    for result in shifted["runs"][0]["results"]:
        region = result["locations"][0]["physicalLocation"]["region"]
        region["startLine"] += 7
        region["endLine"] += 7

    original_hashes = [finding.finding_hash() for finding in parse_trivy_sarif(json.dumps(original))]
    shifted_hashes = [finding.finding_hash() for finding in parse_trivy_sarif(json.dumps(shifted))]

    assert shifted_hashes == original_hashes


def test_absolute_report_path_is_normalized_to_the_scan_target() -> None:
    report = json.loads(_fixture_text())
    for result in report["runs"][0]["results"]:
        artifact = result["locations"][0]["physicalLocation"]["artifactLocation"]
        artifact["uri"] = "/checkout/project/package-lock.json"

    finding = parse_trivy_sarif(json.dumps(report), target_root=Path("/checkout/project"))[0]

    assert finding.path == "package-lock.json"


def test_non_trivy_sarif_is_rejected() -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["tool"]["driver"]["name"] = "another-scanner"

    with pytest.raises(ValueError, match="must be 'Trivy'"):
        parse_trivy_sarif(json.dumps(report))


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid Trivy SARIF JSON"):
        parse_trivy_sarif("not-json")


def test_adapter_declares_the_feed_pinned_sca_contract() -> None:
    adapter = TrivyAdapter()

    assert adapter.name() == "trivy"
    assert adapter.output_format is OutputFormat.SARIF
    assert adapter.determinism is DeterminismTier.FEED_PINNED
    assert adapter.pinned_inputs == ("trivy_db",)
    assert adapter.category is ScannerCategory.SCA
    assert scanner_determinism(adapter.name()) is ScannerDeterminism.FEED_PINNED
    assert scanner_pinned_inputs(adapter.name()) == ("trivy_db",)


def test_scanner_registry_resolves_trivy() -> None:
    assert isinstance(get_scanner("trivy"), TrivyAdapter)


def test_invocation_hash_binds_version_and_db_identity() -> None:
    baseline = _invocation_argv_hash("0.74.0", _DB_IDENTITY)

    assert _invocation_argv_hash("0.74.0", _DB_IDENTITY) == baseline
    assert _invocation_argv_hash("0.74.1", _DB_IDENTITY) != baseline
    assert _invocation_argv_hash("0.74.0", _OTHER_DB_IDENTITY) != baseline


def test_feed_pinned_scan_requires_a_db_pin(tmp_path: Path) -> None:
    with (
        patch("bernstein.adapters.trivy.shutil.which") as which,
        pytest.raises(TrivyError, match=r"require scope.config\['db_pin'\]"),
    ):
        TrivyAdapter().scan(tmp_path, ScanScope(), tmp_path / "work")
    which.assert_not_called()


def test_scan_runs_trivy_with_database_updates_disabled(tmp_path: Path) -> None:
    adapter = TrivyAdapter(cache_dir=tmp_path / "cache")

    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value="/usr/local/bin/trivy"),
        patch("bernstein.adapters.trivy._db_identity", return_value=_DB_IDENTITY),
        patch("bernstein.adapters.trivy.subprocess.run", side_effect=_fake_trivy_run) as run,
    ):
        result = adapter.scan(_TARGET, _scope(), tmp_path / "work")

    assert result.finding_hashes() == sorted(f.finding_hash() for f in parse_trivy_sarif(_fixture_text()))
    assert result.feed_digest == _DB_IDENTITY
    scan_argv = run.call_args_list[1].args[0]
    assert scan_argv[1:5] == ["filesystem", "--scanners", "vuln", "--skip-db-update"]
    assert scan_argv[-1] == str(_TARGET.resolve())
    assert "--cache-dir" in scan_argv
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tool_version == "0.74.0"
    assert adapter.last_invocation.db_pin == _DB_IDENTITY
    assert adapter.last_invocation.db_identity == _DB_IDENTITY
    assert not (tmp_path / "work" / "trivy.sarif").exists()


def test_a_changed_db_identity_is_recorded_not_absorbed(tmp_path: Path) -> None:
    first_adapter = TrivyAdapter(cache_dir=tmp_path / "first-cache")
    second_adapter = TrivyAdapter(cache_dir=tmp_path / "second-cache")
    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value="/usr/local/bin/trivy"),
        patch("bernstein.adapters.trivy._db_identity", side_effect=[_DB_IDENTITY, _OTHER_DB_IDENTITY]),
        patch("bernstein.adapters.trivy.subprocess.run", side_effect=_fake_trivy_run),
    ):
        first = first_adapter.scan(_TARGET, _scope(), tmp_path / "first")
        second = second_adapter.scan(_TARGET, _scope(_OTHER_DB_IDENTITY), tmp_path / "second")

    assert first.finding_hashes() == second.finding_hashes()
    assert first.feed_digest == _DB_IDENTITY
    assert second.feed_digest == _OTHER_DB_IDENTITY
    assert first_adapter.last_invocation is not None
    assert second_adapter.last_invocation is not None
    assert first_adapter.last_invocation.db_identity == _DB_IDENTITY
    assert second_adapter.last_invocation.db_identity == _OTHER_DB_IDENTITY
    assert first_adapter.last_invocation.argv_hash != second_adapter.last_invocation.argv_hash


def test_a_db_pin_mismatch_fails_before_trivy_runs(tmp_path: Path) -> None:
    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value="/usr/local/bin/trivy"),
        patch("bernstein.adapters.trivy._db_identity", return_value=_OTHER_DB_IDENTITY),
        patch("bernstein.adapters.trivy.subprocess.run") as run,
        pytest.raises(TrivyError, match=f"expected {_DB_IDENTITY}, observed {_OTHER_DB_IDENTITY}"),
    ):
        TrivyAdapter(cache_dir=tmp_path / "cache").scan(_TARGET, _scope(), tmp_path / "work")
    run.assert_not_called()


def test_db_identity_hashes_the_database_file_bytes(tmp_path: Path) -> None:
    database = tmp_path / "db" / "trivy.db"
    database.parent.mkdir()
    database.write_bytes(b"recorded trivy database bytes")

    assert _db_identity(tmp_path) == "sha256:" + hashlib.sha256(database.read_bytes()).hexdigest()


def test_db_identity_rejects_a_missing_database(tmp_path: Path) -> None:
    with pytest.raises(TrivyError, match="database does not exist"):
        _db_identity(tmp_path)


def test_a_missing_database_fails_before_trivy_runs(tmp_path: Path) -> None:
    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value="/usr/local/bin/trivy"),
        patch("bernstein.adapters.trivy.subprocess.run") as run,
        pytest.raises(TrivyError, match="database does not exist"),
    ):
        TrivyAdapter(cache_dir=tmp_path / "cache").scan(_TARGET, _scope(), tmp_path / "work")
    run.assert_not_called()


def test_default_cache_directory_matches_trivy_on_macos() -> None:
    with (
        patch("bernstein.adapters.trivy.sys.platform", "darwin"),
        patch.dict("bernstein.adapters.trivy.os.environ", {"HOME": "/Users/example"}, clear=True),
    ):
        cache_dir = _resolve_cache_dir(None)

    assert cache_dir == Path("/Users/example/Library/Caches/trivy")


def test_default_cache_directory_uses_xdg_cache_home_on_linux() -> None:
    with (
        patch("bernstein.adapters.trivy.sys.platform", "linux"),
        patch("bernstein.adapters.trivy.os.name", "posix"),
        patch.dict("bernstein.adapters.trivy.os.environ", {"XDG_CACHE_HOME": "/var/cache/example"}, clear=True),
    ):
        cache_dir = _resolve_cache_dir(None)

    assert cache_dir == Path("/var/cache/example/trivy")


def test_default_cache_directory_matches_trivys_temp_fallback() -> None:
    with (
        patch("bernstein.adapters.trivy.sys.platform", "linux"),
        patch("bernstein.adapters.trivy.os.name", "posix"),
        patch.dict("bernstein.adapters.trivy.os.environ", {}, clear=True),
        patch("bernstein.adapters.trivy.tempfile.gettempdir", return_value="/tmp/example"),
    ):
        cache_dir = _resolve_cache_dir(None)

    assert cache_dir == Path("/tmp/example/trivy")


def test_scan_reports_missing_trivy_without_invoking_a_process(tmp_path: Path) -> None:
    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value=None),
        patch("bernstein.adapters.trivy.subprocess.run") as run,
        pytest.raises(TrivyNotInstalledError, match="not found on PATH"),
    ):
        TrivyAdapter().scan(_TARGET, _scope(), tmp_path / "work")
    run.assert_not_called()


def test_stale_report_cannot_be_reused(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "trivy.sarif").write_text(_fixture_text(), encoding="utf-8")

    def no_report(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Version: 0.74.0\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value="/usr/local/bin/trivy"),
        patch("bernstein.adapters.trivy._db_identity", return_value=_DB_IDENTITY),
        patch("bernstein.adapters.trivy.subprocess.run", side_effect=no_report),
        pytest.raises(TrivyError, match="without writing its SARIF report"),
    ):
        TrivyAdapter().scan(_TARGET, _scope(), workdir)


def test_scan_rejects_a_real_trivy_execution_error(tmp_path: Path) -> None:
    def fail_scan(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Version: 0.74.0\n", stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="database unavailable")

    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value="/usr/local/bin/trivy"),
        patch("bernstein.adapters.trivy._db_identity", return_value=_DB_IDENTITY),
        patch("bernstein.adapters.trivy.subprocess.run", side_effect=fail_scan),
        pytest.raises(TrivyError, match="code 2: database unavailable"),
    ):
        TrivyAdapter().scan(_TARGET, _scope(), tmp_path / "work")


def test_conformance_replays_two_runs_with_the_same_db_pin(tmp_path: Path) -> None:
    transcripts = load_scanner_golden_transcripts(_FIXTURE_DIR)
    assert len(transcripts) == 1

    with (
        patch("bernstein.adapters.trivy.shutil.which", return_value="/usr/local/bin/trivy"),
        patch("bernstein.adapters.trivy._db_identity", return_value=_DB_IDENTITY),
        patch("bernstein.adapters.trivy.subprocess.run", side_effect=_fake_trivy_run) as run,
    ):
        result = ScannerConformanceHarness().replay_transcript(transcripts[0], workdir=tmp_path)

    assert result.passed
    assert result.adapter_name == "trivy"
    assert result.determinism_tier is DeterminismTier.FEED_PINNED
    assert [step.feed_digest for step in result.step_results] == [_DB_IDENTITY, _DB_IDENTITY]
    assert sum(call.args[0][1] == "filesystem" for call in run.call_args_list) == 2
