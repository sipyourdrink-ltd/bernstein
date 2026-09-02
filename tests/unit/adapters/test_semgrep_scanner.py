"""Tests for deterministic normalization of recorded Semgrep SARIF."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters._contract import ScannerDeterminism, scanner_determinism
from bernstein.adapters.scanner import DeterminismTier, OutputFormat, ScannerCategory, ScanScope
from bernstein.adapters.scanner_conformance import (
    ScannerConformanceHarness,
    load_scanner_golden_transcripts,
)
from bernstein.adapters.scanner_registry import get_scanner
from bernstein.adapters.semgrep import (
    SemgrepAdapter,
    SemgrepError,
    SemgrepNotInstalledError,
    _invocation_argv_hash,
    _ruleset_digest,
    parse_semgrep_sarif,
)

_FIXTURE = Path("tests/fixtures/scanners/semgrep/semgrep-1.45.0.sarif")
_CONFIG = Path("tests/fixtures/scanners/semgrep/semgrep-rules.yml")
_FIXTURE_DIR = _FIXTURE.parent


def _fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _fake_semgrep_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if argv[1:] == ["--version"]:
        return subprocess.CompletedProcess(argv, 0, stdout="1.45.0\n", stderr="")
    report_path = Path(argv[argv.index("--output") + 1])
    report_path.write_text(_fixture_text(), encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_real_semgrep_sarif_is_normalized_to_a_finding() -> None:
    finding = parse_semgrep_sarif(_fixture_text())[0]

    assert finding.rule == "bernstein-test-eval-use"
    assert finding.path == "vulnerable.py"
    assert finding.severity == "high"
    assert finding.summary == "Bernstein synthetic dangerous eval() use"
    assert finding.extra["snippet_hash"].startswith("sha256:")


def test_two_parses_of_the_same_recorded_run_have_identical_hashes() -> None:
    first = [finding.finding_hash() for finding in parse_semgrep_sarif(_fixture_text())]
    second = [finding.finding_hash() for finding in parse_semgrep_sarif(_fixture_text())]

    assert first == second


def test_cosmetic_line_shift_does_not_change_the_finding_hash() -> None:
    original = json.loads(_fixture_text())
    shifted = copy.deepcopy(original)
    region = shifted["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    region["startLine"] += 7
    region["endLine"] += 7

    original_hashes = [finding.finding_hash() for finding in parse_semgrep_sarif(json.dumps(original))]
    shifted_hashes = [finding.finding_hash() for finding in parse_semgrep_sarif(json.dumps(shifted))]

    assert shifted_hashes == original_hashes


def test_changing_the_snippet_does_change_the_finding_hash() -> None:
    original = json.loads(_fixture_text())
    changed = copy.deepcopy(original)
    changed["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]["snippet"]["text"] = (
        "eval(other_input)"
    )

    original_hash = parse_semgrep_sarif(json.dumps(original))[0].finding_hash()
    changed_hash = parse_semgrep_sarif(json.dumps(changed))[0].finding_hash()

    assert changed_hash != original_hash


def test_absolute_report_path_is_normalized_to_the_scan_target() -> None:
    report = json.loads(_fixture_text())
    artifact = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]
    artifact["uri"] = "/checkout/project/vulnerable.py"

    finding = parse_semgrep_sarif(json.dumps(report), target_root=Path("/checkout/project"))[0]

    assert finding.path == "vulnerable.py"


def test_non_semgrep_sarif_is_rejected() -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["tool"]["driver"]["name"] = "another-scanner"

    with pytest.raises(ValueError, match="must be 'semgrep'"):
        parse_semgrep_sarif(json.dumps(report))


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid Semgrep SARIF JSON"):
        parse_semgrep_sarif("not-json")


def test_adapter_declares_the_deterministic_sast_scanner_contract() -> None:
    adapter = SemgrepAdapter(config_path=_CONFIG)

    assert adapter.name() == "semgrep"
    assert adapter.output_format is OutputFormat.SARIF
    assert adapter.determinism is DeterminismTier.DETERMINISTIC
    assert adapter.category is ScannerCategory.SAST
    assert scanner_determinism(adapter.name()) is ScannerDeterminism.DETERMINISTIC


def test_scanner_registry_resolves_semgrep() -> None:
    assert isinstance(get_scanner("semgrep"), SemgrepAdapter)


def test_invocation_hash_binds_version_and_ruleset() -> None:
    baseline = _invocation_argv_hash("1.45.0", "sha256:rules-a")

    assert _invocation_argv_hash("1.45.0", "sha256:rules-a") == baseline
    assert _invocation_argv_hash("1.45.1", "sha256:rules-a") != baseline
    assert _invocation_argv_hash("1.45.0", "sha256:rules-b") != baseline


def test_ruleset_digest_hashes_a_rule_directory(tmp_path: Path) -> None:
    rule_dir = tmp_path / "rules"
    rule_dir.mkdir()
    (rule_dir / "a.yml").write_text("rules: []\n", encoding="utf-8")

    before = _ruleset_digest(rule_dir)
    (rule_dir / "b.yml").write_text("rules: []\n", encoding="utf-8")
    after = _ruleset_digest(rule_dir)

    assert before != after


def test_scan_refuses_to_run_without_a_pinned_local_ruleset(tmp_path: Path) -> None:
    """Load-bearing: a Semgrep scan without a pinned local ruleset would fall
    back to remote rule resolution, which is not deterministic, so the
    adapter must refuse rather than silently degrade its determinism tier."""
    target = tmp_path / "target"
    target.mkdir()
    adapter = SemgrepAdapter()

    with (
        patch("bernstein.adapters.semgrep.shutil.which", return_value="/usr/local/bin/semgrep"),
        patch("bernstein.adapters.semgrep.subprocess.run") as run,
        pytest.raises(SemgrepError, match="requires a local rule file or directory"),
    ):
        adapter.scan(target, ScanScope(roots=(target,)), tmp_path / "work")
    run.assert_not_called()


def test_scan_runs_semgrep_and_parses_its_sarif_report(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    adapter = SemgrepAdapter(config_path=_CONFIG)

    with (
        patch("bernstein.adapters.semgrep.shutil.which", return_value="/usr/local/bin/semgrep"),
        patch("bernstein.adapters.semgrep.subprocess.run", side_effect=_fake_semgrep_run) as run,
    ):
        result = adapter.scan(target, ScanScope(roots=(target,)), tmp_path / "work")

    expected_hash = parse_semgrep_sarif(_fixture_text())[0].finding_hash()
    assert result.finding_hashes() == [expected_hash]
    scan_argv = run.call_args_list[1].args[0]
    assert scan_argv[1:3] == ["scan", "--metrics=off"]
    assert "--config" in scan_argv
    assert scan_argv[-1] == str(target.resolve())
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tool_version == "1.45.0"
    assert adapter.last_invocation.ruleset_digest == _ruleset_digest(_CONFIG)


def test_target_local_config_is_discovered(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    local_config = target / ".semgrep.yml"
    local_config.write_bytes(_CONFIG.read_bytes())
    adapter = SemgrepAdapter()

    with (
        patch("bernstein.adapters.semgrep.shutil.which", return_value="/usr/local/bin/semgrep"),
        patch("bernstein.adapters.semgrep.subprocess.run", side_effect=_fake_semgrep_run) as run,
    ):
        adapter.scan(target, ScanScope(roots=(target,)), tmp_path / "work")

    scan_argv = run.call_args_list[1].args[0]
    assert scan_argv[scan_argv.index("--config") + 1] == str(local_config.resolve())


def test_stale_report_cannot_be_reused(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "semgrep.sarif").write_text(_fixture_text(), encoding="utf-8")

    def no_report(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="1.45.0\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    adapter = SemgrepAdapter(config_path=_CONFIG)
    with (
        patch("bernstein.adapters.semgrep.shutil.which", return_value="/usr/local/bin/semgrep"),
        patch("bernstein.adapters.semgrep.subprocess.run", side_effect=no_report),
        pytest.raises(SemgrepError, match="without writing its SARIF report"),
    ):
        adapter.scan(tmp_path, ScanScope(), workdir)


def test_scan_reports_missing_semgrep_without_invoking_a_process(tmp_path: Path) -> None:
    adapter = SemgrepAdapter(config_path=_CONFIG)
    with (
        patch("bernstein.adapters.semgrep.shutil.which", return_value=None),
        patch("bernstein.adapters.semgrep.subprocess.run") as run,
        pytest.raises(SemgrepNotInstalledError, match="not found on PATH"),
    ):
        adapter.scan(tmp_path, ScanScope(), tmp_path / "work")
    run.assert_not_called()


def test_scan_rejects_a_real_semgrep_execution_error(tmp_path: Path) -> None:
    def fail_scan(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="1.45.0\n", stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad rule syntax")

    adapter = SemgrepAdapter(config_path=_CONFIG)
    with (
        patch("bernstein.adapters.semgrep.shutil.which", return_value="/usr/local/bin/semgrep"),
        patch("bernstein.adapters.semgrep.subprocess.run", side_effect=fail_scan),
        pytest.raises(SemgrepError, match="code 2: bad rule syntax"),
    ):
        adapter.scan(tmp_path, ScanScope(), tmp_path / "work")


def test_conformance_replays_two_identical_semgrep_runs(tmp_path: Path) -> None:
    transcripts = load_scanner_golden_transcripts(_FIXTURE_DIR)
    assert len(transcripts) == 1

    with (
        patch("bernstein.adapters.semgrep.shutil.which", return_value="/usr/local/bin/semgrep"),
        patch("bernstein.adapters.semgrep.subprocess.run", side_effect=_fake_semgrep_run) as run,
    ):
        result = ScannerConformanceHarness().replay_transcript(transcripts[0], workdir=tmp_path)

    assert result.passed
    assert result.adapter_name == "semgrep"
    assert result.determinism_tier is DeterminismTier.DETERMINISTIC
    assert sum(call.args[0][1] == "scan" for call in run.call_args_list) == 2
