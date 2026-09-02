"""Transcript-anchored Nmap scanner adapter and XML normalization."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from bernstein.adapters._contract import (
    ScannerCategory as ContractScannerCategory,
)
from bernstein.adapters._contract import (
    ScannerDeterminism,
    ScannerOutputFormat,
    register_scanner_capabilities,
)
from bernstein.adapters.env_isolation import build_filtered_env  # pyright: ignore[reportUnknownVariableType]
from bernstein.adapters.scanner import (
    DeterminismTier,
    OutputFormat,
    ScannerAdapter,
    ScannerCategory,
    ScanResult,
    ScanScope,
)
from bernstein.adapters.scanner_finding import Finding

if TYPE_CHECKING:
    from pathlib import Path

NMAP_REGISTRY_NAME = "nmap"
_SCAN_TIMEOUT_SECONDS = 300
_DEFAULT_PORTS = "1-1024"
_PORT_SPEC = re.compile(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*\Z")


class NmapError(RuntimeError):
    """Base error raised when Nmap cannot complete a scan."""


class NmapNotInstalledError(NmapError):
    """Raised when the Nmap executable cannot be found on ``PATH``."""


@dataclass(frozen=True, order=True)
class NmapPortFact:
    """Stable identity of one network port observation."""

    host: str
    protocol: str
    port: int
    state: str
    service_name: str

    def to_finding(self) -> Finding:
        """Represent this canonical port fact as a Bernstein finding."""
        return Finding(
            rule=f"nmap-port:{self.protocol}:{self.port}",
            path=self.host,
            severity="informational",
            summary=f"{self.state} {self.service_name}",
        )


@dataclass(frozen=True)
class NmapNormalization:
    """Canonical facts, findings, and transcript derived from Nmap XML."""

    facts: tuple[NmapPortFact, ...]
    findings: tuple[Finding, ...]
    transcript: str
    tool_version: str


@dataclass(frozen=True)
class NmapInvocation:
    """Stable provenance for one Nmap invocation."""

    tool_version: str
    target: str
    ports: str
    argv_hash: str


class NmapAdapter(ScannerAdapter):
    """Run Nmap connect scans and record a canonical transcript."""

    registry_name = NMAP_REGISTRY_NAME
    output_format = OutputFormat.XML
    determinism = DeterminismTier.TRANSCRIPT_ANCHORED
    pinned_inputs: tuple[str, ...] = ()
    category = ScannerCategory.RECON

    def __init__(self, *, binary: str = "nmap") -> None:
        super().__init__()
        self._binary = binary
        self.last_invocation: NmapInvocation | None = None

    def name(self) -> str:
        """Return the registry key used by the conformance capability lookup."""
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run an Nmap scan and normalize its volatile XML into a transcript."""
        self.enforce_network_policy()
        target_name, ports = _validate_scope(target, scope)
        binary = shutil.which(self._binary)
        if binary is None:
            raise NmapNotInstalledError(
                f"Nmap executable {self._binary!r} was not found on PATH; install nmap or configure its binary"
            )

        tool_version = _read_version(binary)
        workdir.mkdir(parents=True, exist_ok=True)
        report_path = (workdir / "nmap.xml").resolve()
        report_path.unlink(missing_ok=True)
        command = _build_command(binary, target_name, ports, report_path)
        self.last_invocation = NmapInvocation(
            tool_version=tool_version,
            target=target_name,
            ports=ports,
            argv_hash=_invocation_argv_hash(tool_version, target_name, ports),
        )

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=build_filtered_env([]),
                timeout=_SCAN_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NmapError(f"Nmap execution failed: {exc}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise NmapError(f"Nmap exited with code {completed.returncode}: {detail}")
        if not report_path.is_file():
            raise NmapError("Nmap completed without writing its XML report")

        try:
            normalized = normalize_nmap_xml(report_path.read_bytes())
        finally:
            report_path.unlink(missing_ok=True)
        return ScanResult(findings=list(normalized.findings), transcript=normalized.transcript)


def _validate_scope(target: Path, scope: ScanScope) -> tuple[str, str]:
    if scope.roots or scope.include or scope.exclude or scope.max_depth is not None:
        raise ValueError("NmapAdapter does not support roots, include, exclude, or max_depth scan scope fields")
    unsupported = set(scope.config) - {"ports"}
    if unsupported:
        raise ValueError(f"Unsupported Nmap scan configuration: {', '.join(sorted(unsupported))}")

    target_name = str(target).strip()
    if not target_name or target_name == "." or target_name.startswith("-"):
        raise ValueError("Nmap target must be an explicit host or address")
    ports = str(scope.config.get("ports", _DEFAULT_PORTS)).strip()
    if not _valid_port_spec(ports):
        raise ValueError("Nmap ports must be comma-separated ports or ranges between 1 and 65535")
    return target_name, ports


def _valid_port_spec(ports: str) -> bool:
    if not _PORT_SPEC.fullmatch(ports):
        return False
    for item in ports.split(","):
        start_text, separator, end_text = item.partition("-")
        start = int(start_text)
        end = int(end_text) if separator else start
        if not 1 <= start <= end <= 65535:
            return False
    return True


def _read_version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=build_filtered_env([]),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NmapError(f"Could not read Nmap version: {exc}") from exc
    if completed.returncode != 0:
        raise NmapError(f"Could not read Nmap version: {completed.stderr.strip()}")
    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    match = re.search(r"Nmap version\s+(\S+)", first_line)
    if match is None:
        raise NmapError("Nmap returned an unrecognized version")
    return match.group(1)


def _build_command(binary: str, target: str, ports: str, report_path: Path) -> list[str]:
    return [
        binary,
        "-sT",
        "-sV",
        "-Pn",
        "--no-stylesheet",
        "-p",
        ports,
        "-oX",
        str(report_path),
        target,
    ]


def _invocation_argv_hash(tool_version: str, target: str, ports: str) -> str:
    semantic_invocation = {
        "host_discovery": False,
        "ports": ports,
        "report_format": "xml",
        "scan_type": "connect",
        "service_detection": True,
        "target": target,
        "tool": NMAP_REGISTRY_NAME,
        "tool_version": tool_version,
    }
    canonical = json.dumps(semantic_invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def normalize_nmap_xml(report: str | bytes) -> NmapNormalization:
    """Turn Nmap XML into timestamp-free canonical facts and a transcript."""
    try:
        root = DefusedET.fromstring(report)
    except (ET.ParseError, DefusedXmlException, UnicodeError) as exc:
        raise ValueError(f"Invalid Nmap XML: {exc}") from exc
    if root.tag != "nmaprun" or root.get("scanner") != "nmap":
        raise ValueError("Nmap XML root must be an nmaprun produced by scanner 'nmap'")
    tool_version = root.get("version", "").strip()
    if not tool_version:
        raise ValueError("Nmap XML is missing the tool version")

    facts: list[NmapPortFact] = []
    transcript_ports: list[dict[str, object]] = []
    for host_element in root.findall("host"):
        host = _host_identity(host_element)
        for port_element in host_element.findall("./ports/port"):
            fact, service_details = _port_record(host, port_element)
            facts.append(fact)
            transcript_ports.append(
                {
                    "host": fact.host,
                    "port": fact.port,
                    "protocol": fact.protocol,
                    "service": service_details,
                    "state": fact.state,
                }
            )

    facts.sort()
    transcript_ports.sort(key=lambda item: (str(item["host"]), str(item["protocol"]), cast("int", item["port"])))
    transcript_record = {
        "ports": transcript_ports,
        "tool": {"name": NMAP_REGISTRY_NAME, "version": tool_version},
    }
    transcript = json.dumps(transcript_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return NmapNormalization(
        facts=tuple(facts),
        findings=tuple(fact.to_finding() for fact in facts),
        transcript=transcript,
        tool_version=tool_version,
    )


def _host_identity(host: ET.Element) -> str:
    addresses = host.findall("address")
    for address_type in ("ipv4", "ipv6", "mac"):
        for address in addresses:
            value = address.get("addr", "").strip()
            if address.get("addrtype") == address_type and value:
                return value
    hostname = host.find("./hostnames/hostname")
    if hostname is not None and hostname.get("name", "").strip():
        return hostname.get("name", "").strip()
    raise ValueError("Nmap host is missing an address or hostname")


def _port_record(host: str, port: ET.Element) -> tuple[NmapPortFact, dict[str, object]]:
    protocol = port.get("protocol", "").strip()
    port_text = port.get("portid", "").strip()
    state_element = port.find("state")
    state = state_element.get("state", "").strip() if state_element is not None else ""
    if not protocol or not port_text.isdigit() or not state:
        raise ValueError("Nmap port is missing protocol, numeric portid, or state")
    port_number = int(port_text)
    if not 1 <= port_number <= 65535:
        raise ValueError(f"Nmap port is outside the valid range: {port_number}")

    service = port.find("service")
    service_name = service.get("name", "unknown").strip() if service is not None else "unknown"
    service_name = service_name or "unknown"
    details: dict[str, object] = {"name": service_name}
    if service is not None:
        for xml_name, transcript_name in (
            ("product", "product"),
            ("version", "version"),
            ("extrainfo", "extra_info"),
            ("servicefp", "service_fingerprint"),
            ("tunnel", "tunnel"),
        ):
            value = service.get(xml_name, "").strip()
            if value:
                details[transcript_name] = value
        cpes = sorted(cpe.text.strip() for cpe in service.findall("cpe") if cpe.text and cpe.text.strip())
        if cpes:
            details["cpes"] = cpes
    return NmapPortFact(host, protocol, port_number, state, service_name), details


register_scanner_capabilities(
    NMAP_REGISTRY_NAME,
    output_format=ScannerOutputFormat.XML,
    determinism=ScannerDeterminism.TRANSCRIPT_ANCHORED,
    pinned_inputs=(),
    category=ContractScannerCategory.RECON,
)
