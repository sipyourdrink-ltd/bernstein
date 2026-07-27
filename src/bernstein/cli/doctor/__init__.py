"""Bernstein doctor sub-package - extended diagnostic checks.

Splits the doctor into four categories so failures are easy to locate:

- ``installation_checks`` re-exports :mod:`bernstein.cli.install_check` so
  the legacy install-mismatch detection runs unchanged.
- ``adapter_checks`` probes every CLI adapter binary referenced in
  ``bernstein.yaml`` for PATH presence and ``--version`` output.
- ``network_checks`` opens TCP/443 connections to provider endpoints with
  a short timeout. Honors ``BERNSTEIN_OFFLINE=1``.
- ``environment_checks`` detects GitHub Actions, GitLab CI, Buildkite,
  Docker, devcontainers and systemd-run sandboxes.

Every check returns a :class:`DoctorResult` so the report renderer can
display them in a single Rich table.
"""

from __future__ import annotations

from bernstein.cli import install_check as installation_checks
from bernstein.cli.doctor.adapter_checks import (
    ADAPTER_BINARIES,
    check_adapter_binary,
    run_adapter_checks,
)
from bernstein.cli.doctor.audit_lock_checks import (
    UNRELIABLE_FILESYSTEMS,
    check_audit_lock_filesystem,
    resolve_filesystem_type,
)
from bernstein.cli.doctor.environment_checks import (
    ENVIRONMENT_PROBES,
    detect_environments,
    run_environment_checks,
)
from bernstein.cli.doctor.network_checks import (
    PROVIDER_HOSTS,
    check_provider_reachability,
    run_network_checks,
)
from bernstein.cli.doctor.report import (
    STATUS_GLYPHS,
    DoctorResult,
    DoctorStatus,
    exit_code_for,
    render_report,
    run_all,
    summarize,
)
from bernstein.cli.doctor.suggest_docs import (
    DEFAULT_TOP_N,
    UnansweredTopic,
    format_topic_line,
    hint_line,
    load_unanswered_topics,
    render_suggestions,
    top_n_topics,
)

__all__ = [
    "ADAPTER_BINARIES",
    "DEFAULT_TOP_N",
    "ENVIRONMENT_PROBES",
    "PROVIDER_HOSTS",
    "STATUS_GLYPHS",
    "UNRELIABLE_FILESYSTEMS",
    "DoctorResult",
    "DoctorStatus",
    "UnansweredTopic",
    "check_adapter_binary",
    "check_audit_lock_filesystem",
    "check_provider_reachability",
    "detect_environments",
    "exit_code_for",
    "format_topic_line",
    "hint_line",
    "installation_checks",
    "load_unanswered_topics",
    "render_report",
    "render_suggestions",
    "resolve_filesystem_type",
    "run_adapter_checks",
    "run_all",
    "run_environment_checks",
    "run_network_checks",
    "summarize",
    "top_n_topics",
]
