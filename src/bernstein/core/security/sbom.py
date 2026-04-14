"""SBOM (Software Bill of Materials) generation for agent-produced artifacts.

Generates CycloneDX 1.5 JSON SBOMs from project dependencies and optionally
runs vulnerability scanning via ``osv-scanner`` or ``grype``.

When an agent installs new packages the orchestrator can call:

    generator = SBOMGenerator(workdir)
    sbom = generator.generate()
    generator.save(sbom)
    result = generator.scan(sbom)
    gate = SBOMVulnerabilityGate(block_on=[SBOMVulnSeverity.CRITICAL])
    gate.check(result)   # raises SBOMGateError when critical findings exist

SBOM artifacts are written to ``.sdd/artifacts/sbom/``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# CycloneDX spec version emitted by this generator.
_CYCLONEDX_SPEC_VERSION = "1.5"
_BERNSTEIN_TOOL_NAME = "bernstein"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SBOMFormat(StrEnum):
    """Supported SBOM output formats."""

    CYCLONEDX_JSON = "cyclonedx-json"
    SPDX_JSON = "spdx-json"


class SBOMVulnSeverity(StrEnum):
    """Standardised vulnerability severity levels (CVSS-aligned)."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"
    UNKNOWN = "unknown"


_SEVERITY_ORDER: dict[SBOMVulnSeverity, int] = {
    SBOMVulnSeverity.CRITICAL: 5,
    SBOMVulnSeverity.HIGH: 4,
    SBOMVulnSeverity.MEDIUM: 3,
    SBOMVulnSeverity.LOW: 2,
    SBOMVulnSeverity.NONE: 1,
    SBOMVulnSeverity.UNKNOWN: 0,
}


def _severity_from_str(value: str) -> SBOMVulnSeverity:
    """Map an arbitrary severity string to SBOMVulnSeverity."""
    normalised = value.strip().lower()
    for sev in SBOMVulnSeverity:
        if sev.value == normalised:
            return sev
    return SBOMVulnSeverity.UNKNOWN


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------


@dataclass
class SBOMComponent:
    """A single package component in the SBOM."""

    name: str
    version: str
    purl: str  # Package URL — pkg:pypi/requests@2.28.0
    component_type: str = "library"  # library | framework | application | container
    description: str = ""
    licenses: list[str] = field(default_factory=list)

    def to_cyclonedx_dict(self) -> dict[str, Any]:
        """Serialise to a CycloneDX 1.5 component dict."""
        result: dict[str, Any] = {
            "type": self.component_type,
            "name": self.name,
            "version": self.version,
            "purl": self.purl,
        }
        if self.description:
            result["description"] = self.description
        if self.licenses:
            result["licenses"] = [{"license": {"name": lic}} for lic in self.licenses]
        return result


@dataclass
class SBOMDocument:
    """A CycloneDX or SPDX SBOM document."""

    serial_number: str
    generated_at: float
    components: list[SBOMComponent]
    metadata: dict[str, Any] = field(default_factory=dict)
    sbom_format: SBOMFormat = SBOMFormat.CYCLONEDX_JSON
    source: str = ""  # "pip", "npm", "requirements.txt", etc.

    def to_cyclonedx_dict(self) -> dict[str, Any]:
        """Serialise to CycloneDX 1.5 JSON-compatible dict."""
        import datetime

        ts = datetime.datetime.fromtimestamp(self.generated_at, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "bomFormat": "CycloneDX",
            "specVersion": _CYCLONEDX_SPEC_VERSION,
            "serialNumber": self.serial_number,
            "version": 1,
            "metadata": {
                "timestamp": ts,
                "tools": [{"name": _BERNSTEIN_TOOL_NAME}],
                **self.metadata,
            },
            "components": [c.to_cyclonedx_dict() for c in self.components],
        }

    def to_spdx_dict(self) -> dict[str, Any]:
        """Serialise to a minimal SPDX 2.3 JSON-compatible dict."""
        import datetime

        ts = datetime.datetime.fromtimestamp(self.generated_at, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        packages = []
        for comp in self.components:
            pkg: dict[str, Any] = {
                "SPDXID": f"SPDXRef-{comp.name}-{comp.version}".replace(" ", "-"),
                "name": comp.name,
                "versionInfo": comp.version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": comp.purl,
                    }
                ],
            }
            if comp.licenses:
                pkg["licenseConcluded"] = " AND ".join(comp.licenses)
                pkg["licenseDeclared"] = " AND ".join(comp.licenses)
            packages.append(pkg)

        return {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"bernstein-sbom-{self.serial_number}",
            "documentNamespace": f"https://bernstein.ai/sbom/{self.serial_number}",
            "creationInfo": {
                "created": ts,
                "creators": [f"Tool: {_BERNSTEIN_TOOL_NAME}"],
            },
            "packages": packages,
        }

    def to_json(self) -> str:
        """Serialise to the configured format as a JSON string."""
        if self.sbom_format == SBOMFormat.SPDX_JSON:
            return json.dumps(self.to_spdx_dict(), indent=2, sort_keys=True)
        return json.dumps(self.to_cyclonedx_dict(), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Vulnerability findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SBOMVulnFinding:
    """A single vulnerability finding from scanning an SBOM."""

    component_name: str
    component_version: str
    vuln_id: str  # CVE-... or GHSA-... or OSV-...
    severity: SBOMVulnSeverity
    summary: str
    fix_version: str = ""
    scanner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_name": self.component_name,
            "component_version": self.component_version,
            "vuln_id": self.vuln_id,
            "severity": self.severity.value,
            "summary": self.summary,
            "fix_version": self.fix_version,
            "scanner": self.scanner,
        }


@dataclass
class SBOMScanResult:
    """Result of vulnerability scanning an SBOM."""

    sbom_serial: str
    scanned_at: float
    scanner: str
    findings: list[SBOMVulnFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return any(f.severity == SBOMVulnSeverity.CRITICAL for f in self.findings)

    @property
    def has_high(self) -> bool:
        return any(f.severity == SBOMVulnSeverity.HIGH for f in self.findings)

    @property
    def highest_severity(self) -> SBOMVulnSeverity:
        if not self.findings:
            return SBOMVulnSeverity.NONE
        return max(self.findings, key=lambda f: _SEVERITY_ORDER[f.severity]).severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "sbom_serial": self.sbom_serial,
            "scanned_at": self.scanned_at,
            "scanner": self.scanner,
            "finding_count": len(self.findings),
            "highest_severity": self.highest_severity.value,
            "findings": [f.to_dict() for f in self.findings],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Gate error
# ---------------------------------------------------------------------------


class SBOMGateError(Exception):
    """Raised by SBOMVulnerabilityGate when critical findings block merge."""

    def __init__(self, findings: list[SBOMVulnFinding]) -> None:
        self.findings = findings
        counts = {}
        for f in findings:
            counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        super().__init__(f"SBOM gate blocked: {summary} ({len(findings)} total finding(s))")


# ---------------------------------------------------------------------------
# SBOM Generator
# ---------------------------------------------------------------------------


def _purl_for_python_package(name: str, version: str) -> str:
    """Return the Package URL for a Python package."""
    # purl spec: pkg:pypi/<name>@<version>
    return f"pkg:pypi/{name.lower().replace('_', '-').replace('.', '-')}@{version}"


def _collect_python_packages() -> list[SBOMComponent]:
    """Return installed Python packages via importlib.metadata."""
    try:
        import importlib.metadata as importlib_metadata
    except ImportError:
        return []

    components: list[SBOMComponent] = []
    for dist in importlib_metadata.distributions():
        try:
            name = dist.metadata["Name"] or ""
            version = dist.metadata["Version"] or ""
            if not name or not version:
                continue
            description = dist.metadata.get("Summary", "") or ""
            license_val = dist.metadata.get("License", "") or ""
            licenses = [license_val] if license_val and license_val != "UNKNOWN" else []
            components.append(
                SBOMComponent(
                    name=name,
                    version=version,
                    purl=_purl_for_python_package(name, version),
                    description=description,
                    licenses=licenses,
                )
            )
        except Exception:
            continue
    return sorted(components, key=lambda c: c.name.lower())


class SBOMGenerator:
    """Generate and scan SBOMs for a project.

    Supports:
    - CycloneDX 1.5 JSON output
    - SPDX 2.3 JSON output
    - Vulnerability scanning via osv-scanner or grype

    Artifacts are written to ``<workdir>/.sdd/artifacts/sbom/``.
    """

    def __init__(
        self,
        workdir: Path,
        *,
        sbom_format: SBOMFormat = SBOMFormat.CYCLONEDX_JSON,
        scan_timeout_s: int = 120,
    ) -> None:
        self._workdir = workdir
        self._sbom_format = sbom_format
        self._scan_timeout_s = scan_timeout_s
        self._artifact_dir = workdir / ".sdd" / "artifacts" / "sbom"

    def generate(self, *, source: str = "pip") -> SBOMDocument:
        """Generate an SBOM from the project's installed Python packages.

        Args:
            source: Package source label (e.g., "pip", "npm", "requirements.txt").

        Returns:
            SBOMDocument with all collected components.
        """
        components = _collect_python_packages()
        return SBOMDocument(
            serial_number=f"urn:uuid:{uuid.uuid4()}",
            generated_at=time.time(),
            components=components,
            sbom_format=self._sbom_format,
            source=source,
        )

    def save(self, sbom: SBOMDocument, *, filename: str | None = None) -> Path:
        """Write the SBOM to ``.sdd/artifacts/sbom/``.

        Args:
            sbom: The SBOM document to save.
            filename: Override the output filename (default: ``sbom-<serial>.json``).

        Returns:
            Path to the written file.
        """
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        if filename is None:
            short_serial = sbom.serial_number.split(":")[-1].replace("-", "")[:12]
            ext = "json"
            filename = f"sbom-{short_serial}.{ext}"
        out_path = self._artifact_dir / filename
        out_path.write_text(sbom.to_json(), encoding="utf-8")
        logger.info("SBOM saved to %s (%d components)", out_path, len(sbom.components))
        return out_path

    def scan(self, sbom: SBOMDocument) -> SBOMScanResult:
        """Run vulnerability scanning against the SBOM.

        Tries osv-scanner first, then grype, returning the first successful result.
        Returns an empty result (no findings, with a warning) if neither tool is
        available.

        Args:
            sbom: The SBOM document to scan.

        Returns:
            SBOMScanResult with any vulnerability findings.
        """
        # Save to a temp file for scanning
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        sbom_path = self._artifact_dir / f"scan-input-{uuid.uuid4().hex[:8]}.json"
        sbom_path.write_text(sbom.to_json(), encoding="utf-8")
        try:
            if shutil.which("osv-scanner"):
                result = self._scan_with_osv_scanner(sbom, sbom_path)
                if not result.errors:
                    return result
            if shutil.which("grype"):
                return self._scan_with_grype(sbom, sbom_path)
            # Neither scanner available — return empty result with warning
            logger.warning("Neither osv-scanner nor grype is available; SBOM scan skipped")
            return SBOMScanResult(
                sbom_serial=sbom.serial_number,
                scanned_at=time.time(),
                scanner="none",
                errors=["No vulnerability scanner available (install osv-scanner or grype)"],
            )
        finally:
            import contextlib

            with contextlib.suppress(OSError):
                sbom_path.unlink()

    # -- Private scanner backends -------------------------------------------

    def _scan_with_osv_scanner(self, sbom: SBOMDocument, sbom_path: Path) -> SBOMScanResult:
        """Run osv-scanner against the CycloneDX SBOM file."""
        try:
            completed = subprocess.run(
                ["osv-scanner", "--format=json", "--sbom", str(sbom_path)],
                cwd=str(self._workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=self._scan_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SBOMScanResult(
                sbom_serial=sbom.serial_number,
                scanned_at=time.time(),
                scanner="osv-scanner",
                errors=[f"osv-scanner execution failed: {exc}"],
            )

        findings = _parse_osv_scanner_output(completed.stdout, sbom.serial_number)
        errors: list[str] = []
        if completed.returncode not in (0, 1) and completed.stderr:  # 0=clean, 1=vulnerabilities found
            errors.append(completed.stderr.strip()[:500])

        return SBOMScanResult(
            sbom_serial=sbom.serial_number,
            scanned_at=time.time(),
            scanner="osv-scanner",
            findings=findings,
            errors=errors,
        )

    def _scan_with_grype(self, sbom: SBOMDocument, sbom_path: Path) -> SBOMScanResult:
        """Run grype against the CycloneDX SBOM file."""
        try:
            completed = subprocess.run(
                ["grype", f"sbom:{sbom_path}", "-o", "json"],
                cwd=str(self._workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=self._scan_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SBOMScanResult(
                sbom_serial=sbom.serial_number,
                scanned_at=time.time(),
                scanner="grype",
                errors=[f"grype execution failed: {exc}"],
            )

        findings = _parse_grype_output(completed.stdout, sbom.serial_number)
        errors: list[str] = []
        if completed.returncode not in (0, 1) and completed.stderr:
            errors.append(completed.stderr.strip()[:500])

        return SBOMScanResult(
            sbom_serial=sbom.serial_number,
            scanned_at=time.time(),
            scanner="grype",
            findings=findings,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Scanner output parsers
# ---------------------------------------------------------------------------


def _osv_extract_severity(vuln: dict[str, Any]) -> SBOMVulnSeverity:
    """Extract severity from an osv-scanner vulnerability entry."""
    severity_raw = ""
    for db_info in vuln.get("database_specific", {}).get("severity", []):
        if isinstance(db_info, dict):
            severity_raw = str(db_info.get("score", "")).upper()
            break
    if not severity_raw:
        severity_raw = str(vuln.get("severity", "") or "").lower()
    return _severity_from_str(severity_raw) if severity_raw else SBOMVulnSeverity.UNKNOWN


def _osv_extract_fix_version(vuln: dict[str, Any]) -> str:
    """Extract the earliest fix version from an osv-scanner vulnerability."""
    for affected in vuln.get("affected", []):
        if not isinstance(affected, dict):
            continue
        for rng in affected.get("ranges", []):
            if not isinstance(rng, dict):
                continue
            for event in rng.get("events", []):
                if isinstance(event, dict) and "fixed" in event:
                    return str(event["fixed"])
    return ""


def _osv_parse_vuln(vuln: dict[str, Any], pkg_name: str, pkg_version: str) -> SBOMVulnFinding | None:
    """Parse a single osv-scanner vulnerability into a finding."""
    if not isinstance(vuln, dict):
        return None
    vuln_id = str(vuln.get("id", "unknown"))
    summary = str(vuln.get("summary", "") or vuln.get("details", ""))[:300]
    return SBOMVulnFinding(
        component_name=pkg_name,
        component_version=pkg_version,
        vuln_id=vuln_id,
        severity=_osv_extract_severity(vuln),
        summary=summary,
        fix_version=_osv_extract_fix_version(vuln),
        scanner="osv-scanner",
    )


def _osv_parse_package_entry(pkg_entry: dict[str, Any]) -> list[SBOMVulnFinding]:
    """Parse all vulnerabilities from a single osv-scanner package entry."""
    if not isinstance(pkg_entry, dict):
        return []
    pkg = pkg_entry.get("package", {})
    if not isinstance(pkg, dict):
        return []
    pkg_name = str(pkg.get("name", ""))
    pkg_version = str(pkg.get("version", ""))
    findings: list[SBOMVulnFinding] = []
    for vuln in pkg_entry.get("vulnerabilities", []):
        finding = _osv_parse_vuln(vuln, pkg_name, pkg_version)
        if finding is not None:
            findings.append(finding)
    return findings


def _parse_osv_scanner_output(stdout: str, _sbom_serial: str) -> list[SBOMVulnFinding]:
    """Parse JSON output from ``osv-scanner --format=json``."""
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("osv-scanner: could not parse JSON output")
        return []

    # osv-scanner JSON schema: {"results": [{"packages": [{"package": {...}, "vulnerabilities": [...]}]}]}
    results = payload.get("results", []) if isinstance(payload, dict) else []
    findings: list[SBOMVulnFinding] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        for pkg_entry in result.get("packages", []):
            findings.extend(_osv_parse_package_entry(pkg_entry))
    return findings


def _grype_parse_match(match: dict[str, Any]) -> SBOMVulnFinding | None:
    """Parse a single grype match entry into a finding."""
    vuln = match.get("vulnerability", {})
    artifact = match.get("artifact", {})
    if not isinstance(vuln, dict) or not isinstance(artifact, dict):
        return None
    fix_versions = vuln.get("fix", {})
    fix_version = ""
    if isinstance(fix_versions, dict):
        fix_list = fix_versions.get("versions", [])
        if fix_list:
            fix_version = str(fix_list[0])
    return SBOMVulnFinding(
        component_name=str(artifact.get("name", "")),
        component_version=str(artifact.get("version", "")),
        vuln_id=str(vuln.get("id", "unknown")),
        severity=_severity_from_str(str(vuln.get("severity", "unknown"))),
        summary=str(vuln.get("description", "") or vuln.get("url", ""))[:300],
        fix_version=fix_version,
        scanner="grype",
    )


def _parse_grype_output(stdout: str, _sbom_serial: str) -> list[SBOMVulnFinding]:
    """Parse JSON output from ``grype -o json``."""
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("grype: could not parse JSON output")
        return []

    matches = payload.get("matches", []) if isinstance(payload, dict) else []
    findings: list[SBOMVulnFinding] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        finding = _grype_parse_match(match)
        if finding is not None:
            findings.append(finding)
    return findings


# ---------------------------------------------------------------------------
# Vulnerability gate
# ---------------------------------------------------------------------------


class SBOMVulnerabilityGate:
    """Block merge when SBOM scan findings meet or exceed a severity threshold.

    Example::

        gate = SBOMVulnerabilityGate(block_on=[SBOMVulnSeverity.CRITICAL, SBOMVulnSeverity.HIGH])
        gate.check(scan_result)  # raises SBOMGateError if any high/critical findings

    Args:
        block_on: Severity levels that will cause gate failure.  Defaults to
            blocking on CRITICAL findings only.
    """

    def __init__(
        self,
        block_on: list[SBOMVulnSeverity] | None = None,
    ) -> None:
        self._block_on: frozenset[SBOMVulnSeverity] = frozenset(
            block_on if block_on is not None else [SBOMVulnSeverity.CRITICAL]
        )

    def check(self, result: SBOMScanResult) -> None:
        """Raise SBOMGateError if the scan result contains blocked severities.

        Args:
            result: The scan result to check.

        Raises:
            SBOMGateError: When one or more findings match the blocked severities.
        """
        blocked = [f for f in result.findings if f.severity in self._block_on]
        if blocked:
            raise SBOMGateError(blocked)

    def passes(self, result: SBOMScanResult) -> bool:
        """Return True when the scan result passes the gate (no blocked findings)."""
        try:
            self.check(result)
            return True
        except SBOMGateError:
            return False
