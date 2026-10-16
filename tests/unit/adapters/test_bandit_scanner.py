"""Tests for deterministic normalization of recorded Bandit SARIF."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters._contract import ScannerDeterminism, scanner_determinism
from bernstein.adapters.bandit import (
    BanditAdapter,
    BanditError,
    BanditNotInstalledError,
    _invocation_argv_hash,
    _ruleset_digest,
    parse_bandit_sarif,
)
from bernstein.adapters.scanner import DeterminismTier, OutputFormat, ScannerCategory, ScanScope
from bernstein.adapters.scanner_conformance import (
    ScannerConformanceHarness,
    load_scanner_golden_transcripts,
)
from bernstein.adapters.scanner_registry import get_scanner

#: Recorded with ``bandit --recursive --quiet --exit-zero --format sarif`` from
#: ``bandit[sarif]==1.8.6`` on Python 3.13, run inside ``target/``.
_FIXTURE = Path("tests/fixtures/scanners/bandit/bandit-1.8.6.sarif")
_FIXTURE_DIR = _FIXTURE.parent
_VERSION_OUTPUT = "bandit 1.8.6\n  python version = 3.13.5\n"


def _fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _fake_bandit_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if argv[1:] == ["--version"]:
        return subprocess.CompletedProcess(argv, 0, stdout=_VERSION_OUTPUT, stderr="")
    report_path = Path(argv[argv.index("--output") + 1])
    report_path.write_text(_fixture_text(), encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _patched(run_side_effect: Any = _fake_bandit_run, which: str | None = "/usr/local/bin/bandit") -> Any:
    return (
        patch("bernstein.adapters.bandit.shutil.which", return_value=which),
        patch("bernstein.adapters.bandit.subprocess.run", side_effect=run_side_effect),
    )


def test_real_bandit_sarif_is_normalized_to_findings() -> None:
    import_finding, shell_finding = parse_bandit_sarif(_fixture_text())

    assert (import_finding.rule, import_finding.path, import_finding.severity) == ("B404", "app.py", "low")
    assert (shell_finding.rule, shell_finding.path, shell_finding.severity) == ("B602", "app.py", "high")
    assert shell_finding.summary == "subprocess call with shell=True identified, security issue."
    assert shell_finding.extra["confidence"] == "high"
    assert shell_finding.extra["snippet_hash"].startswith("sha256:")


def test_two_parses_of_the_same_recorded_run_have_identical_hashes() -> None:
    first = [finding.finding_hash() for finding in parse_bandit_sarif(_fixture_text())]
    second = [finding.finding_hash() for finding in parse_bandit_sarif(_fixture_text())]

    assert first == second


def test_cosmetic_line_shift_does_not_change_the_finding_hash() -> None:
    original = json.loads(_fixture_text())
    shifted = copy.deepcopy(original)
    for result in shifted["runs"][0]["results"]:
        region = result["locations"][0]["physicalLocation"]["region"]
        region["startLine"] += 7
        region["endLine"] += 7

    original_hashes = [finding.finding_hash() for finding in parse_bandit_sarif(json.dumps(original))]
    shifted_hashes = [finding.finding_hash() for finding in parse_bandit_sarif(json.dumps(shifted))]

    assert shifted_hashes == original_hashes


def test_changing_the_snippet_does_change_the_finding_hash() -> None:
    original = json.loads(_fixture_text())
    changed = copy.deepcopy(original)
    changed["runs"][0]["results"][1]["locations"][0]["physicalLocation"]["region"]["snippet"]["text"] = (
        "    subprocess.call(other, shell=True)\n"
    )

    original_hash = parse_bandit_sarif(json.dumps(original))[1].finding_hash()
    changed_hash = parse_bandit_sarif(json.dumps(changed))[1].finding_hash()

    assert changed_hash != original_hash


def test_a_file_uri_is_normalized_to_the_scan_target() -> None:
    """Bandit writes ``file://`` URIs when it is handed an absolute target, as the adapter does."""
    report = json.loads(_fixture_text())
    artifact = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]
    artifact["uri"] = "file:///checkout/project/pkg/app%20main.py"

    finding = parse_bandit_sarif(json.dumps(report), target_root=Path("/checkout/project"))[0]

    assert finding.path == "pkg/app main.py"


def test_severity_falls_back_to_the_sarif_level() -> None:
    report = json.loads(_fixture_text())
    del report["runs"][0]["results"][1]["properties"]

    assert parse_bandit_sarif(json.dumps(report))[1].severity == "high"


def test_non_bandit_sarif_is_rejected() -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["tool"]["driver"]["name"] = "semgrep"

    with pytest.raises(ValueError, match="must be 'Bandit'"):
        parse_bandit_sarif(json.dumps(report))


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid Bandit SARIF JSON"):
        parse_bandit_sarif("not-json")


def test_adapter_declares_the_deterministic_sast_scanner_contract() -> None:
    adapter = BanditAdapter()

    assert adapter.name() == "bandit"
    assert adapter.output_format is OutputFormat.SARIF
    assert adapter.determinism is DeterminismTier.DETERMINISTIC
    assert adapter.category is ScannerCategory.SAST
    assert scanner_determinism(adapter.name()) is ScannerDeterminism.DETERMINISTIC


def test_scanner_registry_resolves_bandit() -> None:
    assert isinstance(get_scanner("bandit"), BanditAdapter)


def test_invocation_hash_binds_version_and_ruleset() -> None:
    baseline = _invocation_argv_hash("1.8.6", "sha256:config-a")

    assert _invocation_argv_hash("1.8.6", "sha256:config-a") == baseline
    assert _invocation_argv_hash("1.8.5", "sha256:config-a") != baseline
    assert _invocation_argv_hash("1.8.6", "sha256:config-b") != baseline


def test_ruleset_digest_covers_the_config_file_and_every_ini_file(tmp_path: Path) -> None:
    config = tmp_path / "bandit.yaml"
    config.write_text("skips: []\n", encoding="utf-8")
    ini = tmp_path / ".bandit"
    ini.write_text("[bandit]\nskips = B404\n", encoding="utf-8")

    no_config = _ruleset_digest(None, [])
    with_config = _ruleset_digest(config, [])
    with_both = _ruleset_digest(config, [ini])

    assert len({no_config, with_config, with_both}) == 3
    assert _ruleset_digest(None, []) == no_config


def test_scan_runs_bandit_and_parses_its_sarif_report(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    adapter = BanditAdapter()

    which, run = _patched()
    with which, run as run_mock:
        result = adapter.scan(target, ScanScope(roots=(target,)), tmp_path / "work")

    expected = sorted(finding.finding_hash() for finding in parse_bandit_sarif(_fixture_text()))
    assert result.finding_hashes() == expected
    scan_argv = run_mock.call_args_list[1].args[0]
    assert scan_argv[1:6] == ["--recursive", "--quiet", "--exit-zero", "--format", "sarif"]
    assert "--configfile" not in scan_argv
    assert scan_argv[-1] == str(target.resolve())
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tool_version == "1.8.6"
    assert adapter.last_invocation.ruleset_digest == _ruleset_digest(None, [])


def test_a_configured_config_file_is_passed_and_digested(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    config = tmp_path / "bandit.yaml"
    config.write_text("skips: ['B404']\n", encoding="utf-8")
    adapter = BanditAdapter(config_path=config)

    which, run = _patched()
    with which, run as run_mock:
        adapter.scan(target, ScanScope(roots=(target,)), tmp_path / "work")

    scan_argv = run_mock.call_args_list[1].args[0]
    assert scan_argv[scan_argv.index("--configfile") + 1] == str(config.resolve())
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.ruleset_digest == _ruleset_digest(config.resolve(), [])


def test_an_ini_file_bandit_would_discover_changes_the_recorded_digest(tmp_path: Path) -> None:
    """Bandit reads a ``.bandit`` file it finds in the target tree without being
    asked, so two runs that differ only in one must not record the same digest."""
    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    adapter = BanditAdapter()

    which, run = _patched()
    with which, run:
        adapter.scan(target, ScanScope(), tmp_path / "work")
        assert adapter.last_invocation is not None
        without_ini = adapter.last_invocation.ruleset_digest
        (target / "pkg" / ".bandit").write_text("[bandit]\nskips = B602\n", encoding="utf-8")
        adapter.scan(target, ScanScope(), tmp_path / "work")
        assert adapter.last_invocation is not None
        with_ini = adapter.last_invocation.ruleset_digest

    assert with_ini != without_ini
    assert with_ini == _ruleset_digest(None, [(target / "pkg" / ".bandit").resolve()])


def test_a_missing_config_file_is_refused_before_bandit_runs(tmp_path: Path) -> None:
    adapter = BanditAdapter(config_path=tmp_path / "absent.yaml")

    which, run = _patched()
    with which, run as run_mock, pytest.raises(BanditError, match="config does not exist"):
        adapter.scan(tmp_path, ScanScope(), tmp_path / "work")
    assert all(call.args[0][1:] == ["--version"] for call in run_mock.call_args_list)


def test_stale_report_cannot_be_reused(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "bandit.sarif").write_text(_fixture_text(), encoding="utf-8")

    def no_report(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_VERSION_OUTPUT, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    which, run = _patched(no_report)
    with which, run, pytest.raises(BanditError, match="without writing its SARIF report"):
        BanditAdapter().scan(tmp_path, ScanScope(), workdir)


def test_scan_reports_missing_bandit_without_invoking_a_process(tmp_path: Path) -> None:
    which, run = _patched(which=None)
    with which, run as run_mock, pytest.raises(BanditNotInstalledError, match="not found on PATH"):
        BanditAdapter().scan(tmp_path, ScanScope(), tmp_path / "work")
    run_mock.assert_not_called()


def test_a_bandit_without_the_sarif_extra_is_refused_with_the_remedy(tmp_path: Path) -> None:
    def no_sarif(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_VERSION_OUTPUT, stderr="")
        return subprocess.CompletedProcess(
            argv, 2, stdout="", stderr="bandit: error: argument -f/--format: invalid choice: 'sarif' (choose from csv)"
        )

    which, run = _patched(no_sarif)
    with which, run, pytest.raises(BanditError, match=r"bandit\[sarif\]"):
        BanditAdapter().scan(tmp_path, ScanScope(), tmp_path / "work")


def test_scan_rejects_a_real_bandit_execution_error(tmp_path: Path) -> None:
    def fail_scan(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_VERSION_OUTPUT, stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="Multiple .bandit files found")

    which, run = _patched(fail_scan)
    with which, run, pytest.raises(BanditError, match="code 2: Multiple .bandit files found"):
        BanditAdapter().scan(tmp_path, ScanScope(), tmp_path / "work")


def test_a_target_outside_the_scope_roots_is_refused(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    which, run = _patched()
    with which, run as run_mock, pytest.raises(ValueError, match="outside the allowed ScanScope roots"):
        BanditAdapter().scan(tmp_path, ScanScope(roots=(allowed,)), tmp_path / "work")
    run_mock.assert_not_called()


def test_conformance_replays_two_identical_bandit_runs(tmp_path: Path) -> None:
    transcripts = load_scanner_golden_transcripts(_FIXTURE_DIR)
    assert len(transcripts) == 1

    which, run = _patched()
    with which, run as run_mock:
        result = ScannerConformanceHarness().replay_transcript(transcripts[0], workdir=tmp_path)

    assert result.passed
    assert result.adapter_name == "bandit"
    assert result.determinism_tier is DeterminismTier.DETERMINISTIC
    assert sum(call.args[0][1] == "--recursive" for call in run_mock.call_args_list) == 2
