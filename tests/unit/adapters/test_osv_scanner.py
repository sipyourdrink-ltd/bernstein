"""Tests for the feed-pinned OSV-Scanner adapter and its SARIF normalization."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters._contract import ScannerDeterminism, scanner_determinism
from bernstein.adapters.osv_scanner import (
    OsvScannerAdapter,
    OsvScannerError,
    OsvScannerNotInstalledError,
    _config_digest,
    _db_identity,
    _invocation_argv_hash,
    _resolve_db_dir,
    parse_osv_scanner_sarif,
)
from bernstein.adapters.scanner import DeterminismTier, OutputFormat, ScannerCategory, ScanScope
from bernstein.adapters.scanner_conformance import (
    ScannerConformanceHarness,
    load_scanner_golden_transcripts,
)
from bernstein.adapters.scanner_registry import get_scanner

#: Recorded from ``osv-scanner`` 2.2.3 (``go install .../osv-scanner@v2.2.3``) with the adapter's
#: command line, offline, against a crates.io database. OSV-Scanner writes absolute ``file://``
#: URIs; the recording's host prefix is replaced with ``file:///checkout/project``.
_FIXTURE = Path("tests/fixtures/scanners/osv-scanner/osv-scanner-2.2.3.sarif")
_FIXTURE_DIR = _FIXTURE.parent
_TARGET = _FIXTURE_DIR / "target"
_RECORDED_ROOT = "file:///checkout/project"
_DB_IDENTITY = "sha256:5c4e7405a2af43f1a34811037f086f5bb6d112176d930bbade47d3e49e49e142"
_OTHER_DB_IDENTITY = "sha256:" + "0" * 64
_VERSION_OUTPUT = "osv-scanner version: 2.2.3\nosv-scalibr version: 0.3.4\ncommit: n/a\n"


def _fixture_text(root: Path | None = None) -> str:
    text = _FIXTURE.read_text(encoding="utf-8")
    return text if root is None else text.replace(_RECORDED_ROOT, root.resolve().as_uri())


def _scope(pin: str = _DB_IDENTITY, target: Path = _TARGET) -> ScanScope:
    return ScanScope(roots=(target,), config={"db_pin": pin})


def _fake_osv_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if argv[1:] == ["--version"]:
        return subprocess.CompletedProcess(argv, 0, stdout=_VERSION_OUTPUT, stderr="")
    report_path = Path(argv[argv.index("--output") + 1])
    report_path.write_text(_fixture_text(Path(argv[-1])), encoding="utf-8")
    # OSV-Scanner exits 1 when it found a vulnerability.
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")


def _patched(run_side_effect: Any = _fake_osv_run, identity: str = _DB_IDENTITY) -> Any:
    return (
        patch("bernstein.adapters.osv_scanner.shutil.which", return_value="/usr/local/bin/osv-scanner"),
        patch("bernstein.adapters.osv_scanner._db_identity", return_value=identity),
        patch("bernstein.adapters.osv_scanner.subprocess.run", side_effect=run_side_effect),
    )


def test_real_osv_sarif_is_normalized_to_one_finding_per_package_and_vulnerability() -> None:
    """OSV-Scanner 2.2.3 emits the smallvec result twice; the exact repeat adds nothing."""
    raw = json.loads(_fixture_text())
    assert len(raw["runs"][0]["results"]) == 2

    (finding,) = parse_osv_scanner_sarif(_fixture_text(_TARGET), target_root=_TARGET.resolve())

    assert finding.rule == "CVE-2021-25900"
    assert finding.path == "Cargo.lock"
    assert finding.severity == "critical"
    assert finding.extra == {"package": "smallvec@1.6.0"}
    assert "RUSTSEC-2021-0003" in finding.summary


def test_two_parses_of_the_same_recorded_run_have_identical_hashes() -> None:
    first = [f.finding_hash() for f in parse_osv_scanner_sarif(_fixture_text(_TARGET), target_root=_TARGET.resolve())]
    second = [f.finding_hash() for f in parse_osv_scanner_sarif(_fixture_text(_TARGET), target_root=_TARGET.resolve())]

    assert first == second


def test_the_same_vulnerability_in_another_package_is_a_different_finding() -> None:
    report = json.loads(_fixture_text())
    other = copy.deepcopy(report)
    for result in other["runs"][0]["results"]:
        result["message"]["text"] = result["message"]["text"].replace("smallvec@1.6.0", "smallvec@0.6.13")

    (original,) = parse_osv_scanner_sarif(json.dumps(report))
    (changed,) = parse_osv_scanner_sarif(json.dumps(other))

    assert changed.extra == {"package": "smallvec@0.6.13"}
    assert changed.finding_hash() != original.finding_hash()


@pytest.mark.parametrize(
    ("score", "severity"),
    [("9.8", "critical"), ("7.5", "high"), ("5.3", "medium"), ("2.0", "low"), ("0", "informational")],
)
def test_severity_follows_the_cvss_bands(score: str, severity: str) -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["security-severity"] = score

    assert parse_osv_scanner_sarif(json.dumps(report))[0].severity == severity


def test_a_rule_without_a_score_is_informational() -> None:
    report = json.loads(_fixture_text())
    del report["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["security-severity"]

    assert parse_osv_scanner_sarif(json.dumps(report))[0].severity == "informational"


def test_non_osv_sarif_is_rejected() -> None:
    report = json.loads(_fixture_text())
    report["runs"][0]["tool"]["driver"]["name"] = "Trivy"

    with pytest.raises(ValueError, match="must be 'osv-scanner'"):
        parse_osv_scanner_sarif(json.dumps(report))


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid OSV-Scanner SARIF JSON"):
        parse_osv_scanner_sarif("not-json")


def _synthetic_run(*, with_results: bool = False) -> dict[str, Any]:
    results = [
        {
            "ruleId": "TEST-OSV-0001",
            "message": {"text": "Synthetic parser fixture"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "Cargo.lock"}}}],
        }
    ]
    return {"tool": {"driver": {"name": "osv-scanner"}}, "results": results if with_results else []}


@pytest.mark.parametrize(
    ("prior_runs", "prior_invocations", "with_results"),
    [(0, 0, False), (0, 1, False), (1, 0, False), (0, 0, True)],
)
def test_explicit_failed_invocation_rejects_the_report(
    prior_runs: int, prior_invocations: int, with_results: bool
) -> None:
    failed = _synthetic_run(with_results=with_results)
    failed["invocations"] = [{"executionSuccessful": True}] * prior_invocations + [{"executionSuccessful": False}]
    report = {"version": "2.1.0", "runs": [_synthetic_run()] * prior_runs + [failed]}

    with pytest.raises(ValueError, match="failed execution"):
        parse_osv_scanner_sarif(json.dumps(report))


@pytest.mark.parametrize("with_results", [False, True])
@pytest.mark.parametrize("invocations", [None, [], [{"executionSuccessful": True}]])
def test_absent_or_successful_invocations_preserve_findings(
    with_results: bool, invocations: list[dict[str, bool]] | None
) -> None:
    run = _synthetic_run(with_results=with_results)
    original = parse_osv_scanner_sarif(json.dumps({"version": "2.1.0", "runs": [run]}))
    if invocations is not None:
        run["invocations"] = invocations

    actual = parse_osv_scanner_sarif(json.dumps({"version": "2.1.0", "runs": [run]}))

    assert len(actual) == int(with_results)
    assert [finding.finding_hash() for finding in actual] == [finding.finding_hash() for finding in original]


def test_adapter_declares_the_feed_pinned_sca_scanner_contract() -> None:
    adapter = OsvScannerAdapter()

    assert adapter.name() == "osv-scanner"
    assert adapter.output_format is OutputFormat.SARIF
    assert adapter.determinism is DeterminismTier.FEED_PINNED
    assert adapter.pinned_inputs == ("osv_db",)
    assert adapter.category is ScannerCategory.SCA
    assert scanner_determinism(adapter.name()) is ScannerDeterminism.FEED_PINNED


def test_scanner_registry_resolves_osv_scanner() -> None:
    assert isinstance(get_scanner("osv-scanner"), OsvScannerAdapter)


def test_invocation_hash_binds_version_database_and_config() -> None:
    baseline = _invocation_argv_hash("2.2.3", _DB_IDENTITY, "sha256:cfg-a")

    assert _invocation_argv_hash("2.2.3", _DB_IDENTITY, "sha256:cfg-a") == baseline
    assert _invocation_argv_hash("2.2.4", _DB_IDENTITY, "sha256:cfg-a") != baseline
    assert _invocation_argv_hash("2.2.3", _OTHER_DB_IDENTITY, "sha256:cfg-a") != baseline
    assert _invocation_argv_hash("2.2.3", _DB_IDENTITY, "sha256:cfg-b") != baseline


def test_db_identity_covers_every_ecosystem_archive_and_its_name(tmp_path: Path) -> None:
    crates = tmp_path / "osv-scanner" / "crates.io"
    crates.mkdir(parents=True)
    (crates / "all.zip").write_bytes(b"crates")
    one = _db_identity(tmp_path)

    pypi = tmp_path / "osv-scanner" / "PyPI"
    pypi.mkdir()
    (pypi / "all.zip").write_bytes(b"pypi")
    two = _db_identity(tmp_path)

    renamed = tmp_path / "renamed"
    (renamed / "osv-scanner" / "npm").mkdir(parents=True)
    (renamed / "osv-scanner" / "npm" / "all.zip").write_bytes(b"crates")

    assert len({one, two, _db_identity(renamed)}) == 3


def test_db_identity_rejects_a_missing_database(tmp_path: Path) -> None:
    with pytest.raises(OsvScannerError, match="No OSV database exists"):
        _db_identity(tmp_path)


def test_scan_runs_offline_against_the_pinned_database(tmp_path: Path) -> None:
    db_dir = tmp_path / "db"
    adapter = OsvScannerAdapter(db_dir=db_dir)

    which, identity, run = _patched()
    with which, identity, run as run_mock:
        result = adapter.scan(_TARGET, _scope(), tmp_path / "work")

    expected = sorted(
        f.finding_hash() for f in parse_osv_scanner_sarif(_fixture_text(_TARGET), target_root=_TARGET.resolve())
    )
    assert result.finding_hashes() == expected
    assert result.feed_digest == _DB_IDENTITY
    call = run_mock.call_args_list[1]
    argv = call.args[0]
    assert argv[1:5] == ["scan", "source", "--offline", "--allow-no-lockfiles"]
    assert argv[-1] == str(_TARGET.resolve())
    assert call.kwargs["env"]["OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"] == str(db_dir.resolve())
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tool_version == "2.2.3"
    assert adapter.last_invocation.db_identity == _DB_IDENTITY
    assert adapter.last_invocation.config_digest == _config_digest([], target=_TARGET.resolve())


def test_a_database_that_does_not_match_the_pin_is_refused_before_scanning(tmp_path: Path) -> None:
    which, identity, run = _patched(identity=_OTHER_DB_IDENTITY)
    with which, identity, run as run_mock, pytest.raises(OsvScannerError, match="pin mismatch"):
        OsvScannerAdapter(db_dir=tmp_path / "db").scan(_TARGET, _scope(), tmp_path / "work")
    run_mock.assert_not_called()


def test_a_scan_without_a_pin_is_refused(tmp_path: Path) -> None:
    which, identity, run = _patched()
    with which, identity, run as run_mock, pytest.raises(OsvScannerError, match="require scope.config"):
        OsvScannerAdapter().scan(_TARGET, ScanScope(roots=(_TARGET,)), tmp_path / "work")
    run_mock.assert_not_called()


def test_an_ignore_list_osv_scanner_would_read_changes_the_recorded_invocation(tmp_path: Path) -> None:
    """OSV-Scanner reads ``osv-scanner.toml`` from the scanned tree and drops what it lists."""
    target = tmp_path / "target"
    (target / "crate").mkdir(parents=True)
    adapter = OsvScannerAdapter(db_dir=tmp_path / "db")

    which, identity, run = _patched()
    with which, identity, run:
        adapter.scan(target, _scope(target=target), tmp_path / "work")
        assert adapter.last_invocation is not None
        without = adapter.last_invocation.argv_hash
        (target / "crate" / "osv-scanner.toml").write_text(
            '[[IgnoredVulns]]\nid = "CVE-2021-25900"\n', encoding="utf-8"
        )
        adapter.scan(target, _scope(target=target), tmp_path / "work")
        assert adapter.last_invocation is not None
        with_config = adapter.last_invocation.argv_hash

    assert with_config != without


def test_config_location_changes_invocation_identity(tmp_path: Path) -> None:
    target = tmp_path / "target"
    for directory in ("a", "b"):
        (target / directory).mkdir(parents=True)
    config = target / "a" / "osv-scanner.toml"
    config.write_text('[[IgnoredVulns]]\nid = "TEST-OSV-0001"\n', encoding="utf-8")
    adapter = OsvScannerAdapter(db_dir=tmp_path / "db")
    which, identity, run = _patched()
    with which, identity, run:
        adapter.scan(target, _scope(target=target), tmp_path / "work")
        first = adapter.last_invocation
        config.rename(target / "b" / config.name)
        adapter.scan(target, _scope(target=target), tmp_path / "work")
        second = adapter.last_invocation
    assert first is not None and second is not None
    assert first.config_digest != second.config_digest
    assert first.argv_hash != second.argv_hash


def test_config_identity_is_stable_across_checkout_roots(tmp_path: Path) -> None:
    invocations = []
    which, identity, run = _patched()
    with which, identity, run:
        for checkout, directories in (("first", ("a", "b")), ("second", ("b", "a"))):
            target = tmp_path / checkout
            for directory in directories:
                (target / directory).mkdir(parents=True)
                (target / directory / "osv-scanner.toml").write_text(
                    f'[[IgnoredVulns]]\nid = "TEST-{directory}"\n', encoding="utf-8"
                )
            adapter = OsvScannerAdapter(db_dir=tmp_path / "db")
            adapter.scan(target, _scope(target=target), tmp_path / "work")
            invocations.append(adapter.last_invocation)
    assert invocations[0] is not None
    assert invocations[0] == invocations[1]


@pytest.mark.parametrize(
    ("table", "field", "selector"),
    [
        ("IgnoredVulns", "ignoreUntil", 'id = "TEST-OSV-0001"'),
        ("PackageOverrides", "effectiveUntil", 'name = "synthetic"\nignore = true'),
        ("ignoredvulns", "IGNOREUNTIL", 'id = "TEST-OSV-0001"'),
        ("packageoverrides", "EFFECTIVEUNTIL", 'name = "synthetic"\nignore = true'),
    ],
)
@pytest.mark.parametrize("timestamp", ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_temporal_config_is_rejected_before_any_vendor_process(
    tmp_path: Path, table: str, field: str, selector: str, timestamp: str
) -> None:
    target = tmp_path / "target"
    (target / "package").mkdir(parents=True)
    (target / "package" / "osv-scanner.toml").write_text(
        f"[[{table}]]\n{selector}\n{field} = {timestamp}\n", encoding="utf-8"
    )
    adapter = OsvScannerAdapter(db_dir=tmp_path / "db")
    which, identity, run = _patched()
    with which, identity, run as run_mock:
        with pytest.raises(OsvScannerError, match="time-dependent"):
            adapter.scan(target, _scope(target=target), tmp_path / "work")
        run_mock.assert_not_called()
    assert adapter.last_invocation is None
    assert not (tmp_path / "work").exists()


def test_timeless_config_accepts_temporal_words_in_comments_and_strings(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "osv-scanner.toml").write_text(
        "# ignoreUntil = 2099-01-01T00:00:00Z\n"
        '[[IgnoredVulns]]\nid = "TEST-OSV-0001"\nreason = "ignoreUntil is deliberately absent"\n'
        '[[PackageOverrides]]\nname = "synthetic"\nignore = true\n'
        'reason = "effectiveUntil is deliberately absent"\n',
        encoding="utf-8",
    )
    adapter = OsvScannerAdapter(db_dir=tmp_path / "db")
    which, identity, run = _patched()
    with which, identity, run as run_mock:
        result = adapter.scan(target, _scope(target=target), tmp_path / "work")
    assert run_mock.call_count == 2  # Mocked version and scan, no actual vendor process.
    assert len(result.findings) == 1
    assert adapter.last_invocation is not None


@pytest.mark.parametrize("content", [b"[[IgnoredVulns]", b"\xff"])
def test_unparseable_config_is_refused_before_any_vendor_process(tmp_path: Path, content: bytes) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "osv-scanner.toml").write_bytes(content)
    which, identity, run = _patched()
    with which, identity, run as run_mock:
        with pytest.raises(OsvScannerError, match="Could not validate"):
            OsvScannerAdapter(db_dir=tmp_path / "db").scan(target, _scope(target=target), tmp_path / "work")
        run_mock.assert_not_called()


def test_exit_codes_other_than_zero_and_one_are_a_failed_run(tmp_path: Path) -> None:
    def no_db(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_VERSION_OUTPUT, stderr="")
        return subprocess.CompletedProcess(
            argv, 127, stdout="", stderr="no offline version of the OSV database is available"
        )

    which, identity, run = _patched(no_db)
    with which, identity, run, pytest.raises(OsvScannerError, match="code 127: no offline version"):
        OsvScannerAdapter(db_dir=tmp_path / "db").scan(_TARGET, _scope(), tmp_path / "work")


def test_stale_report_cannot_be_reused(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "osv-scanner.sarif").write_text(_fixture_text(), encoding="utf-8")

    def no_report(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_VERSION_OUTPUT, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    which, identity, run = _patched(no_report)
    with which, identity, run, pytest.raises(OsvScannerError, match="without writing its SARIF report"):
        OsvScannerAdapter(db_dir=tmp_path / "db").scan(_TARGET, _scope(), workdir)


def test_scan_reports_missing_osv_scanner_without_invoking_a_process(tmp_path: Path) -> None:
    with (
        patch("bernstein.adapters.osv_scanner.shutil.which", return_value=None),
        patch("bernstein.adapters.osv_scanner.subprocess.run") as run_mock,
        pytest.raises(OsvScannerNotInstalledError, match="not found on PATH"),
    ):
        OsvScannerAdapter().scan(_TARGET, _scope(), tmp_path / "work")
    run_mock.assert_not_called()


def test_default_database_root_matches_go_user_cache_dir_on_macos() -> None:
    with (
        patch("bernstein.adapters.osv_scanner.sys.platform", "darwin"),
        patch.dict("os.environ", {"HOME": "/Users/example"}),
    ):
        assert _resolve_db_dir(None) == Path("/Users/example/Library/Caches")


def test_conformance_replays_two_identical_osv_scanner_runs(tmp_path: Path) -> None:
    transcripts = load_scanner_golden_transcripts(_FIXTURE_DIR)
    assert len(transcripts) == 1

    which, identity, run = _patched()
    with which, identity, run as run_mock:
        result = ScannerConformanceHarness().replay_transcript(transcripts[0], workdir=tmp_path)

    assert result.passed
    assert result.adapter_name == "osv-scanner"
    assert result.determinism_tier is DeterminismTier.FEED_PINNED
    assert sum(call.args[0][1] == "scan" for call in run_mock.call_args_list) == 2


@pytest.mark.parametrize(
    ("uri", "root"),
    [
        ("file:///C:/checkout/project/Cargo.lock", Path("C:/checkout/project")),
        ("file:///D:/another/project/Cargo.lock", Path("D:/another/project")),
        ("file:///checkout/project/Cargo.lock", Path("/checkout/project")),
        ("FILE:///checkout/project/Cargo.lock", Path("/checkout/project")),
        ("FILE:///C:/checkout/project/Cargo.lock", Path("C:/checkout/project")),
        ("file://server/share/project/Cargo.lock", Path("//server/share/project")),
    ],
)
def test_recorded_findings_keep_the_same_identity_across_checkout_hosts(uri: str, root: Path) -> None:
    report = json.loads(_fixture_text())
    for result in report["runs"][0]["results"]:
        result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = uri

    (finding,) = parse_osv_scanner_sarif(json.dumps(report), target_root=root)

    assert finding.path == "Cargo.lock"
    assert finding.finding_hash() == "e7343cefea97f441b9606539740fb6bd4a6a33741fc0500ce50d970cee64647e"


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("file:///C:/checkout/other/Cargo.lock", "C:/checkout/other/Cargo.lock"),
        ("file://other/share/project/Cargo.lock", "//other/share/project/Cargo.lock"),
        ("file:///C:/checkout/project/a%2520b/Cargo.lock", "a%20b/Cargo.lock"),
    ],
)
def test_reported_paths_preserve_outside_roots_and_decode_file_uris_once(uri: str, expected: str) -> None:
    report = json.loads(_fixture_text())
    for result in report["runs"][0]["results"]:
        result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = uri

    (finding,) = parse_osv_scanner_sarif(json.dumps(report), target_root=Path("C:/checkout/project"))

    assert finding.path == expected
