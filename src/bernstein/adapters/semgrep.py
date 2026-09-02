"""Deterministic Semgrep scanner adapter and SARIF normalization."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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

SEMGREP_REGISTRY_NAME = "semgrep"
_SCAN_TIMEOUT_SECONDS = 600

#: SARIF ``level`` -> Bernstein finding severity. Semgrep's own rule severity
#: (ERROR/WARNING/INFO) is surfaced through SARIF as ``level``.
_LEVEL_TO_SEVERITY: dict[str, str] = {
    "error": "high",
    "warning": "medium",
    "note": "informational",
}


class SemgrepError(RuntimeError):
    """Base error raised when Semgrep cannot complete a scan."""


class SemgrepNotInstalledError(SemgrepError):
    """Raised when the Semgrep executable cannot be found on ``PATH``."""


@dataclass(frozen=True)
class SemgrepInvocation:
    """Stable provenance for one Semgrep invocation."""

    tool_version: str
    ruleset_digest: str
    argv_hash: str


class SemgrepAdapter(ScannerAdapter):
    """Run Semgrep scans against a pinned local ruleset and normalize SARIF findings.

    Semgrep is only deterministic when it cannot resolve rules from its remote
    registry (``--config auto`` or a ``p/...`` registry id pulls a ruleset that
    can change between runs). This adapter therefore requires an explicit local
    rule file or directory and refuses to scan without one, rather than
    silently falling back to a network-resolved ruleset.
    """

    registry_name = SEMGREP_REGISTRY_NAME
    output_format = OutputFormat.SARIF
    determinism = DeterminismTier.DETERMINISTIC
    pinned_inputs: tuple[str, ...] = ()
    category = ScannerCategory.SAST

    def __init__(self, *, binary: str = "semgrep", config_path: str | Path | None = None) -> None:
        super().__init__()
        self._binary = binary
        self._config_path = Path(config_path) if config_path is not None else None
        self.last_invocation: SemgrepInvocation | None = None

    def name(self) -> str:
        """Return the registry key used by the conformance capability lookup."""
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run a deterministic Semgrep scan against a pinned local ruleset.

        Semgrep exits 0 on a normal scan regardless of whether findings were
        produced (this adapter never passes ``--error``, which is the only
        thing that turns findings into a nonzero exit code), so any nonzero
        exit code here means the run itself failed.
        """
        self.enforce_network_policy()
        _validate_scope(target, scope)
        resolved_target = target.resolve()
        if not resolved_target.exists():
            raise SemgrepError(f"Semgrep target does not exist: {target}")
        binary = shutil.which(self._binary)
        if binary is None:
            raise SemgrepNotInstalledError(
                f"Semgrep executable {self._binary!r} was not found on PATH; install semgrep or configure its binary"
            )

        config_path = self._resolve_config_path(scope, resolved_target)
        tool_version = _read_version(binary)
        ruleset_digest = _ruleset_digest(config_path)

        workdir.mkdir(parents=True, exist_ok=True)
        report_path = (workdir / "semgrep.sarif").resolve()
        report_path.unlink(missing_ok=True)
        command = _build_command(binary, resolved_target, report_path, config_path)
        self.last_invocation = SemgrepInvocation(
            tool_version=tool_version,
            ruleset_digest=ruleset_digest,
            argv_hash=_invocation_argv_hash(tool_version, ruleset_digest),
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
            raise SemgrepError(f"Semgrep execution failed: {exc}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise SemgrepError(f"Semgrep exited with code {completed.returncode}: {detail}")
        if not report_path.is_file():
            raise SemgrepError("Semgrep completed without writing its SARIF report")

        findings = parse_semgrep_sarif(report_path.read_bytes(), target_root=resolved_target)
        return ScanResult(findings=findings)

    def _resolve_config_path(self, scope: ScanScope, target: Path) -> Path:
        configured = scope.config.get("config_path")
        config_path = Path(str(configured)) if configured is not None else self._config_path
        if config_path is None:
            config_root = target if target.is_dir() else target.parent
            yaml_config = config_root / ".semgrep.yml"
            dir_config = config_root / ".semgrep"
            if yaml_config.is_file():
                config_path = yaml_config
            elif dir_config.is_dir():
                config_path = dir_config
            else:
                raise SemgrepError(
                    "SemgrepAdapter requires a local rule file or directory (config_path); "
                    "remote rule resolution (registry lookups such as 'auto' or 'p/...') is not deterministic"
                )
        if not config_path.exists():
            raise SemgrepError(f"Semgrep config does not exist: {config_path}")
        return config_path.resolve()


def _validate_scope(target: Path, scope: ScanScope) -> None:
    if scope.include or scope.exclude or scope.max_depth is not None:
        raise ValueError("SemgrepAdapter does not yet support include, exclude, or max_depth scan scope fields")
    unsupported = set(scope.config) - {"config_path"}
    if unsupported:
        raise ValueError(f"Unsupported Semgrep scan configuration: {', '.join(sorted(unsupported))}")
    if scope.roots:
        resolved_target = target.resolve()
        target_is_allowed = any(
            resolved_target == root.resolve() or resolved_target.is_relative_to(root.resolve()) for root in scope.roots
        )
        if not target_is_allowed:
            raise ValueError("Semgrep target is outside the allowed ScanScope roots")


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
        raise SemgrepError(f"Could not read Semgrep version: {exc}") from exc
    if completed.returncode != 0:
        raise SemgrepError(f"Could not read Semgrep version: {completed.stderr.strip()}")
    version = completed.stdout.strip()
    if not version:
        raise SemgrepError("Semgrep returned an empty version")
    return version


def _build_command(binary: str, target: Path, report_path: Path, config_path: Path) -> list[str]:
    return [
        binary,
        "scan",
        "--metrics=off",
        "--disable-version-check",
        "--config",
        str(config_path),
        "--sarif",
        "--output",
        str(report_path),
        str(target),
    ]


def _ruleset_digest(config_path: Path) -> str:
    hasher = hashlib.sha256()
    if config_path.is_dir():
        hasher.update(b"semgrep-ruleset-dir-v1\0")
        for rule_file in sorted(p for p in config_path.rglob("*") if p.is_file()):
            hasher.update(str(rule_file.relative_to(config_path)).encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(rule_file.read_bytes())
            hasher.update(b"\0")
    else:
        hasher.update(b"semgrep-ruleset-file-v1\0")
        hasher.update(config_path.read_bytes())
    return "sha256:" + hasher.hexdigest()


def _invocation_argv_hash(tool_version: str, ruleset_digest: str) -> str:
    semantic_invocation = {
        "command": "scan",
        "report_format": "sarif",
        "ruleset_digest": ruleset_digest,
        "tool": SEMGREP_REGISTRY_NAME,
        "tool_version": tool_version,
    }
    canonical = json.dumps(semantic_invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def parse_semgrep_sarif(report: str | bytes, *, target_root: Path | None = None) -> list[Finding]:
    """Parse a Semgrep SARIF report into stable Bernstein findings.

    Source coordinates are deliberately excluded from the finding identity.
    Only ``ruleId`` + normalised path + a hash of the matched snippet feed the
    hash, so a cosmetic line shift elsewhere in the file does not change it.

    Args:
        report: UTF-8 SARIF JSON emitted by ``semgrep scan --sarif``.
        target_root: Scan root used to make absolute Semgrep paths portable.

    Returns:
        Findings in report order.

    Raises:
        ValueError: If the report is not valid Semgrep SARIF.
    """
    try:
        raw = json.loads(report)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid Semgrep SARIF JSON: {exc}") from exc

    root = _mapping(raw, "SARIF root")
    if root.get("version") != "2.1.0":
        raise ValueError("Semgrep report must use SARIF 2.1.0")

    findings: list[Finding] = []
    for run_index, run_raw in enumerate(_sequence(root.get("runs"), "runs")):
        run = _mapping(run_raw, f"runs[{run_index}]")
        driver = _mapping(_mapping(run.get("tool"), "tool").get("driver"), "tool.driver")
        if driver.get("name") != "semgrep":
            raise ValueError("SARIF tool.driver.name must be 'semgrep'")

        descriptions = _rule_descriptions(driver)
        for result_index, result_raw in enumerate(_sequence(run.get("results", []), "results")):
            result = _mapping(result_raw, f"results[{result_index}]")
            rule = str(result.get("ruleId") or "")
            if not rule:
                raise ValueError(f"results[{result_index}] is missing ruleId")

            physical = _physical_location(result, result_index)
            artifact = _mapping(physical.get("artifactLocation"), "artifactLocation")
            path = str(artifact.get("uri") or "").replace("\\", "/")
            if not path:
                raise ValueError(f"results[{result_index}] is missing artifactLocation.uri")
            normalized_path = _normalize_path(path, target_root)

            region = _mapping(physical.get("region"), "region")
            snippet = str(_mapping(region.get("snippet"), "region.snippet").get("text") or "")
            snippet_hash = "sha256:" + hashlib.sha256(snippet.encode("utf-8")).hexdigest()

            level = str(result.get("level") or "warning")
            severity = _LEVEL_TO_SEVERITY.get(level, "informational")

            findings.append(
                Finding(
                    rule=rule,
                    path=normalized_path,
                    severity=severity,
                    summary=descriptions.get(rule, rule),
                    extra={"snippet_hash": snippet_hash},
                )
            )

    return findings


def _normalize_path(path: str, target_root: Path | None) -> str:
    candidate = Path(path)
    if target_root is not None and candidate.is_absolute():
        with suppress(ValueError):
            candidate = candidate.relative_to(target_root.resolve())
    return candidate.as_posix()


def _rule_descriptions(driver: dict[str, Any]) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for rule_raw in _sequence(driver.get("rules", []), "tool.driver.rules"):
        rule = _mapping(rule_raw, "tool.driver.rules entry")
        rule_id = str(rule.get("id") or "")
        short = _mapping(rule.get("shortDescription", {}), "rule.shortDescription")
        if rule_id:
            descriptions[rule_id] = str(short.get("text") or rule_id)
    return descriptions


def _physical_location(result: dict[str, Any], result_index: int) -> dict[str, Any]:
    locations = _sequence(result.get("locations"), f"results[{result_index}].locations")
    if not locations:
        raise ValueError(f"results[{result_index}] has no location")
    location = _mapping(locations[0], "location")
    return _mapping(location.get("physicalLocation"), "physicalLocation")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast("dict[str, Any]", value)


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast("list[Any]", value)


register_scanner_capabilities(
    SEMGREP_REGISTRY_NAME,
    output_format=ScannerOutputFormat.SARIF,
    determinism=ScannerDeterminism.DETERMINISTIC,
    pinned_inputs=(),
    category=ContractScannerCategory.SAST,
)
