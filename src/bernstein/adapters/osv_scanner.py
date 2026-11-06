"""Feed-pinned OSV-Scanner adapter and SARIF normalization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

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
    normalize_report_path,
)
from bernstein.adapters.scanner_finding import Finding

OSV_SCANNER_REGISTRY_NAME = "osv-scanner"
_SCAN_TIMEOUT_SECONDS = 600
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}\Z")

#: The environment variable OSV-Scanner reads its offline database root from.
_DB_ROOT_ENV = "OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"

#: Under that root, each ecosystem's database is ``osv-scanner/<ecosystem>/all.zip``.
_DB_SUBDIR = "osv-scanner"
_DB_FILE = "all.zip"

#: Config OSV-Scanner reads from a scanned directory without being asked (its ignore list).
_TARGET_CONFIG_NAME = "osv-scanner.toml"

#: OSV-Scanner exits 0 with no vulnerability and 1 with at least one. Anything else is a failed run.
_FINDINGS_EXIT_CODES = frozenset({0, 1})

#: ``Package 'smallvec@1.6.0' is vulnerable to ...`` -- the package the result is about.
_PACKAGE_IN_MESSAGE = re.compile(r"Package '([^']+)'")


class OsvScannerError(RuntimeError):
    """Base error raised when OSV-Scanner cannot complete a scan."""


class OsvScannerNotInstalledError(OsvScannerError):
    """Raised when the OSV-Scanner executable cannot be found on ``PATH``."""


@dataclass(frozen=True)
class OsvScannerInvocation:
    """Stable provenance for one feed-pinned OSV-Scanner invocation."""

    tool_version: str
    db_pin: str
    db_identity: str
    config_digest: str
    argv_hash: str


class OsvScannerAdapter(ScannerAdapter):
    """Run OSV-Scanner offline against a pinned local database and normalize its SARIF.

    OSV-Scanner normally queries osv.dev, whose answer for the same lockfile
    changes as advisories are published. The adapter therefore always passes
    ``--offline`` and points OSV-Scanner at a local database root through
    ``OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY``. The caller pins that database in
    ``scope.config["db_pin"]``; the adapter digests every ``all.zip`` under the
    root and refuses to scan when the digest differs, as the Trivy adapter does
    for ``trivy.db``.
    """

    registry_name = OSV_SCANNER_REGISTRY_NAME
    output_format = OutputFormat.SARIF
    determinism = DeterminismTier.FEED_PINNED
    pinned_inputs = ("osv_db",)
    category = ScannerCategory.SCA

    def __init__(self, *, binary: str = "osv-scanner", db_dir: str | Path | None = None) -> None:
        super().__init__()
        self._binary = binary
        self._db_dir = Path(db_dir) if db_dir is not None else None
        self.last_invocation: OsvScannerInvocation | None = None

    def name(self) -> str:
        """Return the registry key used by the conformance capability lookup."""
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run OSV-Scanner offline after verifying the caller's pin against the database on disk."""
        self.enforce_network_policy()
        db_pin = _validate_scope(target, scope)
        resolved_target = target.resolve()
        if not resolved_target.is_dir():
            raise OsvScannerError(f"OSV-Scanner target must be an existing directory: {target}")

        binary = shutil.which(self._binary)
        if binary is None:
            raise OsvScannerNotInstalledError(
                f"OSV-Scanner executable {self._binary!r} was not found on PATH; "
                "install osv-scanner or configure its binary"
            )

        db_dir = _resolve_db_dir(self._db_dir)
        db_identity = _db_identity(db_dir)
        if db_identity != db_pin:
            raise OsvScannerError(f"OSV database pin mismatch: expected {db_pin}, observed {db_identity}")

        env = build_filtered_env([])
        env[_DB_ROOT_ENV] = str(db_dir)
        tool_version = _read_version(binary, env)
        config_digest = _config_digest(_target_configs(resolved_target))

        workdir.mkdir(parents=True, exist_ok=True)
        report_path = (workdir / "osv-scanner.sarif").resolve()
        report_path.unlink(missing_ok=True)
        command = _build_command(binary, resolved_target, report_path)
        self.last_invocation = OsvScannerInvocation(
            tool_version=tool_version,
            db_pin=db_pin,
            db_identity=db_identity,
            config_digest=config_digest,
            argv_hash=_invocation_argv_hash(tool_version, db_identity, config_digest),
        )

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=_SCAN_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OsvScannerError(f"OSV-Scanner execution failed: {exc}") from exc

        if completed.returncode not in _FINDINGS_EXIT_CODES:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise OsvScannerError(f"OSV-Scanner exited with code {completed.returncode}: {detail}")
        if not report_path.is_file():
            raise OsvScannerError("OSV-Scanner completed without writing its SARIF report")

        try:
            findings = parse_osv_scanner_sarif(report_path.read_bytes(), target_root=resolved_target)
        finally:
            report_path.unlink(missing_ok=True)
        return ScanResult(
            findings=findings,
            feed_digest=db_identity,
            invocation_digest=self.last_invocation.argv_hash,
        )


def _validate_scope(target: Path, scope: ScanScope) -> str:
    if scope.include or scope.exclude or scope.max_depth is not None:
        raise ValueError("OsvScannerAdapter does not yet support include, exclude, or max_depth scan scope fields")
    unsupported = set(scope.config) - {"db_pin"}
    if unsupported:
        raise ValueError(f"Unsupported OSV-Scanner scan configuration: {', '.join(sorted(unsupported))}")
    if scope.roots:
        resolved_target = target.resolve()
        target_is_allowed = any(
            resolved_target == root.resolve() or resolved_target.is_relative_to(root.resolve()) for root in scope.roots
        )
        if not target_is_allowed:
            raise ValueError("OSV-Scanner target is outside the allowed ScanScope roots")

    pin = scope.config.get("db_pin")
    if not isinstance(pin, str) or not _SHA256_DIGEST.fullmatch(pin):
        raise OsvScannerError(
            "Feed-pinned OSV-Scanner scans require scope.config['db_pin'] as a sha256:<64 hex> digest"
        )
    return pin.lower()


def _resolve_db_dir(configured: Path | None) -> Path:
    """Resolve the database root OSV-Scanner would use, and make it explicit.

    OSV-Scanner's default is Go's ``os.UserCacheDir()``: ``~/Library/Caches`` on
    macOS, ``%LocalAppData%`` on Windows, ``$XDG_CACHE_HOME`` or ``~/.cache``
    elsewhere.
    """
    if configured is not None:
        return configured.resolve()
    if sys.platform == "darwin":
        home = os.environ.get("HOME")
        return Path(home) / "Library" / "Caches" if home else Path(tempfile.gettempdir())
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        return Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    home = os.environ.get("HOME")
    if xdg_cache and Path(xdg_cache).is_absolute():
        return Path(xdg_cache)
    if not xdg_cache and home:
        return Path(home) / ".cache"
    return Path(tempfile.gettempdir())


def _db_identity(db_dir: Path) -> str:
    """Digest every ecosystem database OSV-Scanner can load from ``db_dir``.

    The ecosystem name is hashed beside each file's bytes, so moving one
    ecosystem's archive under another name changes the identity.
    """
    base = db_dir / _DB_SUBDIR
    databases = sorted(base.glob(f"*/{_DB_FILE}")) if base.is_dir() else []
    if not databases:
        raise OsvScannerError(
            f"No OSV database exists under {base}; download one "
            "(osv-scanner --offline-vulnerabilities --download-offline-databases) before running a pinned scan"
        )
    digest = hashlib.sha256()
    digest.update(b"osv-scanner-db-v1\0")
    try:
        for database in databases:
            digest.update(database.parent.name.encode("utf-8"))
            digest.update(b"\0")
            with database.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    except OSError as exc:
        raise OsvScannerError(f"Could not hash OSV database under {base}: {exc}") from exc
    return "sha256:" + digest.hexdigest()


def _target_configs(target: Path) -> list[Path]:
    """Return every ``osv-scanner.toml`` OSV-Scanner may read in the target tree, sorted."""
    return sorted(
        Path(root) / _TARGET_CONFIG_NAME for root, _dirs, files in os.walk(target) if _TARGET_CONFIG_NAME in files
    )


def _config_digest(configs: list[Path]) -> str:
    """Digest the ignore lists that can drop a finding. Empty when there are none."""
    digest = hashlib.sha256()
    digest.update(b"osv-scanner-config-v1\0")
    for config in configs:
        digest.update(config.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _read_version(binary: str, env: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OsvScannerError(f"Could not read OSV-Scanner version: {exc}") from exc
    if completed.returncode != 0:
        raise OsvScannerError(f"Could not read OSV-Scanner version: {completed.stderr.strip()}")
    # "osv-scanner version: 2.2.3" followed by the scalibr version and commit.
    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    version = first_line.partition(":")[2].strip()
    if not version:
        raise OsvScannerError("OSV-Scanner returned an empty version")
    return version


def _build_command(binary: str, target: Path, report_path: Path) -> list[str]:
    return [
        binary,
        "scan",
        "source",
        "--offline",
        "--allow-no-lockfiles",
        "--recursive",
        "--format",
        "sarif",
        "--output",
        str(report_path),
        str(target),
    ]


def _invocation_argv_hash(tool_version: str, db_identity: str, config_digest: str) -> str:
    semantic_invocation = {
        "command": "scan source",
        "config_digest": config_digest,
        "db_identity": db_identity,
        "offline": True,
        "report_format": "sarif",
        "tool": OSV_SCANNER_REGISTRY_NAME,
        "tool_version": tool_version,
    }
    canonical = json.dumps(semantic_invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def parse_osv_scanner_sarif(report: str | bytes, *, target_root: Path | None = None) -> list[Finding]:
    """Parse an OSV-Scanner SARIF report into stable Bernstein findings.

    A finding is the vulnerability id, the lockfile path and the affected
    package. OSV-Scanner reports no source region for a lockfile, so there is
    no line number to keep out of the identity. It can emit the same result
    more than once for one package; an exact repeat carries nothing new and is
    reported once.

    Args:
        report: UTF-8 SARIF JSON emitted by ``osv-scanner scan --format sarif``.
        target_root: Scan root used to make OSV-Scanner's absolute paths portable.

    Returns:
        Findings in report order, exact repeats removed.

    Raises:
        ValueError: If the report is not valid OSV-Scanner SARIF.
    """
    try:
        raw = json.loads(report)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid OSV-Scanner SARIF JSON: {exc}") from exc

    root = _mapping(raw, "SARIF root")
    if root.get("version") != "2.1.0":
        raise ValueError("OSV-Scanner report must use SARIF 2.1.0")

    findings: list[Finding] = []
    seen: set[str] = set()
    for run_index, run_raw in enumerate(_sequence(root.get("runs"), "runs")):
        run = _mapping(run_raw, f"runs[{run_index}]")
        driver = _mapping(_mapping(run.get("tool"), "tool").get("driver"), "tool.driver")
        if driver.get("name") != "osv-scanner":
            raise ValueError("SARIF tool.driver.name must be 'osv-scanner'")
        severities = _rule_severities(driver)

        for result_index, result_raw in enumerate(_sequence(run.get("results", []), "results")):
            result = _mapping(result_raw, f"results[{result_index}]")
            rule = str(result.get("ruleId") or "")
            if not rule:
                raise ValueError(f"results[{result_index}] is missing ruleId")
            path = _result_path(result, result_index, target_root)
            message = str(_mapping(result.get("message", {}), "message").get("text") or rule)
            package_match = _PACKAGE_IN_MESSAGE.search(message)
            finding = Finding(
                rule=rule,
                path=path,
                severity=severities.get(rule, "informational"),
                summary=message,
                extra={"package": package_match.group(1) if package_match else ""},
            )
            finding_hash = finding.finding_hash()
            if finding_hash in seen:
                continue
            seen.add(finding_hash)
            findings.append(finding)

    return findings


def _severity_from_score(score: float) -> str:
    """Map a CVSS base score to a Finding severity, using the CVSS v3 bands."""
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0.0:
        return "low"
    return "informational"


def _rule_severities(driver: dict[str, Any]) -> dict[str, str]:
    severities: dict[str, str] = {}
    for rule_raw in _sequence(driver.get("rules", []), "tool.driver.rules"):
        rule = _mapping(rule_raw, "tool.driver.rules entry")
        rule_id = str(rule.get("id") or "")
        properties = _mapping(rule.get("properties", {}), "rule.properties")
        try:
            score = float(str(properties.get("security-severity", "")))
        except ValueError:
            continue
        if rule_id:
            severities[rule_id] = _severity_from_score(score)
    return severities


def _result_path(result: dict[str, Any], result_index: int, target_root: Path | None) -> str:
    locations = _sequence(result.get("locations"), f"results[{result_index}].locations")
    if not locations:
        raise ValueError(f"results[{result_index}] has no location")
    location = _mapping(locations[0], "location")
    physical = _mapping(location.get("physicalLocation"), "physicalLocation")
    artifact = _mapping(physical.get("artifactLocation"), "artifactLocation")
    uri = str(artifact.get("uri") or "")
    if not uri:
        raise ValueError(f"results[{result_index}] is missing artifactLocation.uri")
    parsed = urlparse(uri)
    path = unquote(parsed.path) if parsed.scheme == "file" else unquote(uri)
    return normalize_report_path(path, target_root)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast("dict[str, Any]", value)


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast("list[Any]", value)


register_scanner_capabilities(
    OSV_SCANNER_REGISTRY_NAME,
    output_format=ScannerOutputFormat.SARIF,
    determinism=ScannerDeterminism.FEED_PINNED,
    pinned_inputs=("osv_db",),
    category=ContractScannerCategory.SCA,
)
