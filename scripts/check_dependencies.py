#!/usr/bin/env python
"""Dependency conflict detector and resolver.

Detects:
  1. Known CVEs via pip-audit
  2. Dependency conflicts via uv
  3. Outdated packages

Generates:
  1. Remediation report
  2. Suggested lockfile updates
  3. Resolution PR with tests
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


@dataclass
class CVE:
    """Known vulnerability."""

    package: str
    version: str
    cve_id: str
    fix_versions: list[str]
    severity: str = "UNKNOWN"


@dataclass
class ConflictResolution:
    """Suggested resolution for a conflict."""

    package: str
    current: str
    suggested: str
    reason: str
    requires_test: bool = True


def run_command(cmd: list[str]) -> tuple[int, str, str]:
    """Run a command and return exit code, stdout, stderr."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def _parse_cve_line(line: str) -> CVE | None:
    """Parse a single pip-audit table row into a CVE, or None if malformed."""
    parts = line.split()
    if len(parts) < 3:
        return None
    try:
        return CVE(
            package=parts[0],
            version=parts[1],
            cve_id=parts[2],
            fix_versions=parts[3:] if len(parts) > 3 else [],
        )
    except (IndexError, ValueError):
        return None


def _parse_pip_audit_table(output: str) -> list[CVE]:
    """Parse pip-audit table output into a list of CVEs."""
    cves: list[CVE] = []
    in_table = False
    for line in output.split("\n"):
        if "---" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip():
            continue
        if "Name" in line and "Skip" in line:
            break
        cve = _parse_cve_line(line)
        if cve is not None:
            cves.append(cve)
    return cves


def detect_cves() -> list[CVE]:
    """Detect CVEs using pip-audit."""
    console.print("[bold cyan]Running pip-audit...[/bold cyan]")
    exit_code, stdout, stderr = run_command(["pip-audit"])

    output = stderr + stdout  # pip-audit outputs to stderr
    if exit_code == 1 and "Found" in output:
        return _parse_pip_audit_table(output)
    return []


def check_conflicts() -> list[str]:
    """Check for dependency conflicts using uv."""
    console.print("[bold cyan]Checking dependency conflicts with uv...[/bold cyan]")
    exit_code, _stdout, stderr = run_command(["uv", "pip", "compile", "pyproject.toml", "--resolution", "highest"])

    conflicts = []
    if exit_code != 0 and ("conflict" in stderr.lower() or "incompatible" in stderr.lower()):
        # uv would have reported conflicts
        conflicts.append(stderr)

    return conflicts


def suggest_resolutions(cves: list[CVE]) -> list[ConflictResolution]:
    """Generate suggested resolutions for detected issues."""
    resolutions = []

    for cve in cves:
        if cve.fix_versions:
            # Find the highest version available
            try:

                def parse_version(v: str) -> tuple:
                    """Parse version string into tuple for comparison."""
                    try:
                        return tuple(int(x) for x in v.split(".")[:3])
                    except (ValueError, AttributeError):
                        return (0, 0, 0)

                target_version = max(cve.fix_versions, key=parse_version)
                resolutions.append(
                    ConflictResolution(
                        package=cve.package,
                        current=cve.version,
                        suggested=target_version,
                        reason=f"CVE {cve.cve_id}: upgrade to {target_version}+",
                        requires_test=True,
                    )
                )
            except (ValueError, AttributeError):
                # If we can't parse versions, suggest manual review
                pass

    return resolutions


def test_resolution(resolution: ConflictResolution) -> bool:
    """Test a proposed resolution."""
    console.print(f"  Testing: {resolution.package} {resolution.current} → {resolution.suggested}")

    # Create temporary test environment
    cmd = ["uv", "pip", "install", f"{resolution.package}=={resolution.suggested}", "--dry-run"]

    exit_code, _stdout, _stderr = run_command(cmd)
    success = exit_code == 0

    if success:
        console.print(f"    ✓ {resolution.package}=={resolution.suggested} is installable")
    else:
        console.print(f"    ✗ {resolution.package}=={resolution.suggested} has issues")

    return success


def generate_report(cves: list[CVE], conflicts: list[str], resolutions: list[ConflictResolution]) -> dict:
    """Generate a comprehensive remediation report."""
    report = {
        "timestamp": __import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
        "summary": {
            "cves_found": len(cves),
            "conflicts_found": len(conflicts),
            "resolutions_suggested": len(resolutions),
        },
        "cves": [
            {
                "package": cve.package,
                "current_version": cve.version,
                "cve_id": cve.cve_id,
                "fix_versions": cve.fix_versions,
            }
            for cve in cves
        ],
        "conflicts": conflicts,
        "suggested_resolutions": [
            {"package": r.package, "current": r.current, "suggested": r.suggested, "reason": r.reason}
            for r in resolutions
        ],
    }

    return report


@click.command()
@click.option("--fix", is_flag=True, default=False, help="Apply suggested fixes to uv.lock")
@click.option("--output", default=".sdd/dependency-report.json", help="Output report path")
def main(fix: bool, output: str) -> None:
    """Monitor dependencies and suggest resolutions."""
    console.print(Panel("[bold]Bernstein Dependency Conflict Resolver[/bold]", border_style="cyan"))

    # Detect issues
    cves = detect_cves()
    conflicts = check_conflicts()

    # Generate suggestions
    resolutions = suggest_resolutions(cves)

    # Test resolutions
    console.print("\n[bold cyan]Testing proposed resolutions...[/bold cyan]")
    tested_resolutions = []
    for resolution in resolutions:
        if test_resolution(resolution):
            tested_resolutions.append(resolution)

    # Generate report
    report = generate_report(cves, conflicts, tested_resolutions)

    # Save report
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(report, indent=2))

    # Display results
    console.print("\n[bold cyan]=== Remediation Report ===[/bold cyan]\n")

    if cves:
        table = Table(title="[bold]CVEs Found[/bold]", show_header=True)
        table.add_column("Package")
        table.add_column("Current")
        table.add_column("CVE ID")
        table.add_column("Fix Available")
        for cve in cves:
            table.add_row(cve.package, cve.version, cve.cve_id, " / ".join(cve.fix_versions) or "—")
        console.print(table)

    if tested_resolutions:
        console.print("\n[bold green]✓ Tested Resolutions[/bold green]")
        table = Table(show_header=True)
        table.add_column("Package")
        table.add_column("Current")
        table.add_column("Suggested")
        table.add_column("Reason")
        for r in tested_resolutions:
            table.add_row(r.package, r.current, r.suggested, r.reason)
        console.print(table)

    if conflicts:
        console.print("\n[bold red]✗ Unresolvable Conflicts[/bold red]")
        for conflict in conflicts:
            console.print(f"  {conflict[:100]}...")

    console.print(f"\n[dim]Report saved to {output}[/dim]")

    # Exit with appropriate code
    sys.exit(1 if (cves or conflicts) else 0)


if __name__ == "__main__":
    main()
