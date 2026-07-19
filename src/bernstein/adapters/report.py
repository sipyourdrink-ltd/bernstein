"""Adapter conformance + capability report.

Produces a snapshot of every CLI adapter Bernstein knows about:

* whether the upstream binary resolves on ``PATH``
* a captured ``<binary> --version`` string (best-effort, 5s timeout)
* the contract's declared capability surface (flags + subcommands)
* an in-process conformance verdict: ``ok`` / ``fail`` / ``skip``
* module mtime + contract sha256 for cache-busting in dashboards

The module is consumed by ``bernstein adapters check`` (Rich + JSON
surfaces) and is intentionally side-effect free apart from running
``shutil.which`` and a single ``<binary> --version`` subprocess per
adapter. No pytest subprocess is spawned.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bernstein.adapters._contract import (
    CONTRACTS_DIR,
    DEFAULT_ADAPTER_STRATEGY,
    ContractSpec,
    StrategyView,
    _capability_failures,  # pyright: ignore[reportPrivateUsage]
    _strip_ansi,  # pyright: ignore[reportPrivateUsage]
    probe_failure_reason,
    strategy_for,
)

if TYPE_CHECKING:
    from bernstein.adapters.base import CLIAdapter

# Per-adapter binary overrides where the registry key differs from the
# CLI binary on PATH. Mirrors the table in
# ``bernstein.cli.commands.adapter_cmd._BINARY_OVERRIDES`` so the report
# stays consistent with ``bernstein adapters list``.
_BINARY_OVERRIDES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "devin_terminal": "devin",
    "q_dev": "q",
    "open_interpreter": "interpreter",
    "openai_agents": "python",
    "cloudflare": "wrangler",
    "letta_code": "letta",
    "continue": "continue",
    "openhands": "openhands",
    "mock": "",
    "generic": "",
    "composio": "ao",
    "devin": "devin",
}

# Conformance verdict values.
CONFORMANCE_OK: Literal["ok"] = "ok"
CONFORMANCE_FAIL: Literal["fail"] = "fail"
CONFORMANCE_SKIP: Literal["skip"] = "skip"

ConformanceVerdict = Literal["ok", "fail", "skip"]

# Hard ceiling for ``<binary> --version`` capture.
_VERSION_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class AdapterStatus:
    """One row in the adapter conformance + capability report.

    Args:
        name: Registry key (e.g. ``claude``, ``codex``).
        module_path: Repo-relative source path of the adapter module.
        binary_resolved: Absolute path on ``PATH``, or ``None`` if missing
            or the adapter has no binary (mock, generic, openai_agents).
        version_string: Trimmed first line of ``<binary> --version``, or
            ``None`` when the call failed / timed out / binary is missing.
        capabilities: Frozenset of flags + subcommands the contract
            advertises as the required surface. Empty when no contract.
        conformance: Verdict - ``ok`` / ``fail`` / ``skip``.
        conformance_detail: Human-readable reason. Empty on ``ok``.
        last_modified_utc: ISO-8601 UTC timestamp of the adapter module
            file's mtime. Empty when the module path cannot be resolved.
        contract_hash: SHA-256 of the loaded contract bytes, or empty
            string when the adapter has no contract on disk.
        strategy: The adapter's declared resume / dangerous-mode /
            event-channel strategy as a ``{axis: value}`` dict so operators
            can compare adapters at a glance (issue #1627 AC #4).
    """

    name: str
    module_path: str
    binary_resolved: str | None
    version_string: str | None
    capabilities: frozenset[str]
    conformance: ConformanceVerdict
    conformance_detail: str
    last_modified_utc: str
    contract_hash: str
    strategy: StrategyView = field(default_factory=DEFAULT_ADAPTER_STRATEGY.to_dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict.

        ``capabilities`` is emitted as a sorted list so the JSON output is
        deterministic across runs.
        """
        data = asdict(self)
        data["capabilities"] = sorted(self.capabilities)
        return data


@dataclass(frozen=True)
class ReportSummary:
    """Aggregate counts for the adapter report footer.

    Args:
        total: Number of adapters in the report.
        reachable: How many adapters had a resolvable binary on ``PATH``.
        conform: How many adapters earned a ``conformance == "ok"`` row.
        fail: How many adapters earned a ``conformance == "fail"`` row.
        skip: How many adapters earned a ``conformance == "skip"`` row.
    """

    total: int
    reachable: int
    conform: int
    fail: int
    skip: int

    def to_dict(self) -> dict[str, int]:
        """Serialize to a JSON-friendly dict."""
        return asdict(self)


@dataclass(frozen=True)
class AdapterReport:
    """Full report payload returned by :func:`build_report`.

    Args:
        adapters: One :class:`AdapterStatus` per adapter, sorted by name.
        summary: Aggregate counts.
    """

    adapters: tuple[AdapterStatus, ...] = field(default_factory=tuple)
    summary: ReportSummary = field(default_factory=lambda: ReportSummary(0, 0, 0, 0, 0))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict."""
        return {
            "adapters": [s.to_dict() for s in self.adapters],
            "summary": self.summary.to_dict(),
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to a JSON string (sorted, deterministic)."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _binary_for_adapter(name: str) -> str:
    """Map a registry key to its expected CLI binary name.

    Explicit overrides win, then the adapter's capability profile (which
    already declares its binary), then the registry key itself. Sourcing
    the binary from the declaration keeps profiled adapters out of the
    hand-maintained override table, where a second copy of the same fact
    could drift from the adapter it describes.
    """
    override = _BINARY_OVERRIDES.get(name)
    if override is not None:
        return override
    from bernstein.adapters.capability_profile import profile_binary_for

    return profile_binary_for(name) or name


def _resolve_module_path(adapter: type[CLIAdapter] | CLIAdapter) -> str:
    """Repo-relative module path for an adapter (best-effort)."""
    try:
        target = adapter if inspect.isclass(adapter) else type(adapter)
        src = inspect.getsourcefile(target) or inspect.getfile(target)
    except (TypeError, OSError):
        return "<unknown>"
    if not src:
        return "<unknown>"
    path = Path(src).resolve()
    parts = path.parts
    if "bernstein" in parts:
        idx = parts.index("bernstein")
        return str(Path(*parts[idx:]))
    return path.name


def _module_mtime_utc(adapter: type[CLIAdapter] | CLIAdapter) -> str:
    """ISO-8601 UTC mtime of the adapter module, or ``""`` on error."""
    try:
        target = adapter if inspect.isclass(adapter) else type(adapter)
        src = inspect.getsourcefile(target) or inspect.getfile(target)
        if not src:
            return ""
        from datetime import UTC, datetime

        ts = Path(src).stat().st_mtime
        return datetime.fromtimestamp(ts, tz=UTC).isoformat()
    except (TypeError, OSError):
        return ""


def _contract_hash(name: str, contracts_dir: Path | None = None) -> str:
    """Return ``sha256`` of the contract YAML, or ``""`` when absent."""
    base = contracts_dir if contracts_dir is not None else CONTRACTS_DIR
    path = base / f"{name}.yaml"
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _contract_capabilities(spec: ContractSpec) -> frozenset[str]:
    """All flags + subcommands a contract declares as required."""
    return frozenset(tuple(spec.required_flags) + tuple(spec.required_subcommands))


def _capture_version(binary: str, *, timeout: int = _VERSION_TIMEOUT_SECONDS) -> str | None:
    """Run ``<binary> --version`` with a hard timeout. Never raises.

    Returns the first non-empty trimmed line of stdout+stderr, or
    ``None`` when the call fails for any reason. ``--version`` is the
    universally supported flag; the few CLIs that prefer ``version``
    (no dashes) still respond to ``--version`` with the usage banner,
    which is good enough to populate a "saw something" column.
    """
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not combined:
        return None
    first_line = _strip_ansi(combined).splitlines()[0].strip()
    return first_line or None


# ---------------------------------------------------------------------------
# In-process conformance check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceVerdictPayload:
    """In-process conformance verdict for one adapter.

    Args:
        verdict: ``ok`` / ``fail`` / ``skip``.
        detail: Human-readable reason; empty on ``ok``.
        capabilities: Capability set the verdict was computed against.
    """

    verdict: ConformanceVerdict
    detail: str
    capabilities: frozenset[str]


def check_adapter_in_process(
    name: str,
    *,
    binary_resolved: str | None,
    contracts_dir: Path | None = None,
) -> ConformanceVerdictPayload:
    """Run a lightweight, in-process conformance check for one adapter.

    The check is deliberately simple and never spawns pytest:

    * If no contract YAML exists, the verdict is ``skip`` with detail
      ``"no contract"``. The adapter is not declared as conformance-
      tracked and gets a benign pass-through.
    * If a contract exists but the binary did not resolve, the verdict
      is ``skip`` with detail ``"binary missing"``. Capabilities are
      still surfaced for the operator.
    * If the binary resolved, ``<binary> --help`` is invoked with a 5s
      timeout and the captured text is matched against the contract's
      required flags + subcommands. Any miss is a ``fail``; an all-pass
      is ``ok``. ``--help`` failing to launch is a ``skip``.

    Args:
        name: Adapter registry key.
        binary_resolved: Absolute path returned by ``shutil.which`` for
            the adapter's binary, or ``None`` when missing.
        contracts_dir: Override for the contracts directory (used by
            unit tests). Defaults to the packaged contracts dir.

    Returns:
        A populated :class:`ConformanceVerdictPayload`.
    """
    base = contracts_dir if contracts_dir is not None else CONTRACTS_DIR
    contract_path = base / f"{name}.yaml"
    if not contract_path.exists():
        return ConformanceVerdictPayload(
            verdict=CONFORMANCE_SKIP,
            detail="no contract",
            capabilities=frozenset(),
        )

    try:
        spec = ContractSpec.load(name, contracts_dir=base)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return ConformanceVerdictPayload(
            verdict=CONFORMANCE_SKIP,
            detail=f"contract load failed: {exc}",
            capabilities=frozenset(),
        )

    capabilities = _contract_capabilities(spec)

    if not binary_resolved:
        return ConformanceVerdictPayload(
            verdict=CONFORMANCE_SKIP,
            detail="binary missing",
            capabilities=capabilities,
        )

    # Adapter declared a contract but the binary is on disk - assert
    # the help text still advertises every required token.
    help_cmd = spec.resolved_help_command()
    try:
        proc = subprocess.run(
            help_cmd,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        return ConformanceVerdictPayload(
            verdict=CONFORMANCE_SKIP,
            detail=f"--help failed: {exc}",
            capabilities=capabilities,
        )
    except subprocess.TimeoutExpired:
        return ConformanceVerdictPayload(
            verdict=CONFORMANCE_SKIP,
            detail="--help timed out",
            capabilities=capabilities,
        )

    help_text = (proc.stdout or "") + (proc.stderr or "")

    # A binary that is installed and answers --help but advertises none of
    # its required tokens (or nothing at all) is a broken/redesigned probe
    # surface, not genuine per-flag drift: an installed adapter cannot
    # legitimately drop its entire required surface in one release. Record it
    # as an inconclusive skip so the canary flags it for investigation
    # instead of paging with a misleading "every flag removed" regression
    # (issue #2488). Partial misses still fall through to a fail below.
    probe_reason = probe_failure_reason(spec, help_text)
    if probe_reason is not None:
        total_required = len(spec.required_flags) + len(spec.required_subcommands)
        return ConformanceVerdictPayload(
            verdict=CONFORMANCE_SKIP,
            detail=(
                f"probe inconclusive ({probe_reason}): `{' '.join(help_cmd)}` advertised none of "
                f"the {total_required} required tokens; not treated as drift"
            ),
            capabilities=capabilities,
        )

    failures = _capability_failures(spec, help_text)
    if failures:
        return ConformanceVerdictPayload(
            verdict=CONFORMANCE_FAIL,
            detail="; ".join(failures),
            capabilities=capabilities,
        )

    return ConformanceVerdictPayload(
        verdict=CONFORMANCE_OK,
        detail="",
        capabilities=capabilities,
    )


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _summarize(rows: tuple[AdapterStatus, ...]) -> ReportSummary:
    reachable = sum(1 for r in rows if r.binary_resolved)
    conform = sum(1 for r in rows if r.conformance == CONFORMANCE_OK)
    fail = sum(1 for r in rows if r.conformance == CONFORMANCE_FAIL)
    skip = sum(1 for r in rows if r.conformance == CONFORMANCE_SKIP)
    return ReportSummary(
        total=len(rows),
        reachable=reachable,
        conform=conform,
        fail=fail,
        skip=skip,
    )


def _status_for_one(
    name: str,
    adapter: type[CLIAdapter] | CLIAdapter,
    *,
    contracts_dir: Path | None = None,
    capture_version: bool = True,
) -> AdapterStatus:
    """Compute :class:`AdapterStatus` for a single registry entry."""
    binary_name = _binary_for_adapter(name)
    binary_resolved = shutil.which(binary_name) if binary_name else None
    version = _capture_version(binary_name) if capture_version and binary_resolved else None
    verdict = check_adapter_in_process(name, binary_resolved=binary_resolved, contracts_dir=contracts_dir)
    return AdapterStatus(
        name=name,
        module_path=_resolve_module_path(adapter),
        binary_resolved=binary_resolved,
        version_string=version,
        capabilities=verdict.capabilities,
        conformance=verdict.verdict,
        conformance_detail=verdict.detail,
        last_modified_utc=_module_mtime_utc(adapter),
        contract_hash=_contract_hash(name, contracts_dir=contracts_dir),
        strategy=strategy_for(name).to_dict(),
    )


def build_report(
    *,
    contracts_dir: Path | None = None,
    capture_version: bool = True,
    only: str | None = None,
) -> AdapterReport:
    """Snapshot every registered adapter into an :class:`AdapterReport`.

    Args:
        contracts_dir: Override the contract directory (tests use this).
        capture_version: Set to ``False`` in tests to bypass real
            ``<binary> --version`` subprocesses.
        only: When set, restrict the report to a single adapter name
            (raises :class:`KeyError` if absent from the registry).

    Returns:
        Fully populated :class:`AdapterReport` sorted by adapter name.
    """
    # Lazy import so tests can patch ``bernstein.adapters.registry``.
    from bernstein.adapters.registry import iter_adapter_specs

    rows_list: list[AdapterStatus] = []
    found_only = False
    for name, adapter in iter_adapter_specs():
        if only is not None and name != only:
            continue
        if only is not None:
            found_only = True
        rows_list.append(
            _status_for_one(
                name,
                adapter,
                contracts_dir=contracts_dir,
                capture_version=capture_version,
            )
        )

    if only is not None and not found_only:
        raise KeyError(only)

    rows_list.sort(key=lambda r: r.name)
    rows = tuple(rows_list)
    return AdapterReport(adapters=rows, summary=_summarize(rows))


__all__ = [
    "CONFORMANCE_FAIL",
    "CONFORMANCE_OK",
    "CONFORMANCE_SKIP",
    "AdapterReport",
    "AdapterStatus",
    "ConformanceVerdict",
    "ConformanceVerdictPayload",
    "ReportSummary",
    "build_report",
    "check_adapter_in_process",
]
