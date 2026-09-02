"""Tests for transcript-anchored normalization of recorded Nmap XML."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters._contract import ScannerDeterminism, scanner_determinism
from bernstein.adapters.nmap import (
    NmapAdapter,
    NmapError,
    NmapNotInstalledError,
    _invocation_argv_hash,
    normalize_nmap_xml,
)
from bernstein.adapters.scanner import DeterminismTier, OutputFormat, ScannerCategory, ScanScope
from bernstein.adapters.scanner_conformance import ScannerConformanceHarness, load_scanner_golden_transcripts
from bernstein.adapters.scanner_registry import get_scanner

_FIXTURE_DIR = Path("tests/fixtures/scanners/nmap")
_FIXTURE_A = _FIXTURE_DIR / "nmap-localhost-a.xml"
_FIXTURE_B = _FIXTURE_DIR / "nmap-localhost-b.xml"
_TRANSCRIPT = (
    '{"ports":[{"host":"127.0.0.1","port":8765,"protocol":"tcp","service":'
    '{"cpes":["cpe:/a:python:simplehttpserver:0.6"],"extra_info":"Python 3.9.6",'
    '"name":"http","product":"SimpleHTTPServer","version":"0.6"},"state":"open"}],'
    '"tool":{"name":"nmap","version":"7.991"}}'
)


def _fixture(path: Path = _FIXTURE_A) -> str:
    return path.read_text(encoding="utf-8")


def _scope() -> ScanScope:
    return ScanScope(config={"ports": "8765"})


def _fake_nmap_run(report_path: Path = _FIXTURE_A):
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Nmap version 7.991 ( https://nmap.org )\n", stderr="")
        output = Path(argv[argv.index("-oX") + 1])
        output.write_text(_fixture(report_path), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return run


def test_timestamp_changes_are_normalized_not_assumed_away() -> None:
    raw_a = _fixture(_FIXTURE_A)
    raw_b = _fixture(_FIXTURE_B)
    root_a = ET.fromstring(raw_a)
    root_b = ET.fromstring(raw_b)
    normalized_a = normalize_nmap_xml(raw_a)
    normalized_b = normalize_nmap_xml(raw_b)

    assert raw_a != raw_b
    assert root_a.get("start") != root_b.get("start")
    assert root_a.find("./runstats/finished").get("time") != root_b.find("./runstats/finished").get("time")  # type: ignore[union-attr]
    assert normalized_a.transcript == normalized_b.transcript == _TRANSCRIPT
    assert normalized_a.facts == normalized_b.facts
    assert [finding.finding_hash() for finding in normalized_a.findings] == [
        finding.finding_hash() for finding in normalized_b.findings
    ]


def test_real_nmap_xml_is_normalized_to_a_port_finding() -> None:
    normalized = normalize_nmap_xml(_fixture())

    assert len(normalized.facts) == 1
    fact = normalized.facts[0]
    assert (fact.host, fact.protocol, fact.port, fact.state, fact.service_name) == (
        "127.0.0.1",
        "tcp",
        8765,
        "open",
        "http",
    )
    assert normalized.findings[0].path == "127.0.0.1"
    assert normalized.findings[0].rule == "nmap-port:tcp:8765"
    assert normalized.findings[0].summary == "open http"


def test_banner_change_is_recorded_without_changing_finding_identity() -> None:
    original = normalize_nmap_xml(_fixture())
    root = ET.fromstring(_fixture())
    service = root.find("./host/ports/port/service")
    assert service is not None
    service.set("version", "9.9")
    service.set("extrainfo", "different banner")

    changed = normalize_nmap_xml(ET.tostring(root, encoding="unicode"))

    assert original.facts == changed.facts
    assert [finding.finding_hash() for finding in original.findings] == [
        finding.finding_hash() for finding in changed.findings
    ]
    assert original.transcript != changed.transcript
    assert "different banner" in changed.transcript


def test_non_nmap_xml_is_rejected() -> None:
    with pytest.raises(ValueError, match="produced by scanner 'nmap'"):
        normalize_nmap_xml('<nmaprun scanner="another-tool" version="1"><host /></nmaprun>')


def test_invalid_xml_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid Nmap XML"):
        normalize_nmap_xml("not-xml")


def test_adapter_declares_the_transcript_anchored_recon_contract() -> None:
    adapter = NmapAdapter()

    assert adapter.name() == "nmap"
    assert adapter.output_format is OutputFormat.XML
    assert adapter.determinism is DeterminismTier.TRANSCRIPT_ANCHORED
    assert adapter.pinned_inputs == ()
    assert adapter.category is ScannerCategory.RECON
    assert scanner_determinism(adapter.name()) is ScannerDeterminism.TRANSCRIPT_ANCHORED


def test_scanner_registry_resolves_nmap() -> None:
    assert isinstance(get_scanner("nmap"), NmapAdapter)


def test_invocation_hash_binds_version_target_and_ports() -> None:
    baseline = _invocation_argv_hash("7.991", "127.0.0.1", "8765")

    assert _invocation_argv_hash("7.991", "127.0.0.1", "8765") == baseline
    assert _invocation_argv_hash("7.992", "127.0.0.1", "8765") != baseline
    assert _invocation_argv_hash("7.991", "127.0.0.2", "8765") != baseline
    assert _invocation_argv_hash("7.991", "127.0.0.1", "8766") != baseline


def test_scan_runs_nmap_and_returns_the_canonical_transcript(tmp_path: Path) -> None:
    adapter = NmapAdapter()
    with (
        patch("bernstein.adapters.nmap.shutil.which", return_value="/usr/local/bin/nmap"),
        patch("bernstein.adapters.nmap.subprocess.run", side_effect=_fake_nmap_run()) as run,
    ):
        result = adapter.scan(Path("127.0.0.1"), _scope(), tmp_path / "work")

    assert result.transcript == _TRANSCRIPT
    assert result.finding_hashes() == ["81a37523395db5b88e9ac02117fd9ce5fb4175509d8363ad0db0b5f475ac6de9"]
    scan_argv = run.call_args_list[1].args[0]
    assert scan_argv[1:8] == ["-sT", "-sV", "-Pn", "--no-stylesheet", "-p", "8765", "-oX"]
    assert scan_argv[-1] == "127.0.0.1"
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tool_version == "7.991"
    assert not (tmp_path / "work" / "nmap.xml").exists()


def test_scan_reports_missing_nmap_without_invoking_a_process(tmp_path: Path) -> None:
    with (
        patch("bernstein.adapters.nmap.shutil.which", return_value=None),
        patch("bernstein.adapters.nmap.subprocess.run") as run,
        pytest.raises(NmapNotInstalledError, match="not found on PATH"),
    ):
        NmapAdapter().scan(Path("127.0.0.1"), _scope(), tmp_path / "work")
    run.assert_not_called()


def test_stale_report_cannot_be_reused(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "nmap.xml").write_text(_fixture(), encoding="utf-8")

    def no_report(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Nmap version 7.991\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with (
        patch("bernstein.adapters.nmap.shutil.which", return_value="/usr/local/bin/nmap"),
        patch("bernstein.adapters.nmap.subprocess.run", side_effect=no_report),
        pytest.raises(NmapError, match="without writing its XML report"),
    ):
        NmapAdapter().scan(Path("127.0.0.1"), _scope(), workdir)


def test_scan_rejects_a_real_nmap_execution_error(tmp_path: Path) -> None:
    def fail_scan(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Nmap version 7.991\n", stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="host refused")

    with (
        patch("bernstein.adapters.nmap.shutil.which", return_value="/usr/local/bin/nmap"),
        patch("bernstein.adapters.nmap.subprocess.run", side_effect=fail_scan),
        pytest.raises(NmapError, match="code 2: host refused"),
    ):
        NmapAdapter().scan(Path("127.0.0.1"), _scope(), tmp_path / "work")


@pytest.mark.parametrize("ports", ["0", "65536", "90-80", "80,not-a-port"])
def test_invalid_port_scopes_fail_before_nmap_lookup(tmp_path: Path, ports: str) -> None:
    with (
        patch("bernstein.adapters.nmap.shutil.which") as which,
        pytest.raises(ValueError, match="ports must be"),
    ):
        NmapAdapter().scan(Path("127.0.0.1"), ScanScope(config={"ports": ports}), tmp_path / "work")
    which.assert_not_called()


def test_conformance_replays_both_recordings_and_checks_the_transcript(tmp_path: Path) -> None:
    transcripts = load_scanner_golden_transcripts(_FIXTURE_DIR)
    assert len(transcripts) == 1
    reports = iter((_FIXTURE_A, _FIXTURE_B))

    def replay_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Nmap version 7.991\n", stderr="")
        output = Path(argv[argv.index("-oX") + 1])
        output.write_text(_fixture(next(reports)), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with (
        patch("bernstein.adapters.nmap.shutil.which", return_value="/usr/local/bin/nmap"),
        patch("bernstein.adapters.nmap.subprocess.run", side_effect=replay_run),
    ):
        result = ScannerConformanceHarness().replay_transcript(transcripts[0], workdir=tmp_path)

    assert result.passed
    assert result.adapter_name == "nmap"
    assert result.determinism_tier is DeterminismTier.TRANSCRIPT_ANCHORED
    assert len(result.step_results) == 2


def test_conformance_fails_when_the_expected_transcript_changes(tmp_path: Path) -> None:
    transcript = load_scanner_golden_transcripts(_FIXTURE_DIR)[0]
    transcript.steps[0].expected_transcript = "not the canonical transcript"

    with (
        patch("bernstein.adapters.nmap.shutil.which", return_value="/usr/local/bin/nmap"),
        patch("bernstein.adapters.nmap.subprocess.run", side_effect=_fake_nmap_run()),
    ):
        result = ScannerConformanceHarness().replay_transcript(transcript, workdir=tmp_path)

    assert not result.passed
    assert "recorded transcript differs" in result.step_results[0].message
