"""Tests for SBOM generation, CycloneDX output, and vulnerability gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from bernstein.core.sbom import (
    SBOMComponent,
    SBOMDocument,
    SBOMFormat,
    SBOMGateError,
    SBOMGenerator,
    SBOMScanResult,
    SBOMVulnerabilityGate,
    SBOMVulnFinding,
    SBOMVulnSeverity,
    _parse_grype_output,
    _parse_osv_scanner_output,
    _purl_for_python_package,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_component(name: str = "requests", version: str = "2.28.0") -> SBOMComponent:
    return SBOMComponent(
        name=name,
        version=version,
        purl=_purl_for_python_package(name, version),
        licenses=["Apache-2.0"],
    )


def _make_sbom(components: list[SBOMComponent] | None = None) -> SBOMDocument:
    return SBOMDocument(
        serial_number="urn:uuid:12345678-1234-5678-1234-567812345678",
        generated_at=1_700_000_000.0,
        components=components or [_make_component()],
        sbom_format=SBOMFormat.CYCLONEDX_JSON,
        source="pip",
    )


# ---------------------------------------------------------------------------
# PURL generation
# ---------------------------------------------------------------------------


def test_purl_for_python_package_basic() -> None:
    assert _purl_for_python_package("requests", "2.28.0") == "pkg:pypi/requests@2.28.0"


def test_purl_normalises_underscores() -> None:
    assert _purl_for_python_package("my_package", "1.0.0") == "pkg:pypi/my-package@1.0.0"


def test_purl_lowercases_name() -> None:
    assert _purl_for_python_package("Jinja2", "3.1.2") == "pkg:pypi/jinja2@3.1.2"


# ---------------------------------------------------------------------------
# CycloneDX serialisation
# ---------------------------------------------------------------------------


def test_cyclonedx_document_structure() -> None:
    sbom = _make_sbom()
    doc = sbom.to_cyclonedx_dict()

    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == "1.5"
    assert doc["serialNumber"] == "urn:uuid:12345678-1234-5678-1234-567812345678"
    assert doc["version"] == 1
    assert "timestamp" in doc["metadata"]
    assert len(doc["components"]) == 1


def test_cyclonedx_component_fields() -> None:
    comp = _make_component("flask", "3.0.0")
    result = comp.to_cyclonedx_dict()

    assert result["name"] == "flask"
    assert result["version"] == "3.0.0"
    assert result["purl"] == "pkg:pypi/flask@3.0.0"
    assert result["type"] == "library"
    assert result["licenses"] == [{"license": {"name": "Apache-2.0"}}]


def test_cyclonedx_to_json_is_valid_json() -> None:
    sbom = _make_sbom()
    raw = sbom.to_json()

    doc = json.loads(raw)
    assert doc["bomFormat"] == "CycloneDX"
    assert len(doc["components"]) == 1


def test_cyclonedx_multiple_components() -> None:
    comps = [_make_component("requests", "2.28.0"), _make_component("flask", "3.0.0")]
    sbom = _make_sbom(comps)
    doc = sbom.to_cyclonedx_dict()

    assert len(doc["components"]) == 2
    names = {c["name"] for c in doc["components"]}
    assert names == {"requests", "flask"}


# ---------------------------------------------------------------------------
# SPDX serialisation
# ---------------------------------------------------------------------------


def test_spdx_document_structure() -> None:
    sbom = SBOMDocument(
        serial_number="urn:uuid:aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb",
        generated_at=1_700_000_000.0,
        components=[_make_component()],
        sbom_format=SBOMFormat.SPDX_JSON,
    )
    doc = sbom.to_spdx_dict()

    assert doc["spdxVersion"] == "SPDX-2.3"
    assert doc["SPDXID"] == "SPDXRef-DOCUMENT"
    assert len(doc["packages"]) == 1
    pkg = doc["packages"][0]
    assert pkg["name"] == "requests"
    assert pkg["versionInfo"] == "2.28.0"
    assert pkg["externalRefs"][0]["referenceType"] == "purl"


def test_spdx_to_json_when_format_set() -> None:
    sbom = SBOMDocument(
        serial_number="urn:uuid:aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb",
        generated_at=1_700_000_000.0,
        components=[_make_component()],
        sbom_format=SBOMFormat.SPDX_JSON,
    )
    raw = sbom.to_json()
    doc = json.loads(raw)
    assert doc["spdxVersion"] == "SPDX-2.3"


# ---------------------------------------------------------------------------
# SBOMGenerator
# ---------------------------------------------------------------------------


def test_sbom_generator_generate_returns_document(tmp_path: Path) -> None:
    gen = SBOMGenerator(tmp_path)
    sbom = gen.generate(source="pip")

    # Must have found at least one installed package (the test env has packages)
    assert len(sbom.components) > 0
    assert sbom.source == "pip"
    assert sbom.serial_number.startswith("urn:uuid:")


def test_sbom_generator_save_writes_file(tmp_path: Path) -> None:
    gen = SBOMGenerator(tmp_path)
    sbom = _make_sbom()

    path = gen.save(sbom, filename="test-sbom.json")

    assert path.exists()
    doc = json.loads(path.read_text())
    assert doc["bomFormat"] == "CycloneDX"


def test_sbom_generator_save_creates_artifact_dir(tmp_path: Path) -> None:
    gen = SBOMGenerator(tmp_path)
    sbom = _make_sbom()

    gen.save(sbom)

    artifact_dir = tmp_path / ".sdd" / "artifacts" / "sbom"
    assert artifact_dir.is_dir()


def test_sbom_generator_scan_returns_result_when_no_scanners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no scanners are available, scan returns an empty result with a warning."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    gen = SBOMGenerator(tmp_path)
    sbom = _make_sbom()

    result = gen.scan(sbom)

    assert result.scanner == "none"
    assert len(result.errors) > 0
    assert len(result.findings) == 0


# ---------------------------------------------------------------------------
# osv-scanner output parsing
# ---------------------------------------------------------------------------

_OSV_SCANNER_OUTPUT_CLEAN = json.dumps({"results": []})

_OSV_SCANNER_OUTPUT_WITH_VULN = json.dumps(
    {
        "results": [
            {
                "packages": [
                    {
                        "package": {"name": "jinja2", "version": "2.11.3"},
                        "vulnerabilities": [
                            {
                                "id": "GHSA-h5c8-rqwp-cp95",
                                "summary": "Jinja2 is vulnerable to sandbox bypass",
                                "severity": "high",
                                "affected": [
                                    {
                                        "ranges": [
                                            {
                                                "type": "ECOSYSTEM",
                                                "events": [{"fixed": "3.0.0"}],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ]
    }
)


def test_parse_osv_scanner_clean_output() -> None:
    findings = _parse_osv_scanner_output(_OSV_SCANNER_OUTPUT_CLEAN, "serial-1")
    assert findings == []


def test_parse_osv_scanner_finds_vulnerability() -> None:
    findings = _parse_osv_scanner_output(_OSV_SCANNER_OUTPUT_WITH_VULN, "serial-1")

    assert len(findings) == 1
    f = findings[0]
    assert f.component_name == "jinja2"
    assert f.component_version == "2.11.3"
    assert f.vuln_id == "GHSA-h5c8-rqwp-cp95"
    assert f.fix_version == "3.0.0"
    assert f.scanner == "osv-scanner"


def test_parse_osv_scanner_empty_string_returns_empty() -> None:
    assert _parse_osv_scanner_output("", "serial-1") == []


def test_parse_osv_scanner_invalid_json_returns_empty() -> None:
    assert _parse_osv_scanner_output("not-json", "serial-1") == []


# ---------------------------------------------------------------------------
# osv-scanner severity, as the scanner and the OSV schema actually emit it
# ---------------------------------------------------------------------------

_CRITICAL_VECTOR = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"  # base score 9.8


def _osv_output(
    vulnerabilities: list[dict[str, object]],
    groups: list[dict[str, object]] | None = None,
) -> str:
    """Wrap OSV records in the ``osv-scanner --format=json`` envelope."""
    package: dict[str, object] = {
        "package": {"name": "pkg", "version": "1.0.0", "ecosystem": "PyPI"},
        "vulnerabilities": vulnerabilities,
    }
    if groups is not None:
        package["groups"] = groups
    return json.dumps({"results": [{"source": {"path": "sbom.json", "type": "sbom"}, "packages": [package]}]})


def _ghsa(vuln_id: str, rating: str, vector: str = _CRITICAL_VECTOR) -> dict[str, object]:
    """A GitHub advisory as OSV publishes it: CVSS vectors in ``severity``, its rating as a string."""
    return {
        "id": vuln_id,
        "severity": [{"type": "CVSS_V3", "score": vector}],
        "database_specific": {"severity": rating, "github_reviewed": True},
    }


def _scan_result(stdout: str) -> SBOMScanResult:
    return SBOMScanResult(
        sbom_serial="serial-1",
        scanned_at=0.0,
        scanner="osv-scanner",
        findings=_parse_osv_scanner_output(stdout, "serial-1"),
    )


class TestOsvScannerSeverity:
    """A critical advisory reported by osv-scanner must reach the gate as critical."""

    def test_a_critical_github_advisory_blocks_the_default_gate(self) -> None:
        result = _scan_result(_osv_output([_ghsa("GHSA-aaaa-bbbb-cccc", "CRITICAL")]))

        assert [f.severity for f in result.findings] == [SBOMVulnSeverity.CRITICAL]
        assert not SBOMVulnerabilityGate().passes(result)

    @pytest.mark.parametrize(
        ("rating", "expected"),
        [
            ("CRITICAL", SBOMVulnSeverity.CRITICAL),
            ("HIGH", SBOMVulnSeverity.HIGH),
            ("MODERATE", SBOMVulnSeverity.MEDIUM),
            ("LOW", SBOMVulnSeverity.LOW),
        ],
    )
    def test_github_advisory_ratings(self, rating: str, expected: SBOMVulnSeverity) -> None:
        findings = _parse_osv_scanner_output(_osv_output([_ghsa("GHSA-aaaa-bbbb-cccc", rating)]), "serial-1")

        assert [f.severity for f in findings] == [expected]

    def test_the_scanner_group_score_rates_a_record_without_an_advisory_rating(self) -> None:
        pysec: dict[str, object] = {
            "id": "PYSEC-2099-1",
            "aliases": ["CVE-2099-1"],
            "severity": [{"type": "CVSS_V3", "score": _CRITICAL_VECTOR}],
        }
        stdout = _osv_output([pysec], groups=[{"ids": ["CVE-2099-1", "PYSEC-2099-1"], "max_severity": "9.8"}])
        result = _scan_result(stdout)

        assert [f.severity for f in result.findings] == [SBOMVulnSeverity.CRITICAL]
        assert not SBOMVulnerabilityGate().passes(result)

    def test_the_higher_of_group_score_and_advisory_rating_wins(self) -> None:
        stdout = _osv_output(
            [_ghsa("GHSA-low-rated", "LOW"), _ghsa("GHSA-crit-rated", "CRITICAL")],
            groups=[
                {"ids": ["GHSA-low-rated"], "max_severity": "9.1"},
                {"ids": ["GHSA-crit-rated"], "max_severity": "7.5"},
            ],
        )
        findings = _parse_osv_scanner_output(stdout, "serial-1")

        assert {f.vuln_id: f.severity for f in findings} == {
            "GHSA-low-rated": SBOMVulnSeverity.CRITICAL,
            "GHSA-crit-rated": SBOMVulnSeverity.CRITICAL,
        }

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            ("10.0", SBOMVulnSeverity.CRITICAL),
            ("9.0", SBOMVulnSeverity.CRITICAL),
            ("8.9", SBOMVulnSeverity.HIGH),
            ("7.0", SBOMVulnSeverity.HIGH),
            ("6.9", SBOMVulnSeverity.MEDIUM),
            ("4.0", SBOMVulnSeverity.MEDIUM),
            ("3.9", SBOMVulnSeverity.LOW),
            ("0.1", SBOMVulnSeverity.LOW),
            ("0.0", SBOMVulnSeverity.NONE),
            ("", SBOMVulnSeverity.UNKNOWN),
            ("n/a", SBOMVulnSeverity.UNKNOWN),
            ("nan", SBOMVulnSeverity.UNKNOWN),
            ("11.0", SBOMVulnSeverity.UNKNOWN),
        ],
    )
    def test_group_score_uses_the_cvss_rating_scale(self, score: str, expected: SBOMVulnSeverity) -> None:
        stdout = _osv_output([{"id": "OSV-1"}], groups=[{"ids": ["OSV-1"], "max_severity": score}])

        assert [f.severity for f in _parse_osv_scanner_output(stdout, "serial-1")] == [expected]

    def test_a_record_with_no_rating_anywhere_stays_unknown(self) -> None:
        stdout = _osv_output(
            [{"id": "OSV-1", "summary": "no severity data"}], groups=[{"ids": ["OSV-1"], "max_severity": ""}]
        )

        assert [f.severity for f in _parse_osv_scanner_output(stdout, "serial-1")] == [SBOMVulnSeverity.UNKNOWN]

    def test_a_null_database_specific_does_not_abort_the_parse(self) -> None:
        stdout = _osv_output([{"id": "OSV-1", "database_specific": None}, _ghsa("GHSA-aaaa-bbbb-cccc", "CRITICAL")])

        findings = _parse_osv_scanner_output(stdout, "serial-1")

        assert [f.severity for f in findings] == [SBOMVulnSeverity.UNKNOWN, SBOMVulnSeverity.CRITICAL]

    def test_scan_through_osv_scanner_blocks_a_critical_advisory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stdout = _osv_output(
            [_ghsa("GHSA-aaaa-bbbb-cccc", "CRITICAL")], groups=[{"ids": ["GHSA-aaaa-bbbb-cccc"], "max_severity": "9.8"}]
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/osv-scanner" if name == "osv-scanner" else None)
        monkeypatch.setattr(
            "subprocess.run",
            lambda args, **_: subprocess.CompletedProcess(args, 1, stdout=stdout, stderr=""),
        )

        result = SBOMGenerator(tmp_path).scan(_make_sbom())

        assert result.scanner == "osv-scanner"
        assert result.has_critical
        with pytest.raises(SBOMGateError):
            SBOMVulnerabilityGate().check(result)


# ---------------------------------------------------------------------------
# grype output parsing
# ---------------------------------------------------------------------------

_GRYPE_OUTPUT_CLEAN = json.dumps({"matches": []})

_GRYPE_OUTPUT_WITH_VULN = json.dumps(
    {
        "matches": [
            {
                "vulnerability": {
                    "id": "CVE-2021-23336",
                    "severity": "Medium",
                    "description": "urllib3 is affected by an SSRF vulnerability",
                    "fix": {"versions": ["1.26.5"]},
                },
                "artifact": {
                    "name": "urllib3",
                    "version": "1.26.4",
                },
            },
            {
                "vulnerability": {
                    "id": "CVE-2023-12345",
                    "severity": "Critical",
                    "description": "Remote code execution in cryptography",
                    "fix": {"versions": ["41.0.0"]},
                },
                "artifact": {
                    "name": "cryptography",
                    "version": "38.0.0",
                },
            },
        ]
    }
)


def test_parse_grype_clean_output() -> None:
    findings = _parse_grype_output(_GRYPE_OUTPUT_CLEAN, "serial-1")
    assert findings == []


def test_parse_grype_finds_vulnerabilities() -> None:
    findings = _parse_grype_output(_GRYPE_OUTPUT_WITH_VULN, "serial-1")

    assert len(findings) == 2
    by_id = {f.vuln_id: f for f in findings}

    med = by_id["CVE-2021-23336"]
    assert med.component_name == "urllib3"
    assert med.component_version == "1.26.4"
    assert med.severity == SBOMVulnSeverity.MEDIUM
    assert med.fix_version == "1.26.5"
    assert med.scanner == "grype"

    crit = by_id["CVE-2023-12345"]
    assert crit.severity == SBOMVulnSeverity.CRITICAL


def test_parse_grype_empty_string_returns_empty() -> None:
    assert _parse_grype_output("", "serial-1") == []


# ---------------------------------------------------------------------------
# SBOMVulnerabilityGate
# ---------------------------------------------------------------------------


def _make_finding(severity: SBOMVulnSeverity, vuln_id: str = "CVE-0000") -> SBOMVulnFinding:
    return SBOMVulnFinding(
        component_name="pkg",
        component_version="1.0.0",
        vuln_id=vuln_id,
        severity=severity,
        summary="test finding",
    )


def _make_scan_result(findings: list[SBOMVulnFinding]) -> SBOMScanResult:
    return SBOMScanResult(
        sbom_serial="urn:uuid:test",
        scanned_at=0.0,
        scanner="test",
        findings=findings,
    )


def test_gate_passes_when_no_findings() -> None:
    gate = SBOMVulnerabilityGate()
    result = _make_scan_result([])
    gate.check(result)  # must not raise


def test_gate_passes_for_high_when_blocking_only_critical() -> None:
    gate = SBOMVulnerabilityGate(block_on=[SBOMVulnSeverity.CRITICAL])
    result = _make_scan_result([_make_finding(SBOMVulnSeverity.HIGH)])
    gate.check(result)  # must not raise


def test_gate_blocks_on_critical_finding() -> None:
    gate = SBOMVulnerabilityGate(block_on=[SBOMVulnSeverity.CRITICAL])
    result = _make_scan_result([_make_finding(SBOMVulnSeverity.CRITICAL)])

    with pytest.raises(SBOMGateError) as exc_info:
        gate.check(result)

    assert len(exc_info.value.findings) == 1
    assert "critical" in str(exc_info.value).lower()


def test_gate_blocks_on_multiple_severities() -> None:
    gate = SBOMVulnerabilityGate(block_on=[SBOMVulnSeverity.CRITICAL, SBOMVulnSeverity.HIGH])
    result = _make_scan_result(
        [
            _make_finding(SBOMVulnSeverity.CRITICAL, "CVE-001"),
            _make_finding(SBOMVulnSeverity.HIGH, "CVE-002"),
            _make_finding(SBOMVulnSeverity.MEDIUM, "CVE-003"),
        ]
    )

    with pytest.raises(SBOMGateError) as exc_info:
        gate.check(result)

    # Only critical and high are blocked; medium is not in the gate error
    assert len(exc_info.value.findings) == 2


def test_gate_passes_returns_true_on_clean() -> None:
    gate = SBOMVulnerabilityGate()
    result = _make_scan_result([])
    assert gate.passes(result) is True


def test_gate_passes_returns_false_on_critical() -> None:
    gate = SBOMVulnerabilityGate()
    result = _make_scan_result([_make_finding(SBOMVulnSeverity.CRITICAL)])
    assert gate.passes(result) is False


def test_scan_result_highest_severity_on_empty() -> None:
    result = _make_scan_result([])
    assert result.highest_severity == SBOMVulnSeverity.NONE


def test_scan_result_highest_severity_picks_worst() -> None:
    result = _make_scan_result(
        [
            _make_finding(SBOMVulnSeverity.LOW),
            _make_finding(SBOMVulnSeverity.CRITICAL),
            _make_finding(SBOMVulnSeverity.MEDIUM),
        ]
    )
    assert result.highest_severity == SBOMVulnSeverity.CRITICAL


def test_scan_result_to_dict_structure() -> None:
    result = _make_scan_result([_make_finding(SBOMVulnSeverity.HIGH, "CVE-001")])
    d = result.to_dict()

    assert d["scanner"] == "test"
    assert d["finding_count"] == 1
    assert d["highest_severity"] == "high"
    assert len(d["findings"]) == 1
    assert d["findings"][0]["vuln_id"] == "CVE-001"
