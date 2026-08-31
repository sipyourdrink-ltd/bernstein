"""Deterministic Gitleaks scanner adapter and SARIF normalization."""

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

GITLEAKS_REGISTRY_NAME = "gitleaks"
_SCAN_TIMEOUT_SECONDS = 300


class GitleaksError(RuntimeError):
    """Base error raised when Gitleaks cannot complete a scan."""


class GitleaksNotInstalledError(GitleaksError):
    """Raised when the Gitleaks executable cannot be found on ``PATH``."""


@dataclass(frozen=True)
class GitleaksInvocation:
    """Stable provenance for one Gitleaks invocation."""

    tool_version: str
    ruleset_digest: str
    argv_hash: str


class GitleaksAdapter(ScannerAdapter):
    """Run Gitleaks directory scans and normalize their SARIF findings."""

    registry_name = GITLEAKS_REGISTRY_NAME
    output_format = OutputFormat.SARIF
    determinism = DeterminismTier.DETERMINISTIC
    pinned_inputs: tuple[str, ...] = ()
    category = ScannerCategory.SECRET

    def __init__(self, *, binary: str = "gitleaks", config_path: str | Path | None = None) -> None:
        super().__init__()
        self._binary = binary
        self._config_path = Path(config_path) if config_path is not None else None
        self.last_invocation: GitleaksInvocation | None = None

    def name(self) -> str:
        """Return the registry key used by the conformance capability lookup."""
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run a deterministic Gitleaks directory scan.

        Gitleaks exit code 1 means findings were detected, not that execution
        failed. Both exit codes 0 and 1 therefore produce a ``ScanResult``.
        """
        self.enforce_network_policy()
        _validate_scope(target, scope)
        resolved_target = target.resolve()
        if not resolved_target.exists():
            raise GitleaksError(f"Gitleaks target does not exist: {target}")
        binary = shutil.which(self._binary)
        if binary is None:
            raise GitleaksNotInstalledError(
                f"Gitleaks executable {self._binary!r} was not found on PATH; install gitleaks or configure its binary"
            )

        config_path = self._resolve_config_path(scope, resolved_target)
        ignore_root = resolved_target if resolved_target.is_dir() else resolved_target.parent
        ignore_file = ignore_root / ".gitleaksignore"
        tool_version = _read_version(binary)
        ruleset_digest = _ruleset_digest(binary, config_path, ignore_file if ignore_file.is_file() else None)

        workdir.mkdir(parents=True, exist_ok=True)
        report_path = (workdir / "gitleaks.sarif").resolve()
        report_path.unlink(missing_ok=True)
        command = _build_command(binary, resolved_target, report_path, config_path, ignore_root)
        self.last_invocation = GitleaksInvocation(
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
            raise GitleaksError(f"Gitleaks execution failed: {exc}") from exc

        if completed.returncode not in (0, 1):
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise GitleaksError(f"Gitleaks exited with code {completed.returncode}: {detail}")
        if not report_path.is_file():
            raise GitleaksError("Gitleaks completed without writing its SARIF report")

        try:
            findings = parse_gitleaks_sarif(report_path.read_bytes(), target_root=resolved_target)
        finally:
            # SARIF snippets may contain secrets. Findings retain only a digest,
            # so the raw report should not outlive parsing.
            report_path.unlink(missing_ok=True)
        return ScanResult(findings=findings)

    def _resolve_config_path(self, scope: ScanScope, target: Path) -> Path | None:
        configured = scope.config.get("config_path")
        config_path = Path(str(configured)) if configured is not None else self._config_path
        if config_path is None:
            config_root = target if target.is_dir() else target.parent
            target_config = config_root / ".gitleaks.toml"
            config_path = target_config if target_config.is_file() else None
        if config_path is not None and not config_path.is_file():
            raise GitleaksError(f"Gitleaks config does not exist: {config_path}")
        return config_path.resolve() if config_path is not None else None


def _validate_scope(target: Path, scope: ScanScope) -> None:
    if scope.include or scope.exclude or scope.max_depth is not None:
        raise ValueError("GitleaksAdapter does not yet support include, exclude, or max_depth scan scope fields")
    unsupported = set(scope.config) - {"config_path"}
    if unsupported:
        raise ValueError(f"Unsupported Gitleaks scan configuration: {', '.join(sorted(unsupported))}")
    if scope.roots:
        resolved_target = target.resolve()
        target_is_allowed = any(
            resolved_target == root.resolve() or resolved_target.is_relative_to(root.resolve()) for root in scope.roots
        )
        if not target_is_allowed:
            raise ValueError("Gitleaks target is outside the allowed ScanScope roots")


def _read_version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "version"],
            check=False,
            capture_output=True,
            text=True,
            env=build_filtered_env([]),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitleaksError(f"Could not read Gitleaks version: {exc}") from exc
    if completed.returncode != 0:
        raise GitleaksError(f"Could not read Gitleaks version: {completed.stderr.strip()}")
    version = completed.stdout.strip()
    if not version:
        raise GitleaksError("Gitleaks returned an empty version")
    return version


def _build_command(
    binary: str,
    target: Path,
    report_path: Path,
    config_path: Path | None,
    ignore_root: Path,
) -> list[str]:
    command = [
        binary,
        "dir",
        "--no-banner",
        "--report-format",
        "sarif",
        "--report-path",
        str(report_path),
        "--gitleaks-ignore-path",
        str(ignore_root),
    ]
    if config_path is not None:
        command.extend(["--config", str(config_path)])
    command.append(str(target))
    return command


def _ruleset_digest(binary: str, config_path: Path | None, ignore_path: Path | None = None) -> str:
    source = config_path if config_path is not None else Path(binary)
    source_bytes = source.read_bytes()
    if ignore_path is None:
        digest_bytes = source_bytes
    else:
        digest_bytes = b"gitleaks-policy-v1\0" + source_bytes + b"\0ignore\0" + ignore_path.read_bytes()
    return "sha256:" + hashlib.sha256(digest_bytes).hexdigest()


def _invocation_argv_hash(tool_version: str, ruleset_digest: str) -> str:
    semantic_invocation = {
        "command": "dir",
        "report_format": "sarif",
        "ruleset_digest": ruleset_digest,
        "tool": GITLEAKS_REGISTRY_NAME,
        "tool_version": tool_version,
    }
    canonical = json.dumps(semantic_invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def parse_gitleaks_sarif(report: str | bytes, *, target_root: Path | None = None) -> list[Finding]:
    """Parse a Gitleaks SARIF report into stable Bernstein findings.

    Source coordinates and Gitleaks fingerprints are deliberately excluded
    from the finding. Gitleaks fingerprints contain the start line, so using
    them would make a cosmetic line shift change the finding hash.

    Args:
        report: UTF-8 SARIF JSON emitted by Gitleaks.
        target_root: Scan root used to make absolute Gitleaks paths portable.

    Returns:
        Findings in report order.

    Raises:
        ValueError: If the report is not valid Gitleaks SARIF.
    """
    try:
        raw = json.loads(report)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid Gitleaks SARIF JSON: {exc}") from exc

    root = _mapping(raw, "SARIF root")
    if root.get("version") != "2.1.0":
        raise ValueError("Gitleaks report must use SARIF 2.1.0")

    findings: list[Finding] = []
    for run_index, run_raw in enumerate(_sequence(root.get("runs"), "runs")):
        run = _mapping(run_raw, f"runs[{run_index}]")
        driver = _mapping(_mapping(run.get("tool"), "tool").get("driver"), "tool.driver")
        if driver.get("name") != "gitleaks":
            raise ValueError("SARIF tool.driver.name must be 'gitleaks'")

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

            findings.append(
                Finding(
                    rule=rule,
                    path=normalized_path,
                    severity="informational",
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
    GITLEAKS_REGISTRY_NAME,
    output_format=ScannerOutputFormat.SARIF,
    determinism=ScannerDeterminism.DETERMINISTIC,
    pinned_inputs=(),
    category=ContractScannerCategory.SECRET,
)
