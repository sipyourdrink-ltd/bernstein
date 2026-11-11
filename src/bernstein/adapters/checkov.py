"""Deterministic Checkov IaC scanner adapter and SARIF normalization."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

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

CHECKOV_REGISTRY_NAME = "checkov"
_SCAN_TIMEOUT_SECONDS = 900

#: Name Checkov gives its SARIF file inside ``--output-file-path``.
_REPORT_NAME = "results_sarif.sarif"

#: Config files Checkov reads from the scanned directory without being asked.
_TARGET_CONFIG_NAMES = (".checkov.yaml", ".checkov.yml")
_UNSUPPORTED_CONFIG_KEYS = frozenset({"external-checks-dir", "external-checks-git", "download-external-modules"})

#: SARIF ``level`` -> Bernstein finding severity. Without a Prisma Cloud
#: connection Checkov knows no per-check severity and reports every failed
#: check as ``error``.
_LEVEL_TO_SEVERITY: dict[str, str] = {
    "error": "high",
    "warning": "medium",
    "note": "low",
}


class CheckovError(RuntimeError):
    """Base error raised when Checkov cannot complete a scan."""


class CheckovNotInstalledError(CheckovError):
    """Raised when the Checkov executable cannot be found on ``PATH``."""


@dataclass(frozen=True)
class CheckovInvocation:
    """Stable provenance for one Checkov invocation."""

    tool_version: str
    ruleset_digest: str
    argv_hash: str


class CheckovAdapter(ScannerAdapter):
    """Run Checkov over an infrastructure-as-code directory and normalize its SARIF.

    Checkov's built-in checks ship inside the package, so a pinned Checkov
    version pins the ruleset, provided three things are held still:

    * **No platform download.** ``--skip-download`` stops Checkov fetching
      custom policies and guidelines from Prisma Cloud, which can change
      between runs.
    * **Config the run does not record.** Checkov reads ``.checkov.yaml`` /
      ``.checkov.yml`` from the scanned directory, the working directory and
      the user's home directory. The adapter runs from the target, so those
      two are the same place and both feed the recorded ruleset digest, and
      it points ``HOME`` and ``USERPROFILE`` at a fresh empty directory so an operator's
      personal config cannot change a scan.
    * **No unrecorded checks.** Config keys that load external checks or
      modules are refused, and external module download is explicitly disabled.
    * **Path form.** Checkov writes SARIF URIs relative to the parent of an
      absolute ``--directory``. Scanning ``.`` from inside the target keeps
      them relative to the target itself.
    """

    registry_name = CHECKOV_REGISTRY_NAME
    output_format = OutputFormat.SARIF
    determinism = DeterminismTier.DETERMINISTIC
    pinned_inputs: tuple[str, ...] = ()
    category = ScannerCategory.IAC

    def __init__(self, *, binary: str = "checkov", config_path: str | Path | None = None) -> None:
        super().__init__()
        self._binary = binary
        self._config_path = Path(config_path) if config_path is not None else None
        self.last_invocation: CheckovInvocation | None = None

    def name(self) -> str:
        """Return the registry key used by the conformance capability lookup."""
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run Checkov over the ``target`` directory and return its failed checks.

        The adapter passes ``--soft-fail``, so Checkov exits 0 whether or not a
        check failed, and any nonzero exit means the run itself failed.
        """
        self.enforce_network_policy()
        _validate_scope(target, scope)
        resolved_target = target.resolve()
        if not resolved_target.is_dir():
            raise CheckovError(f"Checkov target must be an existing directory: {target}")
        binary = shutil.which(self._binary)
        if binary is None:
            raise CheckovNotInstalledError(
                f"Checkov executable {self._binary!r} was not found on PATH; install checkov or configure its binary"
            )

        config_path = self._resolve_config_path(scope)
        target_configs = _target_configs(resolved_target)
        _validate_configs(([config_path] if config_path is not None else []) + target_configs)
        workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="checkov-home-", dir=workdir) as home_dir:
            env = build_filtered_env([])
            env["HOME"] = env["USERPROFILE"] = str(Path(home_dir).resolve())

            tool_version = _read_version(binary, env)
            ruleset_digest = _ruleset_digest(config_path, target_configs)

            report_dir = (workdir / "checkov-report").resolve()
            report_dir.mkdir(exist_ok=True)
            report_path = report_dir / _REPORT_NAME
            report_path.unlink(missing_ok=True)
            command = _build_command(binary, report_dir, config_path)
            self.last_invocation = CheckovInvocation(
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
                    cwd=resolved_target,
                    env=env,
                    timeout=_SCAN_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CheckovError(f"Checkov execution failed: {exc}") from exc

            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
                raise CheckovError(f"Checkov exited with code {completed.returncode}: {detail}")
            if not report_path.is_file():
                raise CheckovError("Checkov completed without writing its SARIF report")

            findings = parse_checkov_sarif(report_path.read_bytes(), target_root=resolved_target)
            return ScanResult(findings=findings, invocation_digest=self.last_invocation.argv_hash)

    def _resolve_config_path(self, scope: ScanScope) -> Path | None:
        configured = scope.config.get("config_path")
        config_path = Path(str(configured)) if configured is not None else self._config_path
        if config_path is None:
            return None
        if not config_path.is_file():
            raise CheckovError(f"Checkov config does not exist: {config_path}")
        return config_path.resolve()


def _validate_scope(target: Path, scope: ScanScope) -> None:
    if scope.include or scope.exclude or scope.max_depth is not None:
        raise ValueError("CheckovAdapter does not yet support include, exclude, or max_depth scan scope fields")
    unsupported = set(scope.config) - {"config_path"}
    if unsupported:
        raise ValueError(f"Unsupported Checkov scan configuration: {', '.join(sorted(unsupported))}")
    if scope.roots:
        resolved_target = target.resolve()
        target_is_allowed = any(
            resolved_target == root.resolve() or resolved_target.is_relative_to(root.resolve()) for root in scope.roots
        )
        if not target_is_allowed:
            raise ValueError("Checkov target is outside the allowed ScanScope roots")


def _read_version(binary: str, env: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckovError(f"Could not read Checkov version: {exc}") from exc
    if completed.returncode != 0:
        raise CheckovError(f"Could not read Checkov version: {completed.stderr.strip()}")
    lines = completed.stdout.strip().splitlines()
    version = lines[-1].strip() if lines else ""
    if not version:
        raise CheckovError("Checkov returned an empty version")
    return version


def _target_configs(target: Path) -> list[Path]:
    """Return the config files Checkov reads from the scanned directory, in a fixed order."""
    return [target / name for name in _TARGET_CONFIG_NAMES if (target / name).is_file()]


def _validate_configs(configs: list[Path]) -> None:
    """Refuse configuration that loads checks or modules outside the recorded ruleset."""
    for config in configs:
        try:
            raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise CheckovError(f"Could not read Checkov config {config}: {exc}") from exc
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise CheckovError(f"Checkov config must be a mapping: {config}")
        unsupported = _UNSUPPORTED_CONFIG_KEYS.intersection(raw)
        if unsupported:
            raise CheckovError(f"Unsupported Checkov config keys in {config}: {', '.join(sorted(unsupported))}")


def _build_command(binary: str, report_dir: Path, config_path: Path | None) -> list[str]:
    command = [
        binary,
        "--directory",
        ".",
        "--output",
        "sarif",
        "--output-file-path",
        str(report_dir),
        "--skip-download",
        "--download-external-modules",
        "false",
        "--soft-fail",
        "--quiet",
    ]
    if config_path is not None:
        command.extend(["--config-file", str(config_path)])
    return command


def _ruleset_digest(config_path: Path | None, target_configs: list[Path]) -> str:
    """Digest the configuration that can change which checks run.

    With no config at all, the digest is that of the empty configuration and
    the version alone pins the ruleset.
    """
    hasher = hashlib.sha256()
    hasher.update(b"checkov-ruleset-v1\0")
    hasher.update(b"config\0")
    if config_path is not None:
        hasher.update(config_path.read_bytes())
    hasher.update(b"\0")
    for config in target_configs:
        hasher.update(config.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(config.read_bytes())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def _invocation_argv_hash(tool_version: str, ruleset_digest: str) -> str:
    semantic_invocation = {
        "command": "directory",
        "download_external_modules": False,
        "report_format": "sarif",
        "ruleset_digest": ruleset_digest,
        "skip_download": True,
        "tool": CHECKOV_REGISTRY_NAME,
        "tool_version": tool_version,
    }
    canonical = json.dumps(semantic_invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def parse_checkov_sarif(report: str | bytes, *, target_root: Path | None = None) -> list[Finding]:
    """Parse a Checkov SARIF report into stable Bernstein findings.

    Source coordinates are deliberately excluded from the finding identity.
    The shared Finding hash includes ``ruleId``, normalized path, severity,
    summary and the flagged resource snippet hash. A cosmetic line shift does not
    change it, while the same check failing on two resources yields two.

    Args:
        report: UTF-8 SARIF JSON emitted by ``checkov --output sarif``.
        target_root: Scan root used to make absolute paths portable.

    Returns:
        Findings in report order.

    Raises:
        ValueError: If the report is not valid Checkov SARIF.
    """
    try:
        raw = json.loads(report)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid Checkov SARIF JSON: {exc}") from exc

    root = _mapping(raw, "SARIF root")
    if root.get("version") != "2.1.0":
        raise ValueError("Checkov report must use SARIF 2.1.0")

    findings: list[Finding] = []
    for run_index, run_raw in enumerate(_sequence(root.get("runs"), "runs")):
        run = _mapping(run_raw, f"runs[{run_index}]")
        driver = _mapping(_mapping(run.get("tool"), "tool").get("driver"), "tool.driver")
        if driver.get("name") != "Checkov":
            raise ValueError("SARIF tool.driver.name must be 'Checkov'")

        for result_index, result_raw in enumerate(_sequence(run.get("results", []), "results")):
            result = _mapping(result_raw, f"results[{result_index}]")
            rule = str(result.get("ruleId") or "")
            if not rule:
                raise ValueError(f"results[{result_index}] is missing ruleId")

            physical = _physical_location(result, result_index)
            artifact = _mapping(physical.get("artifactLocation"), "artifactLocation")
            uri = str(artifact.get("uri") or "")
            if not uri:
                raise ValueError(f"results[{result_index}] is missing artifactLocation.uri")
            normalized_path = normalize_report_path(uri, target_root)

            region = _mapping(physical.get("region"), "region")
            snippet = str(_mapping(region.get("snippet", {}), "region.snippet").get("text") or "")
            snippet_hash = "sha256:" + hashlib.sha256(snippet.encode("utf-8")).hexdigest()

            severity = _LEVEL_TO_SEVERITY.get(str(result.get("level") or ""), "informational")
            message = str(_mapping(result.get("message", {}), "message").get("text") or rule)

            findings.append(
                Finding(
                    rule=rule,
                    path=normalized_path,
                    severity=severity,
                    summary=message,
                    extra={"snippet_hash": snippet_hash},
                )
            )

    return findings


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
    CHECKOV_REGISTRY_NAME,
    output_format=ScannerOutputFormat.SARIF,
    determinism=ScannerDeterminism.DETERMINISTIC,
    pinned_inputs=(),
    category=ContractScannerCategory.IAC,
)
