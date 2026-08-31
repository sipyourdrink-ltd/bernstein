"""Tests for deterministic normalization of recorded Gitleaks SARIF."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters._contract import ScannerDeterminism, scanner_determinism
from bernstein.adapters.gitleaks import (
    GitleaksAdapter,
    GitleaksError,
    GitleaksNotInstalledError,
    _invocation_argv_hash,
    _ruleset_digest,
    parse_gitleaks_sarif,
)
from bernstein.adapters.scanner import DeterminismTier, OutputFormat, ScannerCategory, ScanScope
from bernstein.adapters.scanner_conformance import (
    ScannerConformanceHarness,
    load_scanner_golden_transcripts,
)
from bernstein.adapters.scanner_registry import get_scanner

_FIXTURE = Path("tests/fixtures/scanners/gitleaks/gitleaks-8.30.1.sarif")
_CONFIG = Path("tests/fixtures/scanners/gitleaks/gitleaks.toml")
_FIXTURE_DIR = _FIXTURE.parent
_SYNTHETIC_SECRET = "bernstein_test_secret_abcdefghijklmnopqrst"


def _fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _fake_gitleaks_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if argv[1:] == ["version"]:
        return subprocess.CompletedProcess(argv, 0, stdout="8.30.1\n", stderr="")
    report_path = Path(argv[argv.index("--report-path") + 1])
    report_path.write_text(_fixture_text(), encoding="utf-8")
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="leaks found")


def test_real_gitleaks_sarif_is_normalized_to_a_finding() -> None:
    finding = parse_gitleaks_sarif(_fixture_text())[0]

    assert finding.rule == "bernstein-test-token"
    assert finding.path == "app.env"
    assert finding.severity == "informational"
    assert finding.summary == "Bernstein synthetic test token"
    assert finding.extra["snippet_hash"].startswith("sha256:")
    assert _SYNTHETIC_SECRET not in json.dumps(finding.to_dict())


def test_two_parses_of_the_same_recorded_run_have_identical_hashes() -> None:
    first = [finding.finding_hash() for finding in parse_gitleaks_sarif(_fixture_text())]
    second = [finding.finding_hash() for finding in parse_gitleaks_sarif(_fixture_text())]

    assert first == second


def test_cosmetic_line_shift_does_not_change_the_finding_hash() -> None:
    original = json.loads(_fixture_text())
    shifted = copy.deepcopy(original)
    region = shifted["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    region["startLine"] += 7
    region["endLine"] += 7

    original_hashes = [finding.finding_hash() for finding in parse_gitleaks_sarif(json.dumps(original))]
    shifted_hashes = [finding.finding_hash() for finding in parse_gitleaks_sarif(json.dumps(shifted))]

    assert shifted_hashes == original_hashes


def test_absolute_report_path_is_normalized_to_the_scan_target() -> None:
    report = json.loads(_fixture_text())
    artifact = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]
    artifact["uri"] = "/checkout/project/app.env"

    finding = parse_gitleaks_sarif(json.dumps(report), target_root=Path("/checkout/project"))[0]

    assert finding.path == "app.env"


def test_non_gitleaks_sarif_is_rejected() -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["tool"]["driver"]["name"] = "another-scanner"

    with pytest.raises(ValueError, match="must be 'gitleaks'"):
        parse_gitleaks_sarif(json.dumps(report))


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid Gitleaks SARIF JSON"):
        parse_gitleaks_sarif("not-json")


def test_adapter_declares_the_deterministic_secret_scanner_contract() -> None:
    adapter = GitleaksAdapter(config_path=_CONFIG)

    assert adapter.name() == "gitleaks"
    assert adapter.output_format is OutputFormat.SARIF
    assert adapter.determinism is DeterminismTier.DETERMINISTIC
    assert adapter.category is ScannerCategory.SECRET
    assert scanner_determinism(adapter.name()) is ScannerDeterminism.DETERMINISTIC


def test_scanner_registry_resolves_gitleaks() -> None:
    assert isinstance(get_scanner("gitleaks"), GitleaksAdapter)


def test_invocation_hash_binds_version_and_ruleset() -> None:
    baseline = _invocation_argv_hash("8.30.1", "sha256:rules-a")

    assert _invocation_argv_hash("8.30.1", "sha256:rules-a") == baseline
    assert _invocation_argv_hash("8.30.2", "sha256:rules-a") != baseline
    assert _invocation_argv_hash("8.30.1", "sha256:rules-b") != baseline


def test_ignore_file_is_bound_into_the_ruleset_digest(tmp_path: Path) -> None:
    ignore = tmp_path / ".gitleaksignore"
    ignore.write_text("app.env:bernstein-test-token:1\n", encoding="utf-8")

    without_ignore = _ruleset_digest("unused-binary", _CONFIG)
    with_ignore = _ruleset_digest("unused-binary", _CONFIG, ignore)

    assert with_ignore != without_ignore


def test_scan_runs_gitleaks_and_accepts_findings_exit_code(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    adapter = GitleaksAdapter(config_path=_CONFIG)

    with (
        patch("bernstein.adapters.gitleaks.shutil.which", return_value="/usr/local/bin/gitleaks"),
        patch("bernstein.adapters.gitleaks.subprocess.run", side_effect=_fake_gitleaks_run) as run,
    ):
        result = adapter.scan(target, ScanScope(roots=(target,)), tmp_path / "work")

    expected_hash = parse_gitleaks_sarif(_fixture_text())[0].finding_hash()
    assert result.finding_hashes() == [expected_hash]
    scan_argv = run.call_args_list[1].args[0]
    assert scan_argv[1:5] == ["dir", "--no-banner", "--report-format", "sarif"]
    assert "--config" in scan_argv
    assert scan_argv[-1] == str(target.resolve())
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tool_version == "8.30.1"
    assert adapter.last_invocation.ruleset_digest == "sha256:" + hashlib.sha256(_CONFIG.read_bytes()).hexdigest()
    assert not (tmp_path / "work" / "gitleaks.sarif").exists()


def test_target_local_config_is_explicitly_pinned(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    local_config = target / ".gitleaks.toml"
    local_config.write_bytes(_CONFIG.read_bytes())
    adapter = GitleaksAdapter()

    with (
        patch("bernstein.adapters.gitleaks.shutil.which", return_value="/usr/local/bin/gitleaks"),
        patch("bernstein.adapters.gitleaks.subprocess.run", side_effect=_fake_gitleaks_run) as run,
    ):
        adapter.scan(target, ScanScope(roots=(target,)), tmp_path / "work")

    scan_argv = run.call_args_list[1].args[0]
    assert scan_argv[scan_argv.index("--config") + 1] == str(local_config.resolve())
    assert scan_argv[scan_argv.index("--gitleaks-ignore-path") + 1] == str(target.resolve())
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.ruleset_digest == "sha256:" + hashlib.sha256(local_config.read_bytes()).hexdigest()


def test_stale_report_cannot_be_reused(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "gitleaks.sarif").write_text(_fixture_text(), encoding="utf-8")

    def no_report(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="8.30.1\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    adapter = GitleaksAdapter(config_path=_CONFIG)
    with (
        patch("bernstein.adapters.gitleaks.shutil.which", return_value="/usr/local/bin/gitleaks"),
        patch("bernstein.adapters.gitleaks.subprocess.run", side_effect=no_report),
        pytest.raises(GitleaksError, match="without writing its SARIF report"),
    ):
        adapter.scan(tmp_path, ScanScope(), workdir)


def test_scan_reports_missing_gitleaks_without_invoking_a_process(tmp_path: Path) -> None:
    adapter = GitleaksAdapter(config_path=_CONFIG)
    with (
        patch("bernstein.adapters.gitleaks.shutil.which", return_value=None),
        patch("bernstein.adapters.gitleaks.subprocess.run") as run,
        pytest.raises(GitleaksNotInstalledError, match="not found on PATH"),
    ):
        adapter.scan(tmp_path, ScanScope(), tmp_path / "work")
    run.assert_not_called()


def test_scan_rejects_a_real_gitleaks_execution_error(tmp_path: Path) -> None:
    def fail_scan(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="8.30.1\n", stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad config")

    adapter = GitleaksAdapter(config_path=_CONFIG)
    with (
        patch("bernstein.adapters.gitleaks.shutil.which", return_value="/usr/local/bin/gitleaks"),
        patch("bernstein.adapters.gitleaks.subprocess.run", side_effect=fail_scan),
        pytest.raises(GitleaksError, match="code 2: bad config"),
    ):
        adapter.scan(tmp_path, ScanScope(), tmp_path / "work")


def test_conformance_replays_two_identical_gitleaks_runs(tmp_path: Path) -> None:
    transcripts = load_scanner_golden_transcripts(_FIXTURE_DIR)
    assert len(transcripts) == 1

    with (
        patch("bernstein.adapters.gitleaks.shutil.which", return_value="/usr/local/bin/gitleaks"),
        patch("bernstein.adapters.gitleaks.subprocess.run", side_effect=_fake_gitleaks_run) as run,
    ):
        result = ScannerConformanceHarness().replay_transcript(transcripts[0], workdir=tmp_path)

    assert result.passed
    assert result.adapter_name == "gitleaks"
    assert result.determinism_tier is DeterminismTier.DETERMINISTIC
    assert sum(call.args[0][1] == "dir" for call in run.call_args_list) == 2
