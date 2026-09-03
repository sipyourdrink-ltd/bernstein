"""Feed-pinned Trivy scanner adapter and SARIF normalization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
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
)
from bernstein.adapters.scanner_finding import Finding

TRIVY_REGISTRY_NAME = "trivy"
_SCAN_TIMEOUT_SECONDS = 300
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}\Z")


class TrivyError(RuntimeError):
    """Base error raised when Trivy cannot complete a scan."""


class TrivyNotInstalledError(TrivyError):
    """Raised when the Trivy executable cannot be found on ``PATH``."""


@dataclass(frozen=True)
class TrivyInvocation:
    """Stable provenance for one feed-pinned Trivy invocation."""

    tool_version: str
    db_pin: str
    db_identity: str
    argv_hash: str


class TrivyAdapter(ScannerAdapter):
    """Run feed-pinned Trivy filesystem scans and normalize SARIF findings."""

    registry_name = TRIVY_REGISTRY_NAME
    output_format = OutputFormat.SARIF
    determinism = DeterminismTier.FEED_PINNED
    pinned_inputs = ("trivy_db",)
    category = ScannerCategory.SCA

    def __init__(self, *, binary: str = "trivy", cache_dir: str | Path | None = None) -> None:
        super().__init__()
        self._binary = binary
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.last_invocation: TrivyInvocation | None = None

    def name(self) -> str:
        """Return the registry key used by the conformance capability lookup."""
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run Trivy after verifying the caller's pin against the database on disk."""
        self.enforce_network_policy()
        db_pin = _validate_scope(target, scope)
        resolved_target = target.resolve()
        if not resolved_target.exists():
            raise TrivyError(f"Trivy target does not exist: {target}")

        binary = shutil.which(self._binary)
        if binary is None:
            raise TrivyNotInstalledError(
                f"Trivy executable {self._binary!r} was not found on PATH; install trivy or configure its binary"
            )

        cache_dir = _resolve_cache_dir(self._cache_dir)
        db_identity = _db_identity(cache_dir)
        if db_identity != db_pin:
            raise TrivyError(f"Trivy database pin mismatch: expected {db_pin}, observed {db_identity}")

        tool_version = _read_version(binary)
        workdir.mkdir(parents=True, exist_ok=True)
        report_path = (workdir / "trivy.sarif").resolve()
        report_path.unlink(missing_ok=True)
        command = _build_command(binary, resolved_target, report_path, cache_dir)
        self.last_invocation = TrivyInvocation(
            tool_version=tool_version,
            db_pin=db_pin,
            db_identity=db_identity,
            argv_hash=_invocation_argv_hash(tool_version, db_identity),
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
            raise TrivyError(f"Trivy execution failed: {exc}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise TrivyError(f"Trivy exited with code {completed.returncode}: {detail}")
        if not report_path.is_file():
            raise TrivyError("Trivy completed without writing its SARIF report")

        try:
            findings = parse_trivy_sarif(report_path.read_bytes(), target_root=resolved_target)
        finally:
            report_path.unlink(missing_ok=True)
        return ScanResult(
            findings=findings,
            feed_digest=db_identity,
            invocation_digest=self.last_invocation.argv_hash,
        )


def _validate_scope(target: Path, scope: ScanScope) -> str:
    if scope.include or scope.exclude or scope.max_depth is not None:
        raise ValueError("TrivyAdapter does not yet support include, exclude, or max_depth scan scope fields")
    unsupported = set(scope.config) - {"db_pin"}
    if unsupported:
        raise ValueError(f"Unsupported Trivy scan configuration: {', '.join(sorted(unsupported))}")
    if scope.roots:
        resolved_target = target.resolve()
        target_is_allowed = any(
            resolved_target == root.resolve() or resolved_target.is_relative_to(root.resolve()) for root in scope.roots
        )
        if not target_is_allowed:
            raise ValueError("Trivy target is outside the allowed ScanScope roots")

    pin = scope.config.get("db_pin")
    if not isinstance(pin, str) or not _SHA256_DIGEST.fullmatch(pin):
        raise TrivyError("Feed-pinned Trivy scans require scope.config['db_pin'] as a sha256:<64 hex> digest")
    return pin.lower()


def _resolve_cache_dir(configured: Path | None) -> Path:
    """Resolve Trivy's platform default and make it explicit on the command line."""
    if configured is not None:
        return configured.resolve()
    if sys.platform == "darwin":
        home = os.environ.get("HOME")
        base = Path(home) / "Library" / "Caches" if home else Path(tempfile.gettempdir())
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        home = os.environ.get("HOME")
        if xdg_cache and Path(xdg_cache).is_absolute():
            base = Path(xdg_cache)
        elif not xdg_cache and home:
            base = Path(home) / ".cache"
        else:
            base = Path(tempfile.gettempdir())
    return base / "trivy"


def _db_identity(cache_dir: Path) -> str:
    """Return a content digest of the exact vulnerability database Trivy will load."""
    database = cache_dir / "db" / "trivy.db"
    if not database.is_file():
        raise TrivyError(f"Trivy database does not exist at {database}; download it before running a pinned scan")
    digest = hashlib.sha256()
    try:
        with database.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TrivyError(f"Could not hash Trivy database at {database}: {exc}") from exc
    return "sha256:" + digest.hexdigest()


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
        raise TrivyError(f"Could not read Trivy version: {exc}") from exc
    if completed.returncode != 0:
        raise TrivyError(f"Could not read Trivy version: {completed.stderr.strip()}")
    output = completed.stdout.strip()
    if not output:
        raise TrivyError("Trivy returned an empty version")
    first_line = output.splitlines()[0].strip()
    if first_line.lower().startswith("version:"):
        first_line = first_line.partition(":")[2].strip()
    if not first_line:
        raise TrivyError("Trivy returned an empty version")
    return first_line


def _build_command(binary: str, target: Path, report_path: Path, cache_dir: Path) -> list[str]:
    command = [
        binary,
        "filesystem",
        "--scanners",
        "vuln",
        "--skip-db-update",
        "--format",
        "sarif",
        "--output",
        str(report_path),
    ]
    command.extend(["--cache-dir", str(cache_dir)])
    command.append(str(target))
    return command


def _invocation_argv_hash(tool_version: str, db_identity: str) -> str:
    semantic_invocation = {
        "command": "filesystem",
        "db_identity": db_identity,
        "report_format": "sarif",
        "scanners": ["vuln"],
        "skip_db_update": True,
        "tool": TRIVY_REGISTRY_NAME,
        "tool_version": tool_version,
    }
    canonical = json.dumps(semantic_invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def parse_trivy_sarif(report: str | bytes, *, target_root: Path | None = None) -> list[Finding]:
    """Parse Trivy SARIF into findings whose identity excludes source coordinates."""
    try:
        raw = json.loads(report)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid Trivy SARIF JSON: {exc}") from exc

    root = _mapping(raw, "SARIF root")
    if root.get("version") != "2.1.0":
        raise ValueError("Trivy report must use SARIF 2.1.0")

    findings: list[Finding] = []
    for run_index, run_raw in enumerate(_sequence(root.get("runs"), "runs")):
        run = _mapping(run_raw, f"runs[{run_index}]")
        driver = _mapping(_mapping(run.get("tool"), "tool").get("driver"), "tool.driver")
        if str(driver.get("name") or "").lower() != "trivy":
            raise ValueError("SARIF tool.driver.name must be 'Trivy'")
        rules = _rules_by_id(driver)

        for result_index, result_raw in enumerate(_sequence(run.get("results", []), "results")):
            result = _mapping(result_raw, f"results[{result_index}]")
            rule_id = str(result.get("ruleId") or "")
            if not rule_id:
                raise ValueError(f"results[{result_index}] is missing ruleId")
            path = _result_path(result, result_index, target_root)
            message = str(_mapping(result.get("message"), "message").get("text") or "").strip()
            rule = rules.get(rule_id, {})
            summary = message or _rule_summary(rule, rule_id)
            findings.append(
                Finding(
                    rule=rule_id,
                    path=path,
                    severity=_severity(result, rule),
                    summary=summary,
                )
            )
    return findings


def _rules_by_id(driver: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for raw_rule in _sequence(driver.get("rules", []), "tool.driver.rules"):
        rule = _mapping(raw_rule, "tool.driver.rules entry")
        rule_id = str(rule.get("id") or "")
        if rule_id:
            rules[rule_id] = rule
    return rules


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
    return _normalize_path(uri, target_root)


def _normalize_path(uri: str, target_root: Path | None) -> str:
    parsed = urlparse(uri)
    path = unquote(parsed.path) if parsed.scheme == "file" else unquote(uri)
    candidate = Path(path.replace("\\", "/"))
    if target_root is not None and candidate.is_absolute():
        root = target_root if target_root.is_dir() or not target_root.exists() else target_root.parent
        with suppress(ValueError):
            candidate = candidate.relative_to(root.resolve())
    return candidate.as_posix()


def _rule_summary(rule: dict[str, Any], fallback: str) -> str:
    short = _mapping(rule.get("shortDescription", {}), "rule.shortDescription")
    return str(short.get("text") or fallback)


def _severity(result: dict[str, Any], rule: dict[str, Any]) -> str:
    properties = _mapping(rule.get("properties", {}), "rule.properties")
    tags = [str(tag).lower() for tag in _sequence(properties.get("tags", []), "rule.properties.tags")]
    for value in ("critical", "high", "medium", "low", "informational"):
        if value in tags:
            return value

    score = properties.get("security-severity")
    with suppress(ValueError):
        numeric = float(str(score))
        if numeric >= 9:
            return "critical"
        if numeric >= 7:
            return "high"
        if numeric >= 4:
            return "medium"
        if numeric > 0:
            return "low"

    return {"error": "high", "warning": "medium", "note": "low", "none": "informational"}.get(
        str(result.get("level") or "none").lower(), "informational"
    )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast("dict[str, Any]", value)


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast("list[Any]", value)


register_scanner_capabilities(
    TRIVY_REGISTRY_NAME,
    output_format=ScannerOutputFormat.SARIF,
    determinism=ScannerDeterminism.FEED_PINNED,
    pinned_inputs=("trivy_db",),
    category=ContractScannerCategory.SCA,
)
