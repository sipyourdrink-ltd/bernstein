"""``bernstein govern audit``: check contract runner and finding reporter (#5072).

Runs all registered governance checks over a workspace with filtering by area
(--only) and check ID (--skip), emitting findings with evidence hashes and a
three-state verdict vocabulary.
"""

from __future__ import annotations

import json
import re
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

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


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_MEASURABLE = 2


def _finding_kind(finding: Finding) -> str:
    """Map legacy pass/fail verdicts into the three-state vocabulary."""
    if finding.verdict in (Verdict.PASS, Verdict.FAIL):
        return Verdict.MEASURED.value
    return finding.verdict.value


def _measured_failure(finding: Finding) -> bool:
    return finding.verdict == Verdict.FAIL or (finding.verdict == Verdict.MEASURED and finding.passed is not True)


def _exit_code(findings: list[Finding], *, strict: bool, execution_failed: bool = False) -> int:
    """Preserve main's fail-closed gate; distinguish strict incompleteness."""
    if not findings or execution_failed or any(_measured_failure(f) for f in findings):
        return EXIT_FAILED
    if any(f.verdict == Verdict.NOT_MEASURABLE for f in findings):
        return EXIT_NOT_MEASURABLE if strict else EXIT_FAILED
    return EXIT_OK


def _finding_dict(finding: Finding) -> dict[str, Any]:
    """Preserve every contract field, including the public id alias."""
    return finding.to_dict()


def _sarif_location(finding: Finding, root: Path) -> dict[str, Any]:
    """Use reported files; workspace-only evidence points to its root config."""
    for evidence in finding.evidence:
        locator = evidence.locator
        # These producers prefix the evaluated workspace path with a check name.
        for prefix in ("doctor:compliance:", "compliance:encryption_at_rest:"):
            if locator.startswith(prefix):
                locator = locator.removeprefix(prefix)
                break
        parsed = urlsplit(locator)
        if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
            path = Path(unquote(parsed.path))
        elif not parsed.scheme:
            path = Path(locator)
        else:
            continue
        path = (root / path).resolve()
        if not path.is_relative_to(root):
            continue
        if path.is_dir() or not (path.is_file() or path.suffix):
            continue
        break
    else:
        # A missing configuration is itself a useful diagnostic at this path.
        path = next(
            (root / name for name in ("bernstein.yaml", "bernstein.yml") if (root / name).is_file()),
            root / "bernstein.yaml",
        )
    uri = quote(path.relative_to(root).as_posix(), safe="/")
    return {"physicalLocation": {"artifactLocation": {"uri": uri}}}


_LOCAL_PATH = re.compile(
    r"file://[^\s\"'<>]+|(?<![\w/])/(?!/)[^\s\"'<>]+|(?i:\b[a-z]:[\\/])[^\s\"'<>]+|\\\\[^\s\"'<>]+"
)


def _redact_local_paths(value: Any) -> Any:
    """Keep local paths from check diagnostics out of uploadable SARIF."""
    if isinstance(value, str):
        return _LOCAL_PATH.sub("[local path]", value)
    if isinstance(value, dict):
        return {key: _redact_local_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    return value


def _sarif(findings: list[Finding], root: Path) -> dict[str, Any]:
    """Describe checks and locations without counting passing checks as issues."""
    rules = [
        {"id": check_id, "shortDescription": {"text": check_id}} for check_id in dict.fromkeys(f.id for f in findings)
    ]
    rule_indices = {rule["id"]: index for index, rule in enumerate(rules)}
    results = []
    for finding in findings:
        if _measured_failure(finding):
            kind, level = "fail", "error"
        elif finding.verdict == Verdict.NOT_MEASURABLE:
            kind, level = "review", "none"
        elif finding.verdict == Verdict.DECLARED:
            kind, level = "informational", "none"
        else:
            kind, level = "pass", "none"
        message = finding.summary or finding.message or finding.reason or finding.id
        if finding.verdict == Verdict.NOT_MEASURABLE and finding.reason and finding.reason not in message:
            message = f"{message}: {finding.reason}"
        properties = {**_finding_dict(finding), "kind": _finding_kind(finding)}
        # Locators may embed absolute paths in producer-specific identifiers.
        properties["evidence"] = [{"sha256": evidence.sha256} for evidence in finding.evidence]
        results.append(
            {
                "ruleId": finding.id,
                "ruleIndex": rule_indices[finding.id],
                "kind": kind,
                "level": level,
                "message": {"text": message},
                "locations": [_sarif_location(finding, root)],
                "properties": properties,
            }
        )
    return _redact_local_paths(
        {
            "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "bernstein govern audit",
                            "informationUri": "https://github.com/sipyourdrink-ltd/bernstein",
                            "rules": rules,
                        }
                    },
                    "results": results,
                }
            ],
        }
    )


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


def _compare_audit_reports(root: Path, since: str, against: str, *, output_format: str, quiet: bool) -> None:
    from bernstein.cli.helpers import load_verify_only_key
    from bernstein.core.govern.audit_report import diff_reports, read_audit_report, verify_audit_report
    from bernstein.core.security.audit import AuditKeyPermissionError

    lineage_root = root / ".sdd" / "lineage"
    inputs = (("--since", since), ("--against", against))
    for option, digest in inputs:
        if re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", digest) is None:
            raise click.BadParameter("expected a full SHA-256 report hash", param_hint=option)

    reports = []
    try:
        for option, digest in inputs:
            report = read_audit_report(lineage_root, digest)
            if report is None:
                raise click.ClickException(f"Stored report for {option} was not found.")
            reports.append(report)

        key = load_verify_only_key()
        for (option, _), report in zip(inputs, reports, strict=True):
            verified = verify_audit_report(lineage_root=lineage_root, hmac_key=key, report=report)
            if not verified.ok:
                raise click.ClickException(f"Cannot verify report for {option}: {verified.reason}")
    except (OSError, AuditKeyPermissionError) as exc:
        raise click.ClickException(f"Cannot read audit report evidence: {exc}") from exc

    before, after = reports
    drift = diff_reports(before, after)
    if quiet:
        return
    if output_format == "json":
        click.echo(json.dumps([row.to_dict() for row in drift], indent=2))
        return
    for row in drift:
        click.echo(f"{row.finding_id}: {row.change}")
    if not drift:
        click.echo("No finding drift.")


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
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "sarif"]),
    default="text",
    show_default=True,
    help="Output format (SARIF 2.1.0 for code scanning).",
)
@click.option("--strict", is_flag=True, help="Exit 2 for unmeasurable checks unless a check fails or crashes (exit 1).")
@click.option("--quiet", is_flag=True, help="Print nothing; report the result only through the exit code.")
@click.option("--since", help="Compare from this stored audit report hash.")
@click.option("--against", help="Compare against this stored audit report hash.")
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
    output_format: str = "text",
    strict: bool = False,
    quiet: bool = False,
    registry: CheckRegistry | None = None,
    since: str | None = None,
    against: str | None = None,
) -> None:
    """Run registered governance audit checks across the workspace.

    Note: the verifier-key staleness check this command used to run now
    lives in ``bernstein govern audit-keys``.

    BERNSTEIN_GOVERN_AUDIT_SENTINEL=1 enables the harmless sentinel:failure
    selftest, journaling its run and dispatching a synthetic notification.

    Exit codes:
      0: Selected measured checks passed; declared findings never affect the code.
      1: A check failed/crashed, no checks matched, or not_measurable without --strict.
      2: A check is not measurable with --strict and no check failed or crashed,
         or invalid command-line arguments (Click usage error).

    All selected checks are required. Failures and crashes take precedence over
    strict incompleteness. Unexpected tool errors also fail closed with code 1.
    """
    if as_json and output_format == "sarif":
        raise click.UsageError("--json/--json-output cannot be combined with --format sarif")
    if as_json:
        output_format = "json"

    root = Path(workdir).resolve()
    if since is not None or against is not None:
        if since is None or against is None:
            raise click.UsageError("--since and --against must be supplied together")
        if list_checks or only_areas or skip_ids or strict or output_format == "sarif":
            raise click.UsageError("Report comparison supports text or JSON without check execution options")
        _compare_audit_reports(root, since, against, output_format=output_format, quiet=quiet)
        return

    reg = DEFAULT_REGISTRY if registry is None else registry
    populate_default_checks(reg)
    populate_installation_checks(reg)
    if list_checks and output_format == "sarif":
        raise click.UsageError("--list does not support --format sarif")

    if list_checks:
        if quiet:
            raise SystemExit(EXIT_OK)
        checks = reg.list_checks(only_areas=only_areas, skip_ids=skip_ids)
        if output_format == "json":
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

    from bernstein.core.checks.adapters import AuditSentinelCheck

    if AuditSentinelCheck().active and not quiet:
        click.echo(
            f"Sentinel active: {AuditSentinelCheck.env_var}=1 ({AuditSentinelCheck.check_id}); synthetic failure.",
            err=True,
        )
    execution_errors: list[str] = []
    findings = reg.run_all(
        workdir=root,
        only_areas=only_areas,
        skip_ids=skip_ids,
        on_error=execution_errors.append,
        log_errors=not quiet,
    )
    _record_sentinel_run(root, findings)
    exit_code = _exit_code(findings, strict=strict, execution_failed=bool(execution_errors))

    if quiet:
        raise SystemExit(exit_code)

    if output_format in ("json", "sarif"):
        report = _sarif(findings, root) if output_format == "sarif" else [_finding_dict(f) for f in findings]
        click.echo(json.dumps(report, indent=2))
        raise SystemExit(exit_code)

    if not findings:
        console.print("[bold red]No checks matched the specified selector(s).[/bold red]")
        raise SystemExit(exit_code)

    table = Table(title=f"Governance Audit Findings ({root.name})", show_header=True, header_style="bold cyan")
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Check ID", style="bold", no_wrap=True)
    table.add_column("Area", style="magenta")
    table.add_column("Summary / Diagnostic", style="white")

    for f in findings:
        if f.verdict == Verdict.DECLARED:
            status_badge = "[bold cyan]DECLARED[/bold cyan]"
        elif f.verdict == Verdict.NOT_MEASURABLE:
            status_badge = "[bold yellow]UNMEASURED[/bold yellow]"
        elif f.verdict == Verdict.PASS or f.passed:
            status_badge = "[bold green]PASS[/bold green]"
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

    passed_count = sum(
        1 for f in findings if f.verdict == Verdict.PASS or (f.verdict == Verdict.MEASURED and f.passed is True)
    )
    failed_count = sum(_measured_failure(f) for f in findings)
    declared_count = sum(f.verdict == Verdict.DECLARED for f in findings)
    unmeasured_count = sum(f.verdict == Verdict.NOT_MEASURABLE for f in findings)
    console.print(
        f"Total: [bold]{len(findings)}[/bold] | "
        f"Passed: [bold green]{passed_count}[/bold green] | "
        f"Failed: [bold red]{failed_count}[/bold red] | "
        f"Declared: [bold cyan]{declared_count}[/bold cyan] | "
        f"Unmeasurable: [bold yellow]{unmeasured_count}[/bold yellow]"
    )

    raise SystemExit(exit_code)
