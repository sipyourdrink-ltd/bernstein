"""SOC 2 evidence checklist generator with real run-log integration.

The historical SOC 2 surface (``bernstein audit export --period``) emits
the JSON evidence package consumed by :mod:`bernstein.core.security.compliance`
and :mod:`bernstein.core.security.soc2_report`. Both stop short of giving
auditors a *human-readable* checklist where each Trust Service Criteria
control is paired with the concrete file or hash that proves it.

This module provides that checklist:

* :class:`EvidenceSource` - declarative pointer from a SOC 2 control to
  the on-disk artefact that backs it (audit-chain HMAC tail, credential-
  scoping policy, capability-matrix CI run, cluster-TLS cert validation
  log, wheelhouse verification result).
* :func:`resolve_evidence_sources` - walks the project root, materialises
  each source into a path or sha256 reference, and records freshness so
  the markdown can flag stale evidence (last-modified > N days).
* :func:`generate_audit_pack` - renders the checklist as Markdown,
  one row per (control, source).

The markdown is self-contained so it can be uploaded as a CI artifact
or pasted into an external GRC tool without further processing.

Wired into the CLI as ``bernstein audit pack --soc2 [--include-runs <since>]``
(see :mod:`bernstein.cli.commands.audit_cmd`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: How an evidence source materialises on disk. Drives the rendering
#: branch in :func:`generate_audit_pack`.
SourceKind = Literal[
    "audit_chain",
    "policy_file",
    "capability_matrix",
    "tls_cert_log",
    "wheelhouse_verify",
    "run_log",
]

#: Markdown stamp used when a source is configured but the artefact is
#: not present on disk yet (the operator hasn't run the relevant flow).
PENDING_EVIDENCE: str = "evidence: pending - artefact not produced"

#: Resolution status of a single evidence source.
EvidenceStatus = Literal["OK", "PENDING", "STALE"]

#: Pretty-printed status markers to keep the checklist scannable in
#: terminal output and rendered Markdown alike.
STATUS_OK: EvidenceStatus = "OK"
STATUS_PENDING: EvidenceStatus = "PENDING"
STATUS_STALE: EvidenceStatus = "STALE"

#: Default freshness window in days. Anything older flips a source to
#: ``STALE``. Operators tune via :func:`generate_audit_pack(stale_after_days=N)`.
DEFAULT_STALE_AFTER_DAYS: int = 30


# ---------------------------------------------------------------------------
# Declarative source definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """Pointer from a SOC 2 control to the artefact that backs it.

    Attributes:
        control_id: TSC identifier (e.g. ``CC6.1``).
        kind: Materialisation strategy. Drives the resolver.
        relpath: Path inside the project root where the artefact lives.
            Resolvers may treat this as a glob root (e.g. for capability-
            matrix runs that emit timestamped JSON per CI run).
        description: Operator-facing description of what proves the
            control. Lands verbatim in the markdown row.
    """

    control_id: str
    kind: SourceKind
    relpath: str
    description: str


@dataclass(frozen=True, slots=True)
class ResolvedEvidence:
    """A materialised :class:`EvidenceSource`.

    Attributes:
        source: The originating declaration.
        status: ``OK`` / ``PENDING`` / ``STALE``.
        evidence_ref: Either a relative path on disk or
            ``"sha256:<digest>"`` when the source is content-addressed.
        last_modified: Unix mtime of the resolved artefact (``0.0`` when
            pending).
        details: Free-form structured data - e.g. wheelhouse counts, the
            HMAC chain tail digest - that downstream renderers can show
            without re-reading disk.
    """

    source: EvidenceSource
    status: EvidenceStatus
    evidence_ref: str
    last_modified: float = 0.0
    details: dict[str, Any] = field(default_factory=dict[str, Any])


# Registry: each row binds one TSC control to one concrete source. Multiple
# rows per control is allowed (e.g. CC6.1 has both an audit-chain and a
# credential-scoping policy proof).
DEFAULT_EVIDENCE_SOURCES: tuple[EvidenceSource, ...] = (
    EvidenceSource(
        control_id="CC1.1",
        kind="policy_file",
        relpath="docs/security/CODE_OF_CONDUCT_REVIEW.md",
        description="Demonstrates board-level commitment to integrity (CoC review minutes).",
    ),
    EvidenceSource(
        control_id="CC1.2",
        kind="policy_file",
        relpath="CODE_OF_CONDUCT.md",
        description="Code of conduct binding everyone with a Bernstein account.",
    ),
    EvidenceSource(
        control_id="CC2.1",
        kind="audit_chain",
        relpath=".sdd/audit",
        description="HMAC-chained audit log proves communication of internal control responsibilities.",
    ),
    EvidenceSource(
        control_id="CC6.1",
        kind="policy_file",
        relpath="src/bernstein/core/credential_scoping.py",
        description="Credential scoping policy proves least-privilege API key issuance (logical access).",
    ),
    EvidenceSource(
        control_id="CC6.1",
        kind="audit_chain",
        relpath=".sdd/audit",
        description="HMAC-chained audit log of every privileged operation.",
    ),
    EvidenceSource(
        control_id="CC6.6",
        kind="tls_cert_log",
        relpath=".sdd/runtime/cluster_tls",
        description="Cluster mTLS cert validation log proves boundary protection (cluster_tls.py).",
    ),
    EvidenceSource(
        control_id="CC6.7",
        kind="capability_matrix",
        relpath=".sdd/runtime/spawn_capabilities",
        description="Capability matrix run results prove lethal-trifecta enforcement (transmission integrity).",
    ),
    EvidenceSource(
        control_id="CC6.8",
        kind="wheelhouse_verify",
        relpath=".sdd/runtime/wheelhouse",
        description="bernstein verify wheelhouse - supply-chain integrity attestation for installed wheels.",
    ),
    EvidenceSource(
        control_id="CC7.2",
        kind="audit_chain",
        relpath=".sdd/audit",
        description="System monitoring proof - every event is HMAC-chained for tamper-evidence.",
    ),
    EvidenceSource(
        control_id="CC7.4",
        kind="run_log",
        relpath=".sdd/runtime",
        description="Recent orchestrator runs - proves incident response activity is recorded.",
    ),
)


# ---------------------------------------------------------------------------
# Resolvers - one per :class:`SourceKind`
# ---------------------------------------------------------------------------


def _hmac_chain_tail_digest(audit_dir: Path) -> tuple[str, float, dict[str, Any]]:
    """Return ``(sha256_ref, mtime, details)`` for the audit chain tail.

    The chain tail HMAC is a single content hash that summarises the
    entire log - quoting it in the evidence pack lets an auditor pin a
    specific tail without shipping the full chain inline.
    """
    if not audit_dir.is_dir():
        return "", 0.0, {}

    log_files = sorted(audit_dir.glob("*.jsonl"))
    if not log_files:
        return "", 0.0, {"reason": "no audit log files present"}

    last = log_files[-1]
    last_mtime = last.stat().st_mtime
    tail_hmac = ""
    line_count = 0
    for line in last.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        line_count += 1
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and "hmac" in entry:
            tail_hmac = str(entry["hmac"])
    if not tail_hmac:
        return "", last_mtime, {"file": last.name, "lines": line_count, "reason": "no hmac field found"}

    return (
        f"sha256:{tail_hmac}",
        last_mtime,
        {"file": last.name, "lines": line_count, "tail_hmac": tail_hmac[:16] + "…"},
    )


def _policy_file_digest(policy_path: Path) -> tuple[str, float, dict[str, Any]]:
    """Return content-hash + mtime for a static policy file."""
    if not policy_path.is_file():
        return "", 0.0, {}
    body = policy_path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"sha256:{digest}",
        policy_path.stat().st_mtime,
        {"size_bytes": len(body), "lines": body.count(b"\n") + 1},
    )


def _capability_matrix_summary(runtime_dir: Path) -> tuple[str, float, dict[str, Any]]:
    """Summarise capability-matrix CI runs into one evidence reference."""
    if not runtime_dir.is_dir():
        return "", 0.0, {}
    run_files = sorted(runtime_dir.glob("*.json"))
    if not run_files:
        return "", 0.0, {"reason": "no capability runs recorded"}
    latest = run_files[-1]
    body = latest.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    return (
        f"sha256:{digest}",
        latest.stat().st_mtime,
        {
            "file": latest.name,
            "run_count": len(run_files),
            "latest_tools": parsed.get("tools", []),
            "violations": parsed.get("violations", []),
        },
    )


def _tls_cert_log_summary(tls_dir: Path) -> tuple[str, float, dict[str, Any]]:
    """Summarise cluster-TLS cert validation log files."""
    if not tls_dir.is_dir():
        return "", 0.0, {}
    log_files = sorted(tls_dir.glob("*.log")) + sorted(tls_dir.glob("*.json"))
    if not log_files:
        return "", 0.0, {"reason": "no cluster_tls validation logs"}
    latest = log_files[-1]
    body = latest.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"sha256:{digest}",
        latest.stat().st_mtime,
        {"file": latest.name, "log_count": len(log_files)},
    )


def _wheelhouse_verify_summary(wheelhouse_dir: Path) -> tuple[str, float, dict[str, Any]]:
    """Summarise the wheelhouse verify result."""
    if not wheelhouse_dir.is_dir():
        return "", 0.0, {}
    verify_files = sorted(wheelhouse_dir.glob("verify*.json"))
    if not verify_files:
        return "", 0.0, {"reason": "no verify*.json file produced"}
    latest = verify_files[-1]
    body = latest.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    details: dict[str, Any] = {"file": latest.name}
    if isinstance(parsed, dict):
        details["wheels_checked"] = parsed.get("wheels_checked")
        details["all_valid"] = parsed.get("all_valid")
    return f"sha256:{digest}", latest.stat().st_mtime, details


def _run_log_summary(
    runtime_dir: Path,
    *,
    include_since: datetime | None,
) -> tuple[str, float, dict[str, Any]]:
    """Aggregate recent runs into one evidence reference.

    When ``include_since`` is set, only count runs newer than that
    timestamp. The reference is a content hash over the sorted list of
    run-id + mtime pairs so two re-runs of the generator over the same
    inputs produce the same evidence ref.
    """
    if not runtime_dir.is_dir():
        return "", 0.0, {}

    cutoff = include_since.timestamp() if include_since else 0.0
    audit_dir = runtime_dir / "audit"
    runs: list[tuple[str, float]] = []
    if audit_dir.is_dir():
        for path in sorted(audit_dir.glob("*.audit.jsonl")):
            mtime = path.stat().st_mtime
            if mtime < cutoff:
                continue
            run_id = path.name.removesuffix(".audit.jsonl")
            runs.append((run_id, mtime))

    if not runs:
        return "", 0.0, {"reason": "no run audit slices in window"}

    summary_payload = json.dumps(
        [{"run_id": r, "mtime": round(m, 3)} for r, m in runs],
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(summary_payload).hexdigest()
    latest_mtime = max(m for _, m in runs)
    return (
        f"sha256:{digest}",
        latest_mtime,
        {
            "run_count": len(runs),
            "latest_run_id": runs[-1][0],
            "since": include_since.isoformat() if include_since else None,
        },
    )


def _resolve_one(
    source: EvidenceSource,
    *,
    workdir: Path,
    include_since: datetime | None,
    stale_after_days: int,
    now_ts: float,
) -> ResolvedEvidence:
    """Materialise a single :class:`EvidenceSource` against *workdir*."""
    target = (workdir / source.relpath).resolve()
    if source.kind == "audit_chain":
        ref, mtime, details = _hmac_chain_tail_digest(target)
    elif source.kind == "policy_file":
        ref, mtime, details = _policy_file_digest(target)
    elif source.kind == "capability_matrix":
        ref, mtime, details = _capability_matrix_summary(target)
    elif source.kind == "tls_cert_log":
        ref, mtime, details = _tls_cert_log_summary(target)
    elif source.kind == "wheelhouse_verify":
        ref, mtime, details = _wheelhouse_verify_summary(target)
    elif source.kind == "run_log":
        ref, mtime, details = _run_log_summary(target, include_since=include_since)
    else:  # pragma: no cover - Literal exhaustively covered above
        ref, mtime, details = "", 0.0, {"reason": f"unknown source kind {source.kind!r}"}

    if not ref:
        return ResolvedEvidence(
            source=source,
            status=STATUS_PENDING,
            evidence_ref=PENDING_EVIDENCE,
            details=details,
        )

    age_days = (now_ts - mtime) / 86400.0
    if age_days > stale_after_days:
        details = details | {"age_days": round(age_days, 1)}
        return ResolvedEvidence(
            source=source,
            status=STATUS_STALE,
            evidence_ref=f"evidence: {ref} (stale, age={age_days:.1f}d)",
            last_modified=mtime,
            details=details,
        )

    # Either a relative path or a content-addressed reference. Prefer the
    # file path when the source kind has one - content hashes alone are
    # opaque without the file location.
    pretty = f"evidence: {source.relpath} ({ref})"
    return ResolvedEvidence(
        source=source,
        status=STATUS_OK,
        evidence_ref=pretty,
        last_modified=mtime,
        details=details,
    )


def resolve_evidence_sources(
    sources: Iterable[EvidenceSource] = DEFAULT_EVIDENCE_SOURCES,
    *,
    workdir: Path | None = None,
    include_since: datetime | None = None,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    now: datetime | None = None,
) -> list[ResolvedEvidence]:
    """Resolve every declared :class:`EvidenceSource` against the project.

    Args:
        sources: Iterable of source declarations.
        workdir: Project root. Defaults to ``Path.cwd()``.
        include_since: When set, run-log resolvers filter to runs newer
            than this datetime. Other resolvers ignore the value.
        stale_after_days: Mark sources whose mtime is older than this
            window as ``STALE``. Default 30 days.
        now: Override clock for tests.

    Returns:
        Resolved entries in declaration order.
    """
    base = (workdir or Path.cwd()).resolve()
    now_dt = now or datetime.now(tz=UTC)
    now_ts = now_dt.timestamp()
    return [
        _resolve_one(
            src,
            workdir=base,
            include_since=include_since,
            stale_after_days=stale_after_days,
            now_ts=now_ts,
        )
        for src in sources
    ]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _format_status(resolved: ResolvedEvidence) -> str:
    """Format the status column with a literal label (no emoji)."""
    return f"`{resolved.status}`"


def _format_details(details: dict[str, Any]) -> str:
    """Render structured details as a one-line key=value summary."""
    if not details:
        return ""
    parts = [f"{k}={details[k]!r}" for k in sorted(details)]
    return "; ".join(parts)


def render_markdown(
    resolved: list[ResolvedEvidence],
    *,
    period_label: str,
    generated_at: datetime | None = None,
) -> str:
    """Render the resolved evidence list as a SOC 2 checklist Markdown.

    Args:
        resolved: Output of :func:`resolve_evidence_sources`.
        period_label: Human-readable label for the reporting period.
        generated_at: Override clock for tests.

    Returns:
        Markdown body (newline-terminated).
    """
    ts = (generated_at or datetime.now(tz=UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        "# SOC 2 Evidence Checklist",
        "",
        f"_Period: {period_label} | Generated: {ts}_",
        "",
        "## Controls",
        "",
        "| Control | Status | Evidence | Description |",
        "| --- | --- | --- | --- |",
    ]
    for entry in resolved:
        details = _format_details(entry.details)
        evidence_cell = entry.evidence_ref
        if details:
            evidence_cell = f"{entry.evidence_ref} <br/>_{details}_"
        lines.append(
            f"| {entry.source.control_id} | {_format_status(entry)} | {evidence_cell} | {entry.source.description} |",
        )
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Sources resolved: **{len(resolved)}**",
            f"- OK: **{sum(1 for r in resolved if r.status == STATUS_OK)}**",
            f"- Pending: **{sum(1 for r in resolved if r.status == STATUS_PENDING)}**",
            f"- Stale: **{sum(1 for r in resolved if r.status == STATUS_STALE)}**",
            "",
        ],
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point - used by the CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditPackResult:
    """Output of :func:`generate_audit_pack`.

    Attributes:
        markdown: Rendered checklist body.
        markdown_path: On-disk path to the saved markdown (``None`` when
            ``write=False``).
        manifest: JSON manifest with one row per resolved evidence
            source - useful for machine-parseable downstream tooling
            (CI artefacts, Vanta/Drata sync, etc.).
        manifest_path: On-disk path to the saved manifest.
        resolved: The resolved evidence list.
    """

    markdown: str
    markdown_path: Path | None
    manifest: dict[str, Any]
    manifest_path: Path | None
    resolved: list[ResolvedEvidence]


def _build_manifest(
    resolved: list[ResolvedEvidence],
    *,
    period_label: str,
    generated_at: datetime,
) -> dict[str, Any]:
    """Compose the JSON manifest companion to the markdown."""
    return {
        "schema_version": 1,
        "report_type": "soc2_evidence_pack",
        "period": period_label,
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence": [
            {
                "control_id": entry.source.control_id,
                "kind": entry.source.kind,
                "relpath": entry.source.relpath,
                "description": entry.source.description,
                "status": entry.status,
                "evidence_ref": entry.evidence_ref,
                "last_modified": entry.last_modified,
                "details": entry.details,
            }
            for entry in resolved
        ],
    }


def generate_audit_pack(
    *,
    workdir: Path | None = None,
    output_dir: Path | None = None,
    period_label: str = "current",
    sources: Iterable[EvidenceSource] = DEFAULT_EVIDENCE_SOURCES,
    include_since: datetime | None = None,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    now: datetime | None = None,
    write: bool = True,
) -> AuditPackResult:
    """Generate the SOC 2 evidence-checklist pack.

    Args:
        workdir: Project root. Defaults to ``Path.cwd()``.
        output_dir: Where to write the markdown + manifest. Defaults to
            ``<workdir>/.sdd/evidence/soc2/``.
        period_label: Human label for the reporting window.
        sources: Override the default registry (test/extension hook).
        include_since: When set, ``run_log`` resolvers filter by this.
        stale_after_days: Threshold for the ``STALE`` flag.
        now: Override clock for tests.
        write: When False, return rendered content without touching disk.

    Returns:
        :class:`AuditPackResult`.
    """
    base = (workdir or Path.cwd()).resolve()
    generated_at = now or datetime.now(tz=UTC)
    resolved = resolve_evidence_sources(
        sources,
        workdir=base,
        include_since=include_since,
        stale_after_days=stale_after_days,
        now=generated_at,
    )
    markdown = render_markdown(resolved, period_label=period_label, generated_at=generated_at)
    manifest = _build_manifest(resolved, period_label=period_label, generated_at=generated_at)

    md_path: Path | None = None
    manifest_path: Path | None = None
    if write:
        target_dir = (output_dir or base / ".sdd" / "evidence" / "soc2").resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        md_path = target_dir / f"soc2-evidence-{period_label}.md"
        manifest_path = target_dir / f"soc2-evidence-{period_label}.json"
        md_path.write_text(markdown, encoding="utf-8")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        logger.info("SOC 2 evidence pack written: %s", md_path)

    return AuditPackResult(
        markdown=markdown,
        markdown_path=md_path,
        manifest=manifest,
        manifest_path=manifest_path,
        resolved=resolved,
    )


__all__ = [
    "DEFAULT_EVIDENCE_SOURCES",
    "DEFAULT_STALE_AFTER_DAYS",
    "PENDING_EVIDENCE",
    "STATUS_OK",
    "STATUS_PENDING",
    "STATUS_STALE",
    "AuditPackResult",
    "EvidenceSource",
    "ResolvedEvidence",
    "SourceKind",
    "generate_audit_pack",
    "render_markdown",
    "resolve_evidence_sources",
]
