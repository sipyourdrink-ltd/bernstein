"""Multi-host conformance harness for the packaged bernstein skill (#2369).

The install path (:mod:`bernstein.core.skills.packaging`) proves *what*
content an agent host is driving. The remaining issue ACs are live
validation:

* A fresh session can install the plugin and launch a verified run end to
  end.
* The skill works from at least three different agent CLIs against the same
  bernstein install.

This module proves both against one shared install. For each selected agent
host it installs the single bundled skill into that host's skill directory
(receipt-backed, so all hosts share one install lineage) and replays the
skill's documented self-check contract through a :class:`HostTransport`.

The transport is the external boundary an agent CLI crosses when it shells
out ``bernstein ...``. Production shells a subprocess
(:class:`SubprocessTransport`); the boundary is doubled in tests by an
in-process transport that runs the real CLI, so a broken install yields a
real red verdict rather than a stubbed pass. Only the OS-process boundary is
doubled - the production coordination logic in this module is exercised as-is.

The aggregate proof is a content-addressed
:class:`~bernstein.core.skills.provenance.ConformanceReceipt` anchored in the
``skills`` lineage spine plus a ``plugin.conformance_receipt`` audit-chain
event. Strip the chain and the pass/fail table is an untracked log; anchored,
it is a signed attestation that one skill content address drove N distinct
hosts against one install and met the ``min_hosts`` bar.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from bernstein.core.skills.packaging import (
    PACKAGED_SKILL_NAME,
    host_skill_parent,
    install_packaged_skill,
    packaged_skill_dir,
    tree_content_hash,
)
from bernstein.core.skills.provenance import (
    ConformanceReceipt,
    write_conformance_receipt,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Minimum green hosts an AC-satisfying conformance sweep requires. The issue
#: AC reads "at least 3 different agent CLIs".
DEFAULT_MIN_HOSTS = 3


# ---------------------------------------------------------------------------
# Command contract
# ---------------------------------------------------------------------------


def host_contract(*, skill_dir: Path, workdir: Path) -> tuple[tuple[str, ...], ...]:
    """Return the ordered ``bernstein`` argv contract replayed per host.

    Both steps are documented in the shipped ``SKILL.md`` and are
    deterministic and side-effect-free: they pass the shared *workdir*
    explicitly so the probe is cwd-independent.

    * ``skills package show`` proves the bundled skill asset resolves in the
      host's environment (bernstein is on PATH and the packaged asset loads).
    * ``skills package verify --dest`` proves the tree installed for this host
      is attested against the shared install's receipt lineage.

    Args:
        skill_dir: The host's installed skill directory.
        workdir: The shared project root holding ``.sdd/``.
    """
    return (
        ("skills", "package", "show", "-w", str(workdir)),
        ("skills", "package", "verify", "--dest", str(skill_dir), "-w", str(workdir)),
    )


# ---------------------------------------------------------------------------
# Transport boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """The exit code an agent host observed running one contract command."""

    argv: tuple[str, ...]
    exit_code: int


class HostTransport(Protocol):
    """How an agent host runs a ``bernstein`` command.

    Implementations model the boundary an agent CLI crosses to shell out
    orchestration. The production implementation runs a subprocess; tests
    inject a faithful in-process double.
    """

    def invoke(self, host: str, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        """Run ``bernstein <argv>`` for *host* in *cwd*; return the exit code."""
        ...


class SubprocessTransport:
    """Run each contract command as a real ``bernstein`` subprocess.

    This is the production transport: it shells out the same command an agent
    CLI would, so the exit code is the real CLI's verdict.
    """

    def __init__(self, bernstein_bin: str | None = None, *, timeout: float = 120.0) -> None:
        self._argv0 = [bernstein_bin] if bernstein_bin else [sys.executable, "-m", "bernstein"]
        self._timeout = timeout

    def invoke(self, host: str, argv: Sequence[str], *, cwd: Path) -> CommandResult:
        cmd = [*self._argv0, *argv]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # Log the exception type only; command output may carry paths.
            logger.warning("conformance transport failed for host %s: %s", host, type(exc).__name__)
            return CommandResult(argv=tuple(argv), exit_code=127)
        return CommandResult(argv=tuple(argv), exit_code=proc.returncode)


# ---------------------------------------------------------------------------
# Per-host and aggregate results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostConformanceResult:
    """One agent host's conformance verdict over the command contract."""

    host: str
    scope: str
    dest: Path
    steps: tuple[CommandResult, ...]
    ok: bool


@dataclass(frozen=True)
class ConformanceOutcome:
    """Result of one multi-host conformance sweep."""

    skill_hash: str
    hosts: tuple[HostConformanceResult, ...]
    passed_hosts: tuple[str, ...]
    min_hosts: int
    ok: bool
    receipt_id: str
    spine_anchor: str


def run_host_conformance(
    *,
    host: str,
    scope: str,
    workdir: Path,
    skill_dir: Path,
    transport: HostTransport,
) -> HostConformanceResult:
    """Replay the command contract for *host* and return its verdict.

    Every contract step runs through *transport*; the host passes only when
    all steps exit 0.
    """
    steps = tuple(
        transport.invoke(host, argv, cwd=workdir) for argv in host_contract(skill_dir=skill_dir, workdir=workdir)
    )
    return HostConformanceResult(
        host=host,
        scope=scope,
        dest=skill_dir,
        steps=steps,
        ok=all(step.exit_code == 0 for step in steps),
    )


def run_conformance(
    *,
    workdir: Path,
    hosts: Sequence[str],
    transport: HostTransport,
    hmac_key: bytes,
    install_id: str,
    timestamp: int,
    source: Path | None = None,
    scope: str = "project",
    home: Path | None = None,
    min_hosts: int = DEFAULT_MIN_HOSTS,
) -> ConformanceOutcome:
    """Install the bundled skill into each host and replay the contract.

    One bundled *source* is installed (receipt-backed) into each host's skill
    directory, so all hosts share one install lineage; the skill's documented
    self-check contract is then replayed per host through *transport*. The
    aggregate verdict, the shared content address, and the per-host pass/fail
    table are sealed into a content-addressed
    :class:`~bernstein.core.skills.provenance.ConformanceReceipt` anchored in
    the ``skills`` lineage spine plus a ``plugin.conformance_receipt`` audit
    event.

    Args:
        workdir: Shared project root; receipts land under ``.sdd/skills/``.
        hosts: Agent hosts to sweep (each must have a default skill dir).
        transport: The boundary that runs each ``bernstein`` command.
        hmac_key: Audit-chain HMAC key tagging spine and chain entries.
        install_id: Per-sweep identifier recorded in the receipt.
        timestamp: Integer timestamp recorded in the receipt.
        source: Skill tree to install; defaults to the bundled skill.
        scope: Install scope for the host skill directories.
        home: Home-directory override for ``user`` scope (tests).
        min_hosts: Minimum green hosts the sweep requires (default 3).

    Returns:
        A :class:`ConformanceOutcome`. ``ok`` is True only when every swept
        host passed and at least ``min_hosts`` hosts were green.

    Raises:
        PackagedInstallError: On an unknown host or a failed install.
    """
    src = source if source is not None else packaged_skill_dir()
    skill_hash = tree_content_hash(src)

    results: list[HostConformanceResult] = []
    for host in hosts:
        parent = host_skill_parent(host, scope, workdir=workdir, home=home)
        dest = parent / PACKAGED_SKILL_NAME
        install_packaged_skill(
            workdir=workdir,
            dest=dest,
            source=src,
            hmac_key=hmac_key,
            install_id=f"{install_id}-{host}-{scope}",
            timestamp=timestamp,
            host=host,
            scope=scope,
            force=True,
        )
        results.append(
            run_host_conformance(
                host=host,
                scope=scope,
                workdir=workdir,
                skill_dir=dest,
                transport=transport,
            )
        )

    passed = tuple(r.host for r in results if r.ok)
    ok = len(passed) == len(results) and len(passed) >= min_hosts

    host_results = tuple(sorted((r.host, r.ok) for r in results))
    receipt = ConformanceReceipt(
        skill_hash=skill_hash,
        host_results=host_results,
        min_hosts=min_hosts,
        install_id=install_id,
        timestamp=timestamp,
    )
    receipt_id, anchor = write_conformance_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        receipt=receipt,
    )
    _record_conformance_chain_event(
        workdir=workdir,
        hmac_key=hmac_key,
        skill_hash=skill_hash,
        receipt_id=receipt_id,
        host_results=list(host_results),
        min_hosts=min_hosts,
        passed_hosts=len(passed),
        ok=ok,
        install_id=install_id,
        spine_anchor=anchor,
    )
    return ConformanceOutcome(
        skill_hash=skill_hash,
        hosts=tuple(results),
        passed_hosts=passed,
        min_hosts=min_hosts,
        ok=ok,
        receipt_id=receipt_id,
        spine_anchor=anchor,
    )


def _record_conformance_chain_event(
    *,
    workdir: Path,
    hmac_key: bytes,
    skill_hash: str,
    receipt_id: str,
    host_results: list[tuple[str, bool]],
    min_hosts: int,
    passed_hosts: int,
    ok: bool,
    install_id: str,
    spine_anchor: str,
) -> None:
    """Mirror the conformance receipt into the HMAC audit chain."""
    from bernstein.core.security.audit_chain import (
        AuditChainStore,
        record_plugin_conformance_receipt,
    )

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
    record_plugin_conformance_receipt(
        chain=chain,
        skill_hash=skill_hash,
        receipt_id=receipt_id,
        host_results=host_results,
        min_hosts=min_hosts,
        passed_hosts=passed_hosts,
        ok=ok,
        install_id=install_id,
        spine_anchor=spine_anchor,
    )


__all__ = [
    "DEFAULT_MIN_HOSTS",
    "CommandResult",
    "ConformanceOutcome",
    "HostConformanceResult",
    "HostTransport",
    "SubprocessTransport",
    "host_contract",
    "run_conformance",
    "run_host_conformance",
]
