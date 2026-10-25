"""Deterministic Bandit scanner adapter and SARIF normalization."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
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
    normalize_report_path,
)
from bernstein.adapters.scanner_finding import Finding

BANDIT_REGISTRY_NAME = "bandit"
_SCAN_TIMEOUT_SECONDS = 600

#: Bandit's per-issue severity (``properties.issue_severity`` in its SARIF)
#: -> Bernstein finding severity. Read in preference to the SARIF ``level``,
#: which Bandit derives from the same value.
_ISSUE_SEVERITY: dict[str, str] = {
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
    "UNDEFINED": "informational",
}

#: Fallback when a result carries no ``issue_severity``.
_LEVEL_TO_SEVERITY: dict[str, str] = {
    "error": "high",
    "warning": "medium",
    "note": "low",
}


class BanditError(RuntimeError):
    """Base error raised when Bandit cannot complete a scan."""


class BanditNotInstalledError(BanditError):
    """Raised when the Bandit executable cannot be found on ``PATH``."""


@dataclass(frozen=True)
class BanditInvocation:
    """Stable provenance for one Bandit invocation."""

    tool_version: str
    ruleset_digest: str
    argv_hash: str


class BanditAdapter(ScannerAdapter):
    """Run Bandit over Python sources and normalize its SARIF findings.

    Bandit's checks ship inside the package, so a pinned Bandit version pins
    the ruleset. What can still change the result between two runs of the same
    version is configuration: a ``-c`` config file, and any ``.bandit`` ini
    file Bandit finds by walking the target tree (it reads one without being
    asked, and refuses to run when it finds more than one). Both feed the
    recorded ruleset digest, so two runs with different configuration cannot
    claim the same provenance.

    The SARIF formatter is an optional Bandit extra (``bandit[sarif]``). A
    Bandit without it is refused with that remedy rather than read through a
    second output format.
    """

    registry_name = BANDIT_REGISTRY_NAME
    output_format = OutputFormat.SARIF
    determinism = DeterminismTier.DETERMINISTIC
    pinned_inputs: tuple[str, ...] = ()
    category = ScannerCategory.SAST

    def __init__(self, *, binary: str = "bandit", config_path: str | Path | None = None) -> None:
        super().__init__()
        self._binary = binary
        self._config_path = Path(config_path) if config_path is not None else None
        self.last_invocation: BanditInvocation | None = None

    def name(self) -> str:
        """Return the registry key used by the conformance capability lookup."""
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run Bandit recursively over ``target`` and return its findings.

        The adapter passes ``--exit-zero``, so Bandit exits 0 whether or not it
        found anything, and any nonzero exit means the run itself failed.
        """
        self.enforce_network_policy()
        _validate_scope(target, scope)
        resolved_target = target.resolve()
        if not resolved_target.exists():
            raise BanditError(f"Bandit target does not exist: {target}")
        binary = shutil.which(self._binary)
        if binary is None:
            raise BanditNotInstalledError(
                f"Bandit executable {self._binary!r} was not found on PATH; "
                "install bandit[sarif] or configure its binary"
            )

        config_path = self._resolve_config_path(scope)
        tool_version = _read_version(binary)
        ruleset_digest = _ruleset_digest(config_path, _ini_files(resolved_target))

        workdir.mkdir(parents=True, exist_ok=True)
        report_path = (workdir / "bandit.sarif").resolve()
        report_path.unlink(missing_ok=True)
        command = _build_command(binary, resolved_target, report_path, config_path)
        self.last_invocation = BanditInvocation(
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
            raise BanditError(f"Bandit execution failed: {exc}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            if "invalid choice: 'sarif'" in detail:
                raise BanditError("Bandit has no SARIF formatter; install the extra with 'pip install bandit[sarif]'")
            raise BanditError(f"Bandit exited with code {completed.returncode}: {detail}")
        if not report_path.is_file():
            raise BanditError("Bandit completed without writing its SARIF report")

        findings = parse_bandit_sarif(report_path.read_bytes(), target_root=resolved_target)
        return ScanResult(findings=findings, invocation_digest=self.last_invocation.argv_hash)

    def _resolve_config_path(self, scope: ScanScope) -> Path | None:
        configured = scope.config.get("config_path")
        config_path = Path(str(configured)) if configured is not None else self._config_path
        if config_path is None:
            return None
        if not config_path.is_file():
            raise BanditError(f"Bandit config does not exist: {config_path}")
        return config_path.resolve()


def _validate_scope(target: Path, scope: ScanScope) -> None:
    if scope.include or scope.exclude or scope.max_depth is not None:
        raise ValueError("BanditAdapter does not yet support include, exclude, or max_depth scan scope fields")
    unsupported = set(scope.config) - {"config_path"}
    if unsupported:
        raise ValueError(f"Unsupported Bandit scan configuration: {', '.join(sorted(unsupported))}")
    if scope.roots:
        resolved_target = target.resolve()
        target_is_allowed = any(
            resolved_target == root.resolve() or resolved_target.is_relative_to(root.resolve()) for root in scope.roots
        )
        if not target_is_allowed:
            raise ValueError("Bandit target is outside the allowed ScanScope roots")


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
        raise BanditError(f"Could not read Bandit version: {exc}") from exc
    if completed.returncode != 0:
        raise BanditError(f"Could not read Bandit version: {completed.stderr.strip()}")
    # ``bandit --version`` prints "bandit 1.8.6" followed by interpreter lines.
    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    version = first_line.removeprefix("bandit").strip()
    if not version:
        raise BanditError("Bandit returned an empty version")
    return version


def _ini_files(target: Path) -> list[Path]:
    """Return the ``.bandit`` files Bandit would discover under ``target``, sorted."""
    if target.is_file():
        return []
    found = [Path(root) / ".bandit" for root, _dirs, files in os.walk(target) if ".bandit" in files]
    return sorted(found)


def _build_command(binary: str, target: Path, report_path: Path, config_path: Path | None) -> list[str]:
    command = [binary, "--recursive", "--quiet", "--exit-zero", "--format", "sarif", "--output", str(report_path)]
    if config_path is not None:
        command.extend(["--configfile", str(config_path)])
    command.append(str(target))
    return command


def _ruleset_digest(config_path: Path | None, ini_files: list[Path]) -> str:
    """Digest the configuration that can change which checks run.

    With neither a config file nor a ``.bandit`` file, the digest is that of
    the empty configuration: the version alone then pins the ruleset.
    """
    hasher = hashlib.sha256()
    hasher.update(b"bandit-ruleset-v1\0")
    hasher.update(b"config\0")
    if config_path is not None:
        hasher.update(config_path.read_bytes())
    hasher.update(b"\0")
    for ini_file in ini_files:
        hasher.update(b"ini\0")
        hasher.update(ini_file.read_bytes())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def _invocation_argv_hash(tool_version: str, ruleset_digest: str) -> str:
    semantic_invocation = {
        "command": "recursive",
        "report_format": "sarif",
        "ruleset_digest": ruleset_digest,
        "tool": BANDIT_REGISTRY_NAME,
        "tool_version": tool_version,
    }
    canonical = json.dumps(semantic_invocation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def parse_bandit_sarif(report: str | bytes, *, target_root: Path | None = None) -> list[Finding]:
    """Parse a Bandit SARIF report into stable Bernstein findings.

    Source coordinates are deliberately excluded from the finding identity.
    The shared Finding hash includes the rule, normalised path, severity,
    summary and extra metadata (snippet hash and confidence). A cosmetic line
    shift elsewhere in the file does not change it.

    Args:
        report: UTF-8 SARIF JSON emitted by ``bandit --format sarif``.
        target_root: Scan root used to make absolute Bandit paths portable.

    Returns:
        Findings in report order.

    Raises:
        ValueError: If the report is not valid Bandit SARIF.
    """
    try:
        raw = json.loads(report)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid Bandit SARIF JSON: {exc}") from exc

    root = _mapping(raw, "SARIF root")
    if root.get("version") != "2.1.0":
        raise ValueError("Bandit report must use SARIF 2.1.0")

    findings: list[Finding] = []
    for run_index, run_raw in enumerate(_sequence(root.get("runs"), "runs")):
        run = _mapping(run_raw, f"runs[{run_index}]")
        driver = _mapping(_mapping(run.get("tool"), "tool").get("driver"), "tool.driver")
        if driver.get("name") != "Bandit":
            raise ValueError("SARIF tool.driver.name must be 'Bandit'")

        for invocation_raw in _sequence(run.get("invocations", []), "invocations"):
            invocation = _mapping(invocation_raw, "invocation")
            if invocation.get("executionSuccessful") is False:
                raise ValueError("Bandit SARIF reports an unsuccessful execution")

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
            snippet = str(_mapping(region.get("snippet"), "region.snippet").get("text") or "")
            snippet_hash = "sha256:" + hashlib.sha256(snippet.encode("utf-8")).hexdigest()

            properties = _mapping(result.get("properties", {}), f"results[{result_index}].properties")
            issue_severity = str(properties.get("issue_severity") or "").upper()
            severity = _ISSUE_SEVERITY.get(issue_severity) or _LEVEL_TO_SEVERITY.get(
                str(result.get("level") or ""), "informational"
            )
            message = str(_mapping(result.get("message", {}), "message").get("text") or rule)

            findings.append(
                Finding(
                    rule=rule,
                    path=normalized_path,
                    severity=severity,
                    summary=message,
                    extra={
                        "snippet_hash": snippet_hash,
                        "confidence": str(properties.get("issue_confidence") or "").lower(),
                    },
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
    BANDIT_REGISTRY_NAME,
    output_format=ScannerOutputFormat.SARIF,
    determinism=ScannerDeterminism.DETERMINISTIC,
    pinned_inputs=(),
    category=ContractScannerCategory.SAST,
)
