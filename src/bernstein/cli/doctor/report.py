"""DoctorResult dataclass and Rich-based report renderer.

Every doctor check returns a :class:`DoctorResult`. The renderer collects
results into one Rich table with status glyphs, then prints a one-line
summary footer and computes the exit code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from rich.console import Console


DoctorStatus = Literal["ok", "warn", "fail", "skip"]
DoctorCategory = Literal["installation", "adapter", "network", "environment"]


# Public mapping from status to a (glyph, color) tuple. Used by the renderer
# and also by tests that need the exact display string.
STATUS_GLYPHS: dict[str, tuple[str, str]] = {
    "ok": ("✓", "green"),
    "warn": ("⚠", "yellow"),
    "fail": ("✗", "red"),
    "skip": ("○", "dim"),
}


@dataclass(frozen=True)
class DoctorResult:
    """A single doctor check result.

    Attributes:
        name: Stable identifier for the check (for example ``adapter:claude``).
        category: One of ``installation``, ``adapter``, ``network``,
            ``environment``.
        status: One of ``ok``, ``warn``, ``fail``, ``skip``.
        detail: Short human-readable detail line.
        remediation: Optional remediation hint. Empty for passing checks.
    """

    name: str
    category: DoctorCategory
    status: DoctorStatus
    detail: str
    remediation: str = ""
    extra: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def summarize(results: Sequence[DoctorResult]) -> dict[str, int]:
    """Count results by status. Always returns all four keys."""
    counts: dict[str, int] = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def exit_code_for(results: Sequence[DoctorResult]) -> int:
    """Return the process exit code for a result set.

    ``1`` if any FAIL is present, ``0`` otherwise. WARN does not change the
    exit code (the renderer surfaces it via stderr instead).
    """
    return 1 if any(r.status == "fail" for r in results) else 0


def render_report(
    results: Sequence[DoctorResult],
    console: Console | None = None,
    *,
    show_skip: bool = True,
) -> str:
    """Render results to a Rich table and return the captured plain text.

    A console is created on-demand when one is not supplied. The captured
    string lets tests assert on the rendered output without relying on
    terminal width quirks.
    """
    from io import StringIO

    from rich.console import Console
    from rich.table import Table

    buf = StringIO()
    target = console or Console(file=buf, width=120, record=True, color_system=None)
    table = Table(
        title="Bernstein Doctor",
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("Check", min_width=18, overflow="fold")
    table.add_column("Category", min_width=10)
    table.add_column("Status", min_width=6)
    table.add_column("Detail", min_width=24, overflow="fold")
    table.add_column("Remediation", overflow="fold")

    for r in results:
        if not show_skip and r.status == "skip":
            continue
        glyph, color = STATUS_GLYPHS.get(r.status, ("?", "white"))
        table.add_row(
            r.name,
            r.category,
            f"[{color}]{glyph} {r.status.upper()}[/{color}]",
            r.detail,
            r.remediation,
        )

    target.print(table)
    counts = summarize(results)
    footer = (
        f"[green]{counts['ok']} OK[/green]  "
        f"[yellow]{counts['warn']} WARN[/yellow]  "
        f"[red]{counts['fail']} FAIL[/red]  "
        f"[dim]{counts['skip']} SKIP[/dim]"
    )
    target.print(footer)
    return target.export_text() if console is None else buf.getvalue()


async def run_all(
    *,
    adapter_names: Iterable[str] | None = None,
    provider_names: Iterable[str] | None = None,
    audit_dir: Path | None = None,
) -> list[DoctorResult]:
    """Run every doctor category and return the merged result list.

    The four categories run in parallel where safe. Installation checks
    remain synchronous because they shell out trivially and complete
    instantly.

    Args:
        adapter_names: Restrict the adapter probes.
        provider_names: Restrict the network probes.
        audit_dir: Audit directory to check the append lock against. Defaults
            to ``.sdd/audit`` under the working directory.
    """
    from bernstein.cli.doctor.adapter_checks import run_adapter_checks
    from bernstein.cli.doctor.audit_lock_checks import check_audit_lock_filesystem
    from bernstein.cli.doctor.environment_checks import run_environment_checks
    from bernstein.cli.doctor.network_checks import run_network_checks
    from bernstein.cli.install_check import check_installations

    install_results = _installation_to_doctor(check_installations())
    env_results = run_environment_checks()
    env_results.append(check_audit_lock_filesystem(audit_dir or Path.cwd() / ".sdd" / "audit"))

    adapter_task = asyncio.create_task(run_adapter_checks(adapter_names))
    network_task = asyncio.create_task(run_network_checks(provider_names))

    adapter_results, network_results = await asyncio.gather(
        adapter_task,
        network_task,
        return_exceptions=False,
    )

    return [
        *install_results,
        *adapter_results,
        *network_results,
        *env_results,
    ]


def _installation_to_doctor(install_results: object) -> list[DoctorResult]:
    """Adapt legacy InstallWarning objects to DoctorResult."""
    from typing import cast

    results: list[DoctorResult] = []
    if not isinstance(install_results, list):  # pragma: no cover - defensive
        return results
    items = cast("list[object]", install_results)
    for warning in items:
        ok = bool(getattr(warning, "ok", False))
        name = str(getattr(warning, "name", "unknown"))
        detail = str(getattr(warning, "detail", ""))
        fix = str(getattr(warning, "fix", ""))
        results.append(
            DoctorResult(
                name=name,
                category="installation",
                status="ok" if ok else "fail",
                detail=detail,
                remediation=fix,
            )
        )
    return results
