"""Renderer for ``bernstein doctor sovereign``.

Pure CLI layer; the checks themselves live in
:mod:`bernstein.core.distribution.doctor_sovereign`. Mirrors the standalone
semantics of ``bernstein doctor airgap``: the sovereign doctor is a pre-flight
tool an operator runs *before* ``bernstein run --profile sovereign``, so the
profile env vars are not yet set in their shell. When they are absent the
renderer simulates the sovereign activation (airgap network posture + sovereign
marker) for the duration of the checks, then restores the caller's environment
exactly, so a clean host reports the spec-mandated green rows without the
operator having to export env vars first.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.distribution.doctor_sovereign import SovereignReport, run_sovereign_checks
from bernstein.core.security.network_policy import (
    ENV_NETWORK_POLICY,
    ENV_PROFILE_MODE,
    ENV_SOVEREIGN_MODE,
    PROFILE_AIRGAP,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from bernstein.core.distribution.doctor_airgap import CheckStatus

_STATUS_STYLE: dict[str, str] = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}


@contextlib.contextmanager
def _simulated_sovereign_env(workdir: Path | None = None) -> Iterator[bool]:
    """Activate sovereign env vars for the duration of a standalone doctor run.

    Sovereign composes the airgap network posture, so this sets
    ``BERNSTEIN_PROFILE_MODE=airgap`` and the ``BERNSTEIN_SOVEREIGN_MODE`` marker
    plus the network policy a real ``bernstein run --profile sovereign`` would
    install from ``bernstein.yaml``. All three are restored to their original
    state (including absence) in the ``finally`` block. Yields True when the
    doctor simulated the activation.

    The network policy is derived from ``sovereign.allowed_egress`` (the same
    source the real activation reads), not hard-coded to deny-all. A deny-all
    config still simulates ``none``; an allow-list config simulates that
    allow-list. Hard-coding deny-all made the egress invariant compare an honest
    allow-list attestation against a fabricated deny-all runtime and report a
    mismatch the real run would never produce.
    """
    prior_profile = os.environ.get(ENV_PROFILE_MODE)
    prior_policy = os.environ.get(ENV_NETWORK_POLICY)
    prior_sovereign = os.environ.get(ENV_SOVEREIGN_MODE)
    activated = False
    if (prior_sovereign or "").strip().lower() not in {"1", "true", "sovereign"}:
        # A real ``bernstein run --profile sovereign`` installs the airgap
        # baseline plus the config's egress allow-list, so the simulation
        # overrides BOTH the profile mode and the network policy unconditionally
        # (not only when unset) -- otherwise a stale ``BERNSTEIN_NETWORK_POLICY``
        # from a prior session would make the checks report a state the real run
        # would not produce. The caller's values are restored in ``finally``.
        os.environ[ENV_SOVEREIGN_MODE] = "1"
        os.environ[ENV_PROFILE_MODE] = PROFILE_AIRGAP
        os.environ[ENV_NETWORK_POLICY] = _simulated_network_policy_value(workdir)
        activated = True
    try:
        yield activated
    finally:
        if activated:
            _restore(ENV_PROFILE_MODE, prior_profile)
            _restore(ENV_NETWORK_POLICY, prior_policy)
            _restore(ENV_SOVEREIGN_MODE, prior_sovereign)


def _simulated_network_policy_value(workdir: Path | None) -> str:
    """Return the ``BERNSTEIN_NETWORK_POLICY`` a real sovereign activation installs.

    Mirrors ``_install_profile_network_policy``: the sovereign egress allow-list
    comes from ``sovereign.allowed_egress`` in ``bernstein.yaml``, and an empty
    list is deny-all. Reads the config leniently (a missing or unreadable file
    resolves to deny-all); the dedicated config-readable check still reports an
    unreadable config as a FAIL row, so this fallback does not mask it.
    """
    from pathlib import Path as _Path

    from bernstein.core.security.deployment_profile import load_config_snapshot, sovereign_egress_allowlist
    from bernstein.core.security.network_policy import NetworkPolicy

    snapshot = load_config_snapshot(workdir or _Path.cwd(), require=False)
    egress = sovereign_egress_allowlist(snapshot)
    policy = NetworkPolicy.from_specs(egress) if egress else NetworkPolicy.deny_all()
    return policy.to_env_value()


def _restore(key: str, prior: str | None) -> None:
    if prior is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = prior


def run_doctor_sovereign(*, workdir: Path | None = None, as_json: bool = False) -> int:
    """Run the sovereign battery and render the report. Returns the exit code."""
    with _simulated_sovereign_env(workdir) as simulated:
        report = run_sovereign_checks(workdir=workdir)
    if as_json:
        _render_json(report, simulated=simulated)
    else:
        _render_human(report, simulated=simulated)
    return 0 if report.ok else 1


def _render_json(report: SovereignReport, *, simulated: bool) -> None:
    payload = {
        "ok": report.ok,
        "simulated_sovereign_env": simulated,
        "posture_hash": report.posture_hash,
        "attested_hash": report.attested_hash,
        "checks": [asdict(check) | {"status": _status_value(check.status)} for check in report.checks],
    }
    console.print_json(json.dumps(payload))


def _status_value(status: CheckStatus) -> str:
    return status.value


def _render_human(report: SovereignReport, *, simulated: bool) -> None:
    console.print()
    if report.ok:
        console.print(Panel("[bold green]Sovereign doctor: PASSED[/bold green]", border_style="green", expand=False))
    else:
        console.print(Panel("[bold red]Sovereign doctor: FAILED[/bold red]", border_style="red", expand=False))

    if simulated:
        console.print(
            "[dim]Note: sovereign profile env vars were not set in this shell; the "
            "doctor simulated --profile sovereign for the duration of the checks. Invoke "
            "via 'bernstein run --profile sovereign' to suppress this notice.[/dim]"
        )

    console.print(f"[dim]Live posture:[/dim] {report.posture_hash}")
    console.print(f"[dim]Attested posture:[/dim] {report.attested_hash or '(none yet)'}")

    table = Table(show_header=True, header_style="bold", padding=(0, 2))
    table.add_column("Status", no_wrap=True, min_width=6)
    table.add_column("Check", no_wrap=True)
    table.add_column("Detail")
    for check in report.checks:
        style = _STATUS_STYLE.get(check.status.value, "white")
        table.add_row(f"[{style}]{check.status.value}[/{style}]", check.name, check.detail)
    console.print(table)

    fixes = [c for c in report.checks if c.fix and c.status.value != "PASS"]
    if fixes:
        console.print()
        console.print("[bold]Suggested fixes:[/bold]")
        for c in fixes:
            console.print(f"  [dim]{c.name}:[/dim] {c.fix}")
    console.print()
