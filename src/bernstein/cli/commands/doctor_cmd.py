"""Comprehensive health-check command for Bernstein.

``bernstein doctor`` comprehensive health checks.

Checks: adapters installed, API keys set, config valid, disk space,
git installed, server reachable, and more.  Delegates to the existing
``status_cmd.doctor`` implementation and adds new checks for disk
space and git availability.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import SERVER_URL

logger = logging.getLogger(__name__)

_TASK_SERVER_LABEL = "Task server"

_CONFIG_FILE_LABEL = "Config file"

# ---------------------------------------------------------------------------
# Health check dataclass
# ---------------------------------------------------------------------------

_CHECK_PASS = "PASS"
_CHECK_FAIL = "FAIL"
_CHECK_WARN = "WARN"


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python_version() -> dict[str, Any]:
    """Check Python version >= 3.12."""
    major, minor = sys.version_info.major, sys.version_info.minor
    ok = (major, minor) >= (3, 12)
    return {
        "name": "Python version",
        "status": _CHECK_PASS if ok else _CHECK_FAIL,
        "detail": f"{major}.{minor}",
        "fix": "Install Python 3.12 or newer" if not ok else "",
    }


def check_adapters_installed() -> list[dict[str, Any]]:
    """Check which CLI adapters are on PATH."""
    results: list[dict[str, Any]] = []
    for name in ("agy", "claude", "codex", "gemini", "qwen", "aider"):
        found = shutil.which(name) is not None
        results.append(
            {
                "name": f"Adapter: {name}",
                "status": _CHECK_PASS if found else _CHECK_WARN,
                "detail": "found in PATH" if found else "not in PATH",
                "fix": f"Install {name} CLI" if not found else "",
            }
        )
    return results


def _probe_adapter_version(name: str) -> str | None:
    """Return the installed version string for an adapter binary, or None.

    Runs ``<name> --version`` and extracts the first dotted-numeric token.
    Best-effort: any launch failure, timeout, or unparseable output yields
    ``None`` (reported as "unknown" rather than a false below-floor warning).
    """
    import re

    exe = shutil.which(name)
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = f"{proc.stdout}\n{proc.stderr}"
    match = re.search(r"\d+(?:\.\d+){1,3}", blob)
    return match.group(0) if match else None


#: Posture verdict labels (kept distinct from the spawn-preflight verdict
#: vocabulary: the doctor snapshots the environment, it does not decide a
#: spawn).
_POSTURE_SATISFIED = "satisfied"
_POSTURE_BELOW_FLOOR = "below_floor"
_POSTURE_UNKNOWN = "unknown_version"

#: Schema version stamped into the version-posture receipt preimage.
_VERSION_POSTURE_SCHEMA_VERSION = 1


def collect_version_posture() -> list[dict[str, Any]]:
    """Structured version posture for each installed tracked adapter (#2515).

    For every adapter with a curated minimum-safe floor that is installed on
    PATH, returns a row binding the installed version, the floor, the advisory
    id, and a floor verdict (``satisfied`` / ``below_floor`` /
    ``unknown_version``). This is the single source the console rows *and* the
    signed posture receipt both project from, so the printed report is a
    faithful projection of the sealed record. Uninstalled adapters are omitted
    so the surface stays quiet.
    """
    from bernstein.adapters.advisories import (
        ADAPTER_MIN_SAFE_VERSIONS,
        check_adapter_version,
    )

    entries: list[dict[str, Any]] = []
    for name, advisory in sorted(ADAPTER_MIN_SAFE_VERSIONS.items()):
        if shutil.which(name) is None:
            continue  # adapter not installed: nothing to report
        version = _probe_adapter_version(name)
        if version is None:
            verdict = _POSTURE_UNKNOWN
        elif check_adapter_version(name, version) is not None:
            verdict = _POSTURE_BELOW_FLOOR
        else:
            verdict = _POSTURE_SATISFIED
        entries.append(
            {
                "adapter": name,
                "installed_version": version,
                "floor": advisory.min_safe_version,
                "advisory_id": advisory.advisory_id,
                "verdict": verdict,
            }
        )
    return entries


def check_adapter_advisories() -> list[dict[str, Any]]:
    """Report a supply-chain version-floor status for each tracked adapter.

    A projection of :func:`collect_version_posture` (the same rows the signed
    posture receipt seals):

    - OK (PASS) when the discovered version meets or exceeds the floor,
    - below-floor (WARN) when it is strictly below the floor,
    - unknown (WARN) when the binary is present but the version cannot be
      determined,
    - not installed is omitted so the surface stays quiet for adapters the
      operator does not run.

    Couples to the adapter conformance contract: a below-floor binary is a
    conformance signal an operator can act on before a worker records a run
    against a version we already know is unsafe.
    """
    from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS

    results: list[dict[str, Any]] = []
    for entry in collect_version_posture():
        name = entry["adapter"]
        version = entry["installed_version"]
        floor = entry["floor"]
        label = f"Adapter version: {name}"
        if entry["verdict"] == _POSTURE_UNKNOWN:
            results.append(
                {
                    "name": label,
                    "status": _CHECK_WARN,
                    "detail": f"installed, version unknown (safe floor {floor})",
                    "fix": f"Verify {name} version is >= {floor}",
                }
            )
        elif entry["verdict"] == _POSTURE_BELOW_FLOOR:
            advisory = ADAPTER_MIN_SAFE_VERSIONS[name]
            results.append(
                {
                    "name": label,
                    "status": _CHECK_WARN,
                    "detail": (f"{version} below safe floor {floor} [{advisory.advisory_id}]: {advisory.note}"),
                    "fix": f"Upgrade {name} to >= {floor}",
                }
            )
        else:
            results.append(
                {
                    "name": label,
                    "status": _CHECK_PASS,
                    "detail": f"{version} >= safe floor {floor}",
                    "fix": "",
                }
            )
    return results


def build_version_posture_receipt(entries: list[dict[str, Any]], *, generated_at: str) -> dict[str, Any]:
    """Bind a version-posture snapshot into a canonical receipt mapping (#2515).

    Determinism: a pure function of the posture entries and the timestamp, so
    an identical installed set and floor map produce a byte-identical receipt
    payload modulo ``generated_at``. The floor map's content hash is pinned so
    a map mutated after the fact is caught at verification.
    """
    from bernstein.adapters.security_floor import floor_map_content_hash

    return {
        "schema_version": _VERSION_POSTURE_SCHEMA_VERSION,
        "kind": "adapter.version_posture",
        "entries": [entry.copy() for entry in entries],
        "floor_map_hash": floor_map_content_hash(),
        "generated_at": generated_at,
    }


def emit_version_posture_receipt(workdir: Path) -> dict[str, Any]:
    """Seal the version posture into a receipt and anchor it in the chain (#2515).

    The console version-posture rows become a projection of this receipt:
    "only floor-satisfying binaries were spawnable in this environment during
    window X" is provable offline from a contiguous chain slice, not merely a
    console print an operator may never run. Best-effort anchoring: a doctor
    run must never fail because the audit chain could not be written.

    Returns:
        ``{"receipt": <dict>, "receipt_sha256": <hex>, "entries": [...],
        "anchored": <bool>}``.
    """
    from datetime import UTC, datetime

    from bernstein.adapters.security_floor import receipt_sha256

    entries = collect_version_posture()
    receipt = build_version_posture_receipt(entries, generated_at=datetime.now(UTC).isoformat())
    sha = receipt_sha256(receipt)
    anchored = False
    try:
        from bernstein.core.security.audit_chain import (
            AuditChainStore,
            record_adapter_version_posture_receipt,
        )

        chain = AuditChainStore(workdir / ".sdd" / "audit")
        record_adapter_version_posture_receipt(
            chain=chain,
            receipt_sha256=sha,
            floor_map_hash=receipt["floor_map_hash"],
            entries=entries,
        )
        anchored = True
    except Exception as exc:  # audit write must never break the doctor run
        logger.warning("Could not anchor adapter.version_posture receipt: %s", type(exc).__name__)
    return {"receipt": receipt, "receipt_sha256": sha, "entries": entries, "anchored": anchored}


def check_canary_last_green() -> list[dict[str, Any]]:
    """Warn when an installed agent version is ahead of its last-green.

    The nightly adapter conformance canary regenerates a per-adapter
    last-green projection (the newest upstream version whose conformance
    receipt passed; see ``docs/adapters/conformance-canary.md``). When a
    locally installed binary is *newer* than last-green, the canary has
    not yet verified that release against the adapter contract, so
    unattended runs are one upstream regression away from failing without
    warning. Rows:

    - PASS when the installed version is at or below last-green,
    - WARN when it is strictly ahead of last-green,
    - adapters whose binary is missing, whose version cannot be probed,
      or which carry no last-green row are omitted so the surface stays
      quiet.
    """
    from packaging.version import InvalidVersion, Version

    from bernstein.adapters.canary import load_last_green

    results: list[dict[str, Any]] = []
    for name, entry in sorted(load_last_green().items()):
        if shutil.which(entry.binary) is None:
            continue  # adapter not installed: nothing to advise on
        version = _probe_adapter_version(entry.binary)
        if version is None:
            continue  # unknown version is already surfaced by advisories
        try:
            installed = Version(version)
            last_green = Version(entry.version)
        except InvalidVersion:
            continue
        label = f"Adapter last-green: {name}"
        if installed > last_green:
            results.append(
                {
                    "name": label,
                    "status": _CHECK_WARN,
                    "detail": (
                        f"{version} is ahead of last-green {entry.version} "
                        f"(receipt {entry.receipt_sha256[:12]}); the conformance "
                        "canary has not verified this release yet"
                    ),
                    "fix": (
                        f"Check docs/adapters/conformance-canary.md, or pin {entry.binary} "
                        f"to {entry.version} for unattended runs"
                    ),
                }
            )
        else:
            results.append(
                {
                    "name": label,
                    "status": _CHECK_PASS,
                    "detail": f"{version} <= last-green {entry.version}",
                    "fix": "",
                }
            )
    return results


def check_api_keys() -> list[dict[str, Any]]:
    """Check environment variables for common API keys."""
    results: list[dict[str, Any]] = []
    keys = {
        "ANTHROPIC_API_KEY": "Claude",
        "OPENAI_API_KEY": "Codex / OpenAI",
        "GOOGLE_API_KEY": "Gemini",
    }
    for env_var, label in keys.items():
        present = bool(os.environ.get(env_var))
        results.append(
            {
                "name": f"API key: {label}",
                "status": _CHECK_PASS if present else _CHECK_WARN,
                "detail": f"{env_var} set" if present else f"{env_var} not set",
                "fix": f"export {env_var}=<your-key>" if not present else "",
            }
        )
    return results


def check_config_valid() -> dict[str, Any]:
    """Check that bernstein.yaml (if present) is valid YAML."""
    yaml_path = Path.cwd() / "bernstein.yaml"
    if not yaml_path.exists():
        return {
            "name": _CONFIG_FILE_LABEL,
            "status": _CHECK_WARN,
            "detail": "bernstein.yaml not found",
            "fix": "Run 'bernstein init' to create one",
        }
    try:
        import yaml

        with yaml_path.open() as f:
            yaml.safe_load(f)
        return {
            "name": _CONFIG_FILE_LABEL,
            "status": _CHECK_PASS,
            "detail": f"bernstein.yaml valid ({yaml_path})",
            "fix": "",
        }
    except Exception as exc:
        return {
            "name": _CONFIG_FILE_LABEL,
            "status": _CHECK_FAIL,
            "detail": f"bernstein.yaml parse error: {exc}",
            "fix": "Fix YAML syntax in bernstein.yaml",
        }


def check_disk_space() -> dict[str, Any]:
    """Check available disk space (warn if < 1 GB)."""
    try:
        usage = shutil.disk_usage(Path.cwd())
        free_gb = usage.free / (1024**3)
        ok = free_gb >= 1.0
        return {
            "name": "Disk space",
            "status": _CHECK_PASS if ok else _CHECK_WARN,
            "detail": f"{free_gb:.1f} GB free ({_format_bytes(usage.free)})",
            "fix": "Free up disk space" if not ok else "",
        }
    except Exception as exc:
        return {
            "name": "Disk space",
            "status": _CHECK_WARN,
            "detail": f"could not check: {exc}",
            "fix": "",
        }


def check_git_installed() -> dict[str, Any]:
    """Check that git is installed and accessible."""
    git_path = shutil.which("git")
    if not git_path:
        return {
            "name": "Git",
            "status": _CHECK_FAIL,
            "detail": "git not found in PATH",
            "fix": "Install git: https://git-scm.com/",
        }
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        version = result.stdout.strip()
        return {
            "name": "Git",
            "status": _CHECK_PASS,
            "detail": version,
            "fix": "",
        }
    except Exception as exc:
        return {
            "name": "Git",
            "status": _CHECK_WARN,
            "detail": f"git found but error: {exc}",
            "fix": "",
        }


def check_server_reachable() -> dict[str, Any]:
    """Check if the Bernstein task server is reachable."""
    try:
        import httpx

        resp = httpx.get(f"{SERVER_URL}/health", timeout=2.0)
        if resp.status_code == 200:
            return {
                "name": _TASK_SERVER_LABEL,
                "status": _CHECK_PASS,
                "detail": f"reachable at {SERVER_URL}",
                "fix": "",
            }
        return {
            "name": _TASK_SERVER_LABEL,
            "status": _CHECK_WARN,
            "detail": f"returned {resp.status_code}",
            "fix": "Start with 'bernstein run'",
        }
    except Exception:
        return {
            "name": _TASK_SERVER_LABEL,
            "status": _CHECK_WARN,
            "detail": "not running",
            "fix": "Start with 'bernstein run'",
        }


def check_port_available() -> dict[str, Any]:
    """Check if port 8052 is available or already in use by Bernstein."""
    port = 8052
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", port))
            in_use = result == 0
    except Exception:
        in_use = False

    if in_use:
        return {
            "name": f"Port {port}",
            "status": _CHECK_WARN,
            "detail": "in use (server may already be running)",
            "fix": "Run 'bernstein stop' to free the port",
        }
    return {
        "name": f"Port {port}",
        "status": _CHECK_PASS,
        "detail": "available",
        "fix": "",
    }


def _spiffe_extra_available() -> bool:
    """Return True when the optional ``spiffe`` extra (py-spiffe SDK) is importable.

    Wrapped as a module-level indirection so tests can stub the extra presence
    without installing the SDK.
    """
    from bernstein.core.identity.spiffe.workload_api import spiffe_extra_available

    return spiffe_extra_available()


def _spiffe_socket_reachable(endpoint: str) -> bool:
    """Return True when the SPIRE Workload API socket at ``endpoint`` accepts a connect.

    Accepts a ``unix://`` endpoint or a bare filesystem path. A missing path or
    a non-socket file is treated as unreachable; the probe never blocks longer
    than half a second and never sends credential material.
    """
    import stat as _stat

    path = endpoint[len("unix://") :] if endpoint.startswith("unix://") else endpoint
    if not path:
        return False
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    if not _stat.S_ISSOCK(mode):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(path)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def check_spiffe_workload_api() -> dict[str, Any]:
    """Preflight the packaged SPIFFE credential path (issue #2516, Phase 4).

    The default Ed25519 identity path needs no extra. This check reports:

    * PASS (informational) when the ``spiffe`` extra is absent -- the default
      path is active and nothing is broken;
    * WARN when the extra is present but ``SPIFFE_ENDPOINT_SOCKET`` is unset or
      the socket is not reachable -- workload-attested grants cannot be issued
      until a SPIRE agent is wired up;
    * PASS when the extra is present and the Workload API socket is reachable --
      the SVID path is ready and new grants can carry the SPIFFE ID issuer.
    """
    name = "SPIFFE workload API"
    if not _spiffe_extra_available():
        return {
            "name": name,
            "status": _CHECK_PASS,
            "detail": "spiffe extra not installed; default Ed25519 identity path active",
            "fix": "",
        }
    endpoint = os.environ.get("SPIFFE_ENDPOINT_SOCKET", "").strip()
    if not endpoint:
        return {
            "name": name,
            "status": _CHECK_WARN,
            "detail": "spiffe extra present but SPIFFE_ENDPOINT_SOCKET is unset",
            "fix": "Point SPIFFE_ENDPOINT_SOCKET at the SPIRE agent's Workload API socket",
        }
    if not _spiffe_socket_reachable(endpoint):
        return {
            "name": name,
            "status": _CHECK_WARN,
            "detail": f"Workload API socket not reachable at {endpoint}",
            "fix": "Start the SPIRE agent or correct SPIFFE_ENDPOINT_SOCKET",
        }
    return {
        "name": name,
        "status": _CHECK_PASS,
        "detail": f"SVID path ready; Workload API socket reachable at {endpoint}",
        "fix": "",
    }


def check_sdd_workspace() -> dict[str, Any]:
    """Check for .sdd/ workspace structure."""
    workdir = Path.cwd()
    required = [".sdd", ".sdd/backlog", ".sdd/runtime"]
    missing = [d for d in required if not (workdir / d).exists()]
    if missing:
        return {
            "name": ".sdd workspace",
            "status": _CHECK_WARN,
            "detail": f"missing: {', '.join(missing)}",
            "fix": "Run 'bernstein init' to create workspace",
        }
    return {
        "name": ".sdd workspace",
        "status": _CHECK_PASS,
        "detail": "present",
        "fix": "",
    }


def check_schedule_supervisor() -> dict[str, Any]:
    """Check the schedule supervisor liveness, last fire, next fire.

    Surfaces #1798's doctor AC: confirm the supervisor is alive and
    report the timestamps the operator needs to reason about the
    recurring-goal subsystem.
    """
    workdir = Path.cwd()
    sdd_dir = workdir / ".sdd"
    if not sdd_dir.exists():
        return {
            "name": "Schedule supervisor",
            "status": _CHECK_WARN,
            "detail": "no .sdd workspace",
            "fix": "Run 'bernstein init' first",
        }
    try:
        from bernstein.core.orchestration.schedule_supervisor import ScheduleSupervisor
        from bernstein.core.planning.schedule_store import ScheduleStore

        store = ScheduleStore(sdd_dir)
        supervisor = ScheduleSupervisor(store, lambda _e: None, None)
        status = supervisor.status()
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "name": "Schedule supervisor",
            "status": _CHECK_WARN,
            "detail": f"unavailable: {exc}",
            "fix": "Check src/bernstein/core/orchestration/schedule_supervisor.py imports",
        }

    if status.schedules_total == 0:
        return {
            "name": "Schedule supervisor",
            "status": _CHECK_PASS,
            "detail": "no schedules registered",
            "fix": "",
        }

    import time as _time

    parts = [f"{status.schedules_total} schedules"]
    if status.last_fire_at:
        parts.append(f"last fire {_time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime(status.last_fire_at))}")
    else:
        parts.append("last fire (none)")
    if status.next_fire_at:
        parts.append(f"next fire {_time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime(status.next_fire_at))}")
    detail = "; ".join(parts)

    # We cannot prove liveness from a single doctor invocation because
    # the supervisor lives in a separate process. Report PASS when
    # schedules exist + a next-fire is computable; surface a WARN only
    # when computation itself failed (handled above).
    return {
        "name": "Schedule supervisor",
        "status": _CHECK_PASS,
        "detail": detail,
        "fix": "",
    }


def check_price_table_advisory() -> dict[str, Any]:
    """Warn when the shipped cost-scheduling price table is stale (issue #2354).

    USD budgets are enforced against a hash-pinned price table; provider rates
    drift between releases, so a table older than the staleness window is a
    signal to refresh ``cost_policy.pricing`` (or the shipped defaults). This is
    an advisory only -- it never blocks.
    """
    from datetime import UTC, datetime

    from bernstein.core.cost.scheduling.price_table import DEFAULT_PRICE_TABLE, price_table_staleness

    advisory = price_table_staleness(DEFAULT_PRICE_TABLE, now_iso=datetime.now(tz=UTC).strftime("%Y-%m-%d"))
    return {
        "name": "Cost price table",
        "status": _CHECK_WARN if advisory.stale else _CHECK_PASS,
        "detail": advisory.message,
        "fix": "Refresh cost_policy.pricing in bernstein.yaml or update the shipped price table"
        if advisory.stale
        else "",
    }


def check_knob_matrix_advisory() -> dict[str, Any]:
    """Warn when the shipped dispatch knob matrix is stale (issue #2519).

    The knob matrix pins the effort, lane, and cache economics every dispatch
    fingerprint seals; its lane / cache multipliers drift with provider pricing,
    so a matrix older than the staleness window is a signal to refresh
    ``cost_policy.knobs`` (or the shipped defaults). Advisory only -- non-blocking.
    """
    from datetime import UTC, datetime

    from bernstein.core.cost.scheduling.knob_matrix import DEFAULT_KNOB_MATRIX, knob_matrix_staleness

    advisory = knob_matrix_staleness(DEFAULT_KNOB_MATRIX, now_iso=datetime.now(tz=UTC).strftime("%Y-%m-%d"))
    return {
        "name": "Cost knob matrix",
        "status": _CHECK_WARN if advisory.stale else _CHECK_PASS,
        "detail": advisory.message,
        "fix": "Refresh cost_policy.knobs in bernstein.yaml or update the shipped knob matrix"
        if advisory.stale
        else "",
    }


def check_skill_revocations() -> list[dict[str, Any]]:
    """Flag catalog-installed skills covered by a signed revocation (issue #2527).

    Reads the project's cached catalog and ``skills.lock`` and reports every
    installed version a *signed* revocation covers, so an operator sees the
    fleet-wide kill switch's effect within one poll interval. Advisory: it
    never fails the process, and any error degrades to a single WARN row rather
    than masking other checks.
    """
    try:
        from bernstein.core.skills.catalog.enforcement import revoked_install_report

        refused = revoked_install_report(Path.cwd())
    except Exception as exc:  # pragma: no cover - defensive; advisory surface
        return [
            {
                "name": "Skill revocations",
                "status": _CHECK_WARN,
                "detail": f"could not evaluate revocations: {exc}",
                "fix": "",
            }
        ]

    if not refused:
        return [
            {
                "name": "Skill revocations",
                "status": _CHECK_PASS,
                "detail": "no installed skill is under a signed revocation",
                "fix": "",
            }
        ]

    return [
        {
            "name": f"Skill revocation: {item.skill_id} {item.version}",
            "status": _CHECK_FAIL,
            "detail": f"revoked ({item.reason}); range {item.version_range}",
            "fix": f"Uninstall or upgrade {item.skill_id} out of {item.version_range}",
        }
        for item in refused
    ]


def check_eval_gate_min_n_advisory(workdir: Path | None = None) -> dict[str, Any]:
    """Warn when a stored eval gate decision was taken below the minimum n (#2520).

    A verdict receipt records whether the minimum n per arm was met. A gate
    decision taken below that floor is statistically underpowered, so any such
    receipt in ``.sdd/eval/gate`` is surfaced here as an advisory (never
    blocking): it flags promotions that may have stood on too few tasks to
    survive a re-run.
    """
    import json as _json

    root = workdir if workdir is not None else Path()
    gate_dir = root / ".sdd" / "eval" / "gate"
    underpowered = 0
    scanned = 0
    if gate_dir.is_dir():
        for path in gate_dir.glob("sha256:*.json"):
            try:
                raw = _json.loads(path.read_text(encoding="utf-8"))
                evidence = raw["evidence"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            scanned += 1
            if evidence.get("min_n_satisfied") is False:
                underpowered += 1
    if underpowered:
        return {
            "name": "Eval gate power",
            "status": _CHECK_WARN,
            "detail": f"{underpowered}/{scanned} verdict receipt(s) decided below the minimum n per arm",
            "fix": "Re-run the gate with a larger suite (raise n per arm) before promoting on those verdicts",
        }
    return {
        "name": "Eval gate power",
        "status": _CHECK_PASS,
        "detail": f"{scanned} verdict receipt(s) met the minimum n per arm" if scanned else "no verdict receipts",
        "fix": "",
    }


def run_all_checks() -> list[dict[str, Any]]:
    """Run all health checks and return results."""
    checks: list[dict[str, Any]] = []
    checks.append(check_python_version())
    checks.extend(check_adapters_installed())
    checks.extend(check_adapter_advisories())
    checks.extend(check_canary_last_green())
    checks.extend((check_price_table_advisory(), check_knob_matrix_advisory()))
    checks.extend(check_skill_revocations())
    checks.append(check_eval_gate_min_n_advisory())
    checks.extend(check_api_keys())
    checks.extend(
        (
            check_config_valid(),
            check_disk_space(),
            check_git_installed(),
            check_server_reachable(),
            check_port_available(),
            check_sdd_workspace(),
            check_schedule_supervisor(),
            check_spiffe_workload_api(),
        )
    )
    return checks


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Substrate health-check (``bernstein doctor --substrate``)
# ---------------------------------------------------------------------------


def _substrate_status_for(host: Any) -> dict[str, Any]:
    """Return a doctor-style row describing one host's substrate state.

    States:
      - ``unsupported``: host is stubbed (we cannot register it yet)
      - ``no_config_path``: host is supported but path is unavailable
      - ``not_registered``: host config exists / could exist; no entry
      - ``registered``: entry present and matches canonical command
      - ``stale``: entry present but command/args differ from canonical
    """
    from bernstein.core.substrate import is_registered, is_stale

    if not host.supported:
        return {"host": host.name, "state": "unsupported", "config_path": None}
    path = host.config_path()
    if path is None:
        return {"host": host.name, "state": "no_config_path", "config_path": None}
    if not is_registered(host, path=path):
        return {"host": host.name, "state": "not_registered", "config_path": str(path)}
    if is_stale(host, path=path):
        return {"host": host.name, "state": "stale", "config_path": str(path)}
    return {"host": host.name, "state": "registered", "config_path": str(path)}


def _run_substrate_checks() -> list[dict[str, Any]]:
    """Build the substrate report for every host in the registry."""
    from bernstein.core.substrate import HOST_REGISTRY, known_host_names

    return [_substrate_status_for(HOST_REGISTRY[name]) for name in known_host_names()]


def _render_substrate_report(  # NOSONAR python:S3516 - advisory surface, always exit 0 by design
    rows: list[dict[str, Any]], *, as_json: bool
) -> int:
    """Render the substrate report and return the desired exit code.

    The substrate report is advisory: it always returns ``0`` regardless
    of the host states it renders. A non-zero exit code is reserved for a
    future ``--gate`` flag and is intentionally not produced here (hence
    the ``S3516`` invariant-return waiver).
    """
    import json as _json

    from rich.table import Table

    from bernstein.cli.helpers import console

    if as_json:
        console.print_json(_json.dumps({"substrate": rows}))
        return 0

    table = Table(title="Bernstein substrate state", show_lines=False)
    table.add_column("Host", style="cyan", no_wrap=True)
    table.add_column("State")
    table.add_column("Config path", overflow="fold")

    palette = {
        "registered": "[green]registered[/green]",
        "not_registered": "[yellow]not_registered[/yellow]",
        "stale": "[red]stale[/red]",
        "unsupported": "[dim]unsupported[/dim]",
        "no_config_path": "[dim]no_config_path[/dim]",
    }
    for row in rows:
        path = row["config_path"] or "[dim](n/a)[/dim]"
        table.add_row(row["host"], palette.get(row["state"], row["state"]), str(path))

    console.print(table)
    return 0


# ---------------------------------------------------------------------------
# Endpoint certification (``bernstein doctor --endpoint``, issue #2356)
# ---------------------------------------------------------------------------


def _run_endpoint_certification(
    *,
    endpoint: str,
    model: str | None,
    engine: str,
    api_key_env: str | None,
    timeout: float,
    roles: tuple[str, ...],
    as_json: bool,
) -> int:
    """Probe an OpenAI-compatible endpoint and seal a certification receipt.

    Returns the process exit code: 0 when every evaluated role certified,
    1 when at least one role was rejected, 2 when no model could be
    resolved for the endpoint.
    """
    import json as _json
    import time

    from rich.table import Table

    from bernstein.cli.helpers import console
    from bernstein.core.endpoints.certification import (
        build_endpoint_certification,
        certification_path,
        load_or_create_endpoint_identity,
    )
    from bernstein.core.endpoints.conformance import (
        LOCAL_TIER_ROLES,
        discover_default_model,
        evaluate_roles,
        run_conformance,
    )
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore

    api_key = os.environ.get(api_key_env) if api_key_env else None

    resolved_model = model or discover_default_model(base_url=endpoint, api_key=api_key, timeout=timeout)
    if not resolved_model:
        console.print(
            "[red]Cannot resolve a model for this endpoint.[/red] The /models "
            "listing is unavailable; pass --endpoint-model explicitly."
        )
        return 2

    evaluated_roles = roles or tuple(sorted(LOCAL_TIER_ROLES))
    transcript = run_conformance(
        base_url=endpoint,
        model=resolved_model,
        api_key=api_key,
        timeout=timeout,
    )
    verdicts = evaluate_roles(transcript, evaluated_roles)

    workdir = Path.cwd()
    hmac_key = load_or_create_audit_key()
    private_pem, public_pem = load_or_create_endpoint_identity(workdir / ".sdd" / "identity")
    chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
    sealed = build_endpoint_certification(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        transcript=transcript,
        verdicts=verdicts,
        engine=engine,
        timestamp=int(time.time()),
        chain=chain,
    )
    receipt_path = certification_path(workdir, sealed.fingerprint())
    all_certified = all(v.certified for v in verdicts)

    if as_json:
        payload = {
            "base_url": transcript.base_url,
            "model": resolved_model,
            "engine": engine,
            "fingerprint": sealed.fingerprint(),
            "suite_version": transcript.suite_version,
            "transcript_hash": transcript.transcript_hash(),
            "probes": [r.to_dict() for r in transcript.results],
            "verdicts": [v.to_dict() for v in verdicts],
            "journal_entry_hash": sealed.journal_entry_hash,
            "receipt_path": str(receipt_path),
        }
        click.echo(_json.dumps(payload, indent=2, sort_keys=True))
        return 0 if all_certified else 1

    console.print()
    console.print(f"[bold]Endpoint certification[/bold] {transcript.base_url} model={resolved_model}")
    console.print(f"  transcript  {transcript.transcript_hash()}")
    console.print(f"  receipt     {receipt_path}")
    console.print(f"  anchor      {sealed.journal_entry_hash}")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Role", style="bold")
    table.add_column("Verdict")
    table.add_column("Reasons", overflow="fold")
    for verdict in verdicts:
        status = "[green]certified[/green]" if verdict.certified else "[red]rejected[/red]"
        table.add_row(verdict.role, status, ", ".join(verdict.reasons) or "-")
    console.print(table)
    console.print(
        "\n[dim]The receipt is signed and anchored to the audit chain; config "
        "validation gates merge-critical roles on it.[/dim]\n"
    )
    return 0 if all_certified else 1


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--fix", "auto_fix", is_flag=True, default=False, help="Attempt to auto-fix issues.")
@click.option(
    "--substrate",
    "substrate_only",
    is_flag=True,
    default=False,
    help="Report which host applications have Bernstein registered.",
)
@click.pass_context
def doctor_cmd(ctx: click.Context, as_json: bool, auto_fix: bool, substrate_only: bool) -> None:
    """Run health checks on the Bernstein installation.

    \b
    Checks:
      - Python version (>= 3.12)
      - CLI adapters installed (claude, codex, gemini, qwen, aider)
      - API keys set (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY)
      - Config file valid (bernstein.yaml)
      - Disk space (>= 1 GB free)
      - Git installed and accessible
      - Task server reachable
      - Port 8052 available
      - .sdd workspace structure

    \b
    Examples:
      bernstein doctor             # print diagnostic report
      bernstein doctor --json      # machine-readable output
      bernstein doctor --fix       # attempt to auto-fix issues
      bernstein doctor --substrate # report host registration state only
    """
    if substrate_only:
        rows = _run_substrate_checks()
        exit_code = _render_substrate_report(rows, as_json=as_json)
        if exit_code:
            raise SystemExit(exit_code)
        return

    # Delegate to the existing full doctor implementation which has more checks
    from bernstein.cli.status_cmd import doctor as _doctor_impl

    ctx.invoke(_doctor_impl, as_json=as_json, auto_fix=auto_fix)
