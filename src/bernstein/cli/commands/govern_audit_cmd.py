"""``bernstein govern audit``: check contract runner and finding reporter (#5072).

Runs all registered governance checks over a workspace with filtering by area
(--only) and check ID (--skip), emitting findings with evidence hashes and a
three-state verdict vocabulary.
"""

from __future__ import annotations

import json
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.checks.contract import Verdict
from bernstein.core.checks.registry import (
    DEFAULT_REGISTRY,
    check_area,
    populate_default_checks,
)

if TYPE_CHECKING:
    from bernstein.core.checks.contract import Finding
    from bernstein.core.checks.registry import CheckRegistry


# Stable installation IDs, in the legacy doctor's display order. Backend
# helpers share storage's ID: postgres/redis are alternate implementations.
INSTALLATION_CHECKS: tuple[tuple[str, str], ...] = (
    ("INS-001", "python"),
    ("INS-002", "adapters"),
    ("INS-003", "last_green"),
    ("INS-004", "version_posture"),
    ("INS-005", "auth"),
    ("INS-006", "port"),
    ("INS-007", "workspace"),
    ("INS-008", "stale_pids"),
    ("INS-009", "guardrails"),
    ("INS-010", "ci_tools"),
    ("INS-011", "storage"),
    ("INS-012", "secrets"),
    ("INS-013", "readiness"),
    ("INS-014", "context_and_plugins"),
    ("INS-015", "commit_attribution"),
    ("INS-016", "compliance"),
    ("INS-017", "schedule_supervisor"),
    ("INS-018", "eval_gate_power"),
    ("INS-019", "otel_export"),
    ("INS-020", "audit_anchoring"),
)


class DoctorInstallationAdapter:
    """An opt-in install check, sharing the exact producer used by doctor."""

    area = "INS"
    opt_in = True

    def __init__(self, check_id: str, name: str) -> None:
        self.check_id = check_id
        self._name = name
        self.title = name.replace("_", " ").capitalize()
        self.description = f"Doctor installation check: {self.title}"

    def run(self, workdir: Path | None = None) -> Finding:
        from bernstein.cli.commands import status_cmd

        helper = getattr(status_cmd, f"_doctor_check_{self._name}")
        rows: list[dict[str, Any]] = []
        arguments: dict[str, Any] = {}
        for name in signature(helper).parameters:
            if name == "checks":
                arguments[name] = rows
            elif name == "workdir":
                arguments[name] = workdir or Path.cwd()
            elif name == "_check":
                arguments[name] = lambda *args: status_cmd._add_check(rows, *args)
        return helper(**arguments)

    def __call__(self, workdir: Path | None = None) -> Finding:
        return self.run(workdir)


def populate_installation_checks(registry: CheckRegistry) -> None:
    """Compose the CLI-owned doctor checks explicitly into this audit runner."""
    for check_id, name in INSTALLATION_CHECKS:
        if registry.get_check(check_id) is None:
            registry.register(DoctorInstallationAdapter(check_id, name))


def _has_failures(findings: list[Finding]) -> bool:
    """Return True if any finding is not a passing finding."""
    return any(f.verdict != Verdict.PASS and not f.passed for f in findings)


def _record_sentinel_run(root: Path, findings: list[Finding]) -> None:
    """Anchor the injected run, then dispatch through the existing transport."""
    from bernstein.core.checks.adapters import AuditSentinelCheck

    sentinel = next((f for f in findings if f.check_id == AuditSentinelCheck.check_id and f.passed is False), None)
    if sentinel is None:
        return

    import asyncio
    import time
    from dataclasses import asdict

    from bernstein.cli.commands.notify_cmd import _load_notifications_config
    from bernstein.core.govern.audit_report import GOVERN_AUDIT_RUN_ID, AuditReport, anchor_audit_report
    from bernstein.core.lifecycle.notify_bridge import build_bridge_from_config
    from bernstein.core.notifications.protocol import NotificationEvent, NotificationEventKind
    from bernstein.core.security.audit import load_or_create_audit_key

    rows: tuple[dict[str, Any], ...] = tuple(asdict(f) | {"id": f.check_id} for f in findings)
    report = anchor_audit_report(
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=load_or_create_audit_key(),
        report=AuditReport(findings=rows, timestamp=int(time.time())),
    )
    config_path = root / "bernstein.yaml"
    if not config_path.exists():
        return
    config = _load_notifications_config(config_path)
    if not config.enabled:
        return
    bridge = build_bridge_from_config(config, runtime_dir=root / ".sdd" / "runtime", register_in_registry=False)
    event = NotificationEvent(
        event_id=f"govern-audit-sentinel-{report.journal_entry_hash}",
        kind=NotificationEventKind.SYNTHETIC,
        title="Governance audit selftest failure",
        body=sentinel.summary,
        severity="error",
        run_id=GOVERN_AUDIT_RUN_ID,
        timestamp=report.timestamp,
        details={"check_id": sentinel.check_id, "journal_entry_hash": report.journal_entry_hash},
    )

    async def dispatch() -> None:
        try:
            await bridge.dispatch_event(event)
        finally:
            await bridge.aclose()

    asyncio.run(dispatch())


@click.command("audit")
@click.option(
    "--list",
    "list_checks",
    is_flag=True,
    help="List registered check IDs and areas without running them.",
)
@click.option(
    "--only",
    "only_areas",
    multiple=True,
    help="Run only checks in the specified AREA (repeatable, e.g. --only doctor).",
)
@click.option(
    "--skip",
    "skip_ids",
    multiple=True,
    help="Skip checks matching the specified ID (repeatable, e.g. --skip doctor:compliance).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root directory to audit.",
)
@click.option(
    "--json",
    "--json-output",
    "as_json",
    is_flag=True,
    help="Output findings as JSON.",
)
def govern_audit_cmd(
    list_checks: bool,
    only_areas: tuple[str, ...],
    skip_ids: tuple[str, ...],
    workdir: str,
    as_json: bool,
    registry: CheckRegistry | None = None,
) -> None:
    """Run registered governance audit checks across the workspace.

    Note: the verifier-key staleness check this command used to run now
    lives in ``bernstein govern audit-keys``.

    BERNSTEIN_GOVERN_AUDIT_SENTINEL=1 enables the harmless sentinel:failure
    selftest, journaling its run and dispatching a synthetic notification.

    Exit codes:
      0: All executed checks passed (at least one check executed).
      1: One or more checks failed, were not measurable, or no checks were selected.
    """
    reg = DEFAULT_REGISTRY if registry is None else registry
    populate_default_checks(reg)
    populate_installation_checks(reg)

    if list_checks:
        checks = reg.list_checks(only_areas=only_areas, skip_ids=skip_ids)
        if as_json:
            payload = [
                {
                    "check_id": c.check_id,
                    "area": check_area(c),
                    "title": getattr(c, "title", ""),
                    "description": getattr(c, "description", ""),
                }
                for c in checks
            ]
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(0)

        table = Table(title="Registered Governance Audit Checks", show_header=True, header_style="bold cyan")
        table.add_column("Check ID", style="bold", no_wrap=True)
        table.add_column("Area", style="magenta")
        table.add_column("Title", style="white")
        table.add_column("Description", style="dim")

        for c in checks:
            table.add_row(c.check_id, check_area(c), getattr(c, "title", ""), getattr(c, "description", ""))

        console.print(table)
        console.print(f"\n[bold]{len(checks)}[/bold] check(s) matched.")
        raise SystemExit(0)

    root = Path(workdir).resolve()
    from bernstein.core.checks.adapters import AuditSentinelCheck

    if AuditSentinelCheck().active:
        click.echo(
            f"Sentinel active: {AuditSentinelCheck.env_var}=1 ({AuditSentinelCheck.check_id}); synthetic failure.",
            err=True,
        )
    findings = reg.run_all(workdir=root, only_areas=only_areas, skip_ids=skip_ids)
    _record_sentinel_run(root, findings)

    if not findings:
        if as_json:
            click.echo(json.dumps([], indent=2))
        else:
            console.print("[bold red]No checks matched the specified selector(s).[/bold red]")
        raise SystemExit(1)

    has_failed = _has_failures(findings)

    if as_json:
        payload = [f.to_dict() for f in findings]
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(1 if has_failed else 0)

    table = Table(title=f"Governance Audit Findings ({root.name})", show_header=True, header_style="bold cyan")
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Check ID", style="bold", no_wrap=True)
    table.add_column("Area", style="magenta")
    table.add_column("Summary / Diagnostic", style="white")

    for f in findings:
        if f.verdict == Verdict.PASS or f.passed:
            status_badge = "[bold green]PASS[/bold green]"
        elif f.verdict == Verdict.NOT_MEASURABLE:
            status_badge = "[bold yellow]UNMEASURED[/bold yellow]"
        else:
            status_badge = "[bold red]FAIL[/bold red]"

        msg = f.summary or f.message or f.reason or ""
        if f.remediation and f.verdict != Verdict.PASS:
            msg += f" (Fix: {f.remediation})"
        if f.what_would_make_it_measurable and f.verdict == Verdict.NOT_MEASURABLE:
            msg += f" (Prerequisite: {f.what_would_make_it_measurable})"

        table.add_row(status_badge, f.check_id, f.area, msg)

    console.print()
    console.print(table)
    console.print()

    passed_count = sum(1 for f in findings if f.verdict == Verdict.PASS or f.passed)
    console.print(
        f"Total: [bold]{len(findings)}[/bold] | "
        f"Passed: [bold green]{passed_count}[/bold green] | "
        f"Failed/Unmeasurable: [bold red]{len(findings) - passed_count}[/bold red]"
    )

    if has_failed:
        raise SystemExit(1)
    raise SystemExit(0)
