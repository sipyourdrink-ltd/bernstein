"""Tests for deterministic normalization of recorded Checkov SARIF."""

from __future__ import annotations

import copy
import json
import ntpath
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters._contract import ScannerDeterminism, scanner_determinism
from bernstein.adapters.checkov import (
    CheckovAdapter,
    CheckovError,
    CheckovNotInstalledError,
    _invocation_argv_hash,
    _ruleset_digest,
    parse_checkov_sarif,
)
from bernstein.adapters.scanner import DeterminismTier, OutputFormat, ScannerCategory, ScanScope
from bernstein.adapters.scanner_conformance import (
    ScannerConformanceHarness,
    load_scanner_golden_transcripts,
)
from bernstein.adapters.scanner_registry import get_scanner

#: Recorded with the adapter's own command line (``checkov --directory .
#: --output sarif --skip-download --soft-fail --quiet``) from
#: ``checkov==3.2.469``, run inside ``target/`` with an empty ``HOME``.
_FIXTURE = Path("tests/fixtures/scanners/checkov/checkov-3.2.469.sarif")
_FIXTURE_DIR = _FIXTURE.parent
_TARGET = _FIXTURE_DIR / "target"


def _fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _fake_checkov_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if argv[1:] == ["--version"]:
        return subprocess.CompletedProcess(argv, 0, stdout="3.2.469\n", stderr="")
    report_dir = Path(argv[argv.index("--output-file-path") + 1])
    (report_dir / "results_sarif.sarif").write_text(_fixture_text(), encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _patched(run_side_effect: Any = _fake_checkov_run, which: str | None = "/usr/local/bin/checkov") -> Any:
    return (
        patch("bernstein.adapters.checkov.shutil.which", return_value=which),
        patch("bernstein.adapters.checkov.subprocess.run", side_effect=run_side_effect),
    )


def _scan_call(run_mock: Any) -> Any:
    return next(call for call in run_mock.call_args_list if call.args[0][1:] != ["--version"])


def test_real_checkov_sarif_is_normalized_to_findings() -> None:
    findings = parse_checkov_sarif(_fixture_text())

    assert [finding.rule for finding in findings] == [
        "CKV_AWS_20",
        "CKV2_AWS_62",
        "CKV2_AWS_6",
        "CKV2_AWS_61",
        "CKV_AWS_18",
        "CKV_AWS_145",
        "CKV_AWS_144",
        "CKV_AWS_21",
    ]
    public_read = findings[0]
    assert public_read.path == "main.tf"
    assert public_read.severity == "high"
    assert public_read.summary == "S3 Bucket has an ACL defined which allows public READ access."
    assert public_read.extra["snippet_hash"].startswith("sha256:")


def test_two_parses_of_the_same_recorded_run_have_identical_hashes() -> None:
    first = [finding.finding_hash() for finding in parse_checkov_sarif(_fixture_text())]
    second = [finding.finding_hash() for finding in parse_checkov_sarif(_fixture_text())]

    assert first == second


def test_cosmetic_line_shift_does_not_change_the_finding_hash() -> None:
    original = json.loads(_fixture_text())
    shifted = copy.deepcopy(original)
    for result in shifted["runs"][0]["results"]:
        region = result["locations"][0]["physicalLocation"]["region"]
        region["startLine"] += 7
        region["endLine"] += 7

    original_hashes = [finding.finding_hash() for finding in parse_checkov_sarif(json.dumps(original))]
    shifted_hashes = [finding.finding_hash() for finding in parse_checkov_sarif(json.dumps(shifted))]

    assert shifted_hashes == original_hashes


def test_the_same_check_on_a_different_resource_is_a_different_finding() -> None:
    original = json.loads(_fixture_text())
    other = copy.deepcopy(original)
    snippet = other["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]["snippet"]
    snippet["text"] = snippet["text"].replace('"logs"', '"audit"')

    original_hash = parse_checkov_sarif(json.dumps(original))[0].finding_hash()
    other_hash = parse_checkov_sarif(json.dumps(other))[0].finding_hash()

    assert other_hash != original_hash


def test_an_absolute_report_path_is_normalized_to_the_scan_target() -> None:
    report = json.loads(_fixture_text())
    artifact = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]
    artifact["uri"] = "/checkout/infra/modules/main.tf"

    finding = parse_checkov_sarif(json.dumps(report), target_root=Path("/checkout/infra"))[0]

    assert finding.path == "modules/main.tf"


def test_non_checkov_sarif_is_rejected() -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["tool"]["driver"]["name"] = "Bandit"

    with pytest.raises(ValueError, match="must be 'Checkov'"):
        parse_checkov_sarif(json.dumps(report))


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid Checkov SARIF JSON"):
        parse_checkov_sarif("not-json")


def test_adapter_declares_the_deterministic_iac_scanner_contract() -> None:
    adapter = CheckovAdapter()

    assert adapter.name() == "checkov"
    assert adapter.output_format is OutputFormat.SARIF
    assert adapter.determinism is DeterminismTier.DETERMINISTIC
    assert adapter.category is ScannerCategory.IAC
    assert scanner_determinism(adapter.name()) is ScannerDeterminism.DETERMINISTIC


def test_scanner_registry_resolves_checkov() -> None:
    assert isinstance(get_scanner("checkov"), CheckovAdapter)


def test_invocation_hash_binds_version_and_ruleset() -> None:
    baseline = _invocation_argv_hash("3.2.469", "sha256:config-a")

    assert _invocation_argv_hash("3.2.469", "sha256:config-a") == baseline
    assert _invocation_argv_hash("3.2.470", "sha256:config-a") != baseline
    assert _invocation_argv_hash("3.2.469", "sha256:config-b") != baseline


def test_ruleset_digest_covers_the_config_file_and_target_configs(tmp_path: Path) -> None:
    config = tmp_path / "checkov.yaml"
    config.write_text("skip-check: []\n", encoding="utf-8")
    target_config = tmp_path / ".checkov.yaml"
    target_config.write_text("skip-check: [CKV_AWS_20]\n", encoding="utf-8")

    digests = {
        _ruleset_digest(None, []),
        _ruleset_digest(config, []),
        _ruleset_digest(None, [target_config]),
        _ruleset_digest(config, [target_config]),
    }

    assert len(digests) == 4


def test_scan_runs_checkov_offline_from_inside_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "work"
    adapter = CheckovAdapter()

    which, run = _patched()
    with which, run as run_mock:
        result = adapter.scan(target, ScanScope(roots=(target,)), workdir)

    assert result.finding_hashes() == sorted(f.finding_hash() for f in parse_checkov_sarif(_fixture_text()))
    call = _scan_call(run_mock)
    argv = call.args[0]
    assert argv[1:5] == ["--directory", ".", "--output", "sarif"]
    assert "--skip-download" in argv
    assert "--soft-fail" in argv
    assert "--config-file" not in argv
    # Run from the target, so SARIF paths are relative to it and its own config is the one read.
    assert call.kwargs["cwd"] == target.resolve()
    # An operator's ~/.checkov.yaml must not change a scan.
    home = Path(call.kwargs["env"]["HOME"])
    assert home.parent == workdir.resolve()
    assert home.name.startswith("checkov-home-")
    assert call.kwargs["env"]["USERPROFILE"] == str(home)
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tool_version == "3.2.469"
    assert adapter.last_invocation.ruleset_digest == _ruleset_digest(None, [])


def test_a_configured_config_file_is_passed_and_digested(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    config = tmp_path / "checkov.yaml"
    config.write_text("skip-check: [CKV_AWS_18]\n", encoding="utf-8")
    adapter = CheckovAdapter(config_path=config)

    which, run = _patched()
    with which, run as run_mock:
        adapter.scan(target, ScanScope(), tmp_path / "work")

    argv = _scan_call(run_mock).args[0]
    assert argv[argv.index("--config-file") + 1] == str(config.resolve())
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.ruleset_digest == _ruleset_digest(config.resolve(), [])


def test_a_config_checkov_reads_from_the_target_changes_the_recorded_digest(tmp_path: Path) -> None:
    """Checkov reads ``.checkov.yaml`` from the scanned directory unasked, so two
    runs that differ only in one must not record the same digest."""
    target = tmp_path / "target"
    target.mkdir()
    adapter = CheckovAdapter()

    which, run = _patched()
    with which, run:
        adapter.scan(target, ScanScope(), tmp_path / "work")
        assert adapter.last_invocation is not None
        without = adapter.last_invocation.ruleset_digest
        (target / ".checkov.yaml").write_text("skip-check: [CKV_AWS_20]\n", encoding="utf-8")
        adapter.scan(target, ScanScope(), tmp_path / "work")
        assert adapter.last_invocation is not None
        with_config = adapter.last_invocation.ruleset_digest

    assert with_config != without


def test_a_file_target_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "main.tf"
    target.write_text("", encoding="utf-8")

    which, run = _patched()
    with which, run as run_mock, pytest.raises(CheckovError, match="must be an existing directory"):
        CheckovAdapter().scan(target, ScanScope(), tmp_path / "work")
    run_mock.assert_not_called()


def test_a_missing_config_file_is_refused_before_checkov_runs(tmp_path: Path) -> None:
    which, run = _patched()
    with which, run as run_mock, pytest.raises(CheckovError, match="config does not exist"):
        CheckovAdapter(config_path=tmp_path / "absent.yaml").scan(tmp_path, ScanScope(), tmp_path / "work")
    run_mock.assert_not_called()


def test_stale_report_cannot_be_reused(tmp_path: Path) -> None:
    report_dir = tmp_path / "work" / "checkov-report"
    report_dir.mkdir(parents=True)
    (report_dir / "results_sarif.sarif").write_text(_fixture_text(), encoding="utf-8")

    def no_report(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="3.2.469\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    which, run = _patched(no_report)
    with which, run, pytest.raises(CheckovError, match="without writing its SARIF report"):
        CheckovAdapter().scan(tmp_path, ScanScope(), tmp_path / "work")


def test_scan_reports_missing_checkov_without_invoking_a_process(tmp_path: Path) -> None:
    which, run = _patched(which=None)
    with which, run as run_mock, pytest.raises(CheckovNotInstalledError, match="not found on PATH"):
        CheckovAdapter().scan(tmp_path, ScanScope(), tmp_path / "work")
    run_mock.assert_not_called()


def test_scan_rejects_a_real_checkov_execution_error(tmp_path: Path) -> None:
    def fail_scan(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="3.2.469\n", stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="config file is not valid YAML")

    which, run = _patched(fail_scan)
    with which, run, pytest.raises(CheckovError, match="code 2: config file is not valid YAML"):
        CheckovAdapter().scan(tmp_path, ScanScope(), tmp_path / "work")


def test_a_target_outside_the_scope_roots_is_refused(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    which, run = _patched()
    with which, run as run_mock, pytest.raises(ValueError, match="outside the allowed ScanScope roots"):
        CheckovAdapter().scan(tmp_path, ScanScope(roots=(allowed,)), tmp_path / "work")
    run_mock.assert_not_called()


def test_conformance_replays_two_identical_checkov_runs(tmp_path: Path) -> None:
    transcripts = load_scanner_golden_transcripts(_FIXTURE_DIR)
    assert len(transcripts) == 1

    which, run = _patched()
    with which, run as run_mock:
        result = ScannerConformanceHarness().replay_transcript(transcripts[0], workdir=tmp_path)

    assert result.passed
    assert result.adapter_name == "checkov"
    assert result.determinism_tier is DeterminismTier.DETERMINISTIC
    assert sum(call.args[0][1] == "--directory" for call in run_mock.call_args_list) == 2


@pytest.mark.parametrize("origin", [".checkov.yaml", ".checkov.yml", "constructor", "scope"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("external-checks-dir", "./unrecorded-checks"),
        ("external-checks-git", "null"),
        ("download-external-modules", "true"),
        ("download-external-modules", "false"),
    ],
)
def test_unrecorded_checks_and_downloads_are_refused_before_any_process(
    tmp_path: Path, origin: str, key: str, value: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    config = target / origin if origin.startswith(".") else tmp_path / "caller.yaml"
    config.write_text(f"{key}: {value}\n", encoding="utf-8")
    adapter = CheckovAdapter(config_path=config if origin == "constructor" else None)
    scope = ScanScope(config={"config_path": str(config)} if origin == "scope" else {})

    which, run = _patched()
    with which, run as run_mock, pytest.raises(CheckovError, match=key):
        adapter.scan(target, scope, tmp_path / "work")
    run_mock.assert_not_called()


def test_yaml_merged_external_check_keys_are_refused_before_dispatch(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / ".checkov.yaml").write_text(
        "defaults: &defaults\n  external-checks-dir: ./checks\n<<: *defaults\n", encoding="utf-8"
    )
    which, run = _patched()
    with which, run as run_mock, pytest.raises(CheckovError, match="external-checks-dir"):
        CheckovAdapter().scan(target, ScanScope(), tmp_path / "work")
    run_mock.assert_not_called()


@pytest.mark.parametrize("config", ["skip-check: [unterminated", "[]", "false"])
def test_unreadable_config_structure_is_refused_before_dispatch(tmp_path: Path, config: str) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / ".checkov.yaml").write_text(config, encoding="utf-8")
    which, run = _patched()
    with which, run as run_mock, pytest.raises(CheckovError, match="config"):
        CheckovAdapter().scan(target, ScanScope(), tmp_path / "work")
    run_mock.assert_not_called()


def test_external_module_download_is_explicitly_disabled_in_the_actual_scan_argv(tmp_path: Path) -> None:
    which, run = _patched()
    with which, run as run_mock:
        CheckovAdapter().scan(_TARGET, ScanScope(), tmp_path)
    argv = _scan_call(run_mock).args[0]
    assert "--download-external-modules" in argv
    assert argv[argv.index("--download-external-modules") + 1] == "false"


def test_scan_result_retains_the_invocation_that_produced_its_findings(tmp_path: Path) -> None:
    adapter = CheckovAdapter()
    which, run = _patched()
    with which, run:
        result = adapter.scan(_TARGET, ScanScope(), tmp_path)
    assert adapter.last_invocation is not None
    assert result.invocation_digest == adapter.last_invocation.argv_hash


def test_reused_workdirs_do_not_reintroduce_home_configuration(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    old_home = workdir / "checkov-home"
    old_home.mkdir(parents=True)
    (old_home / ".checkov.yaml").write_text("skip-check: [CKV_AWS_20]\n", encoding="utf-8")
    homes = []

    def inspect_home(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["env"]["HOME"])
        assert not (home / ".checkov.yaml").exists()
        if argv[1:] != ["--version"]:
            homes.append(home)
        return _fake_checkov_run(argv, **kwargs)

    which, run = _patched(inspect_home)
    with which, run:
        for _ in range(2):
            CheckovAdapter().scan(_TARGET, ScanScope(), workdir)

    assert len(set(homes)) == 2
    assert all(home.parent == workdir.resolve() and not home.exists() for home in homes)
    assert (old_home / ".checkov.yaml").is_file()


def test_windows_home_lookup_cannot_select_operator_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "operator"))

    def inspect_windows_home(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        with patch.dict(os.environ, env, clear=True):
            assert ntpath.expanduser("~") == env["HOME"]
        return _fake_checkov_run(argv, **kwargs)

    which, run = _patched(inspect_windows_home)
    with which, run:
        CheckovAdapter().scan(_TARGET, ScanScope(), tmp_path / "work")


@pytest.mark.parametrize(
    ("level", "expected"), [("error", "high"), ("warning", "medium"), ("note", "low"), (None, "informational")]
)
def test_report_severity_follows_the_existing_sarif_level_contract(level: str | None, expected: str) -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["results"][0]["level"] = level
    assert parse_checkov_sarif(json.dumps(report))[0].severity == expected


def test_benign_config_values_and_comments_do_not_trigger_key_refusal(tmp_path: Path) -> None:
    config = tmp_path / "caller.yaml"
    config.write_text("# external-checks-git: ignored\ncheck: [external-checks-dir]\n", encoding="utf-8")
    which, run = _patched()
    with which, run:
        result = CheckovAdapter(config_path=config).scan(_TARGET, ScanScope(), tmp_path / "work")
    assert len(result.findings) == 8
