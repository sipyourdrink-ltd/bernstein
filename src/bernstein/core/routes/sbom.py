"""SBOM (Software Bill of Materials) REST endpoints.

Provides on-demand SBOM generation and artifact listing for enterprise
supply-chain compliance workflows.

Endpoints
---------
POST /sbom/generate
    Generate a CycloneDX or SPDX SBOM from installed packages, optionally
    run vulnerability scanning, and enforce the configured gate.

GET /sbom/artifacts
    List previously generated SBOM artifact files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SBOMGenerateRequest(BaseModel):
    """Body for POST /sbom/generate."""

    sbom_format: str = Field(
        default="cyclonedx-json",
        description="Output format: 'cyclonedx-json' or 'spdx-json'.",
    )
    source: str = Field(
        default="pip",
        description="Package source label (pip, npm, requirements.txt, etc.).",
    )
    run_scan: bool = Field(
        default=True,
        description="Run vulnerability scanning (osv-scanner or grype) after generation.",
    )
    block_on_critical: bool = Field(
        default=True,
        description="Raise 422 when critical vulnerabilities are found.",
    )


class SBOMVulnFindingResponse(BaseModel):
    """Serialised vulnerability finding."""

    component_name: str
    component_version: str
    vuln_id: str
    severity: str
    summary: str
    fix_version: str
    scanner: str


class SBOMScanResultResponse(BaseModel):
    """Serialised scan result."""

    scanner: str
    finding_count: int
    highest_severity: str
    findings: list[SBOMVulnFindingResponse]
    errors: list[str]
    passed_gate: bool


class SBOMGenerateResponse(BaseModel):
    """Response from POST /sbom/generate."""

    serial_number: str
    sbom_format: str
    component_count: int
    artifact_path: str
    scan_result: SBOMScanResultResponse | None = None


class SBOMArtifactEntry(BaseModel):
    """A single SBOM artifact file entry."""

    filename: str
    path: str
    size_bytes: int


class SBOMListResponse(BaseModel):
    """Response from GET /sbom/artifacts."""

    artifacts: list[SBOMArtifactEntry]
    artifact_dir: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_workdir(request: Request) -> Path:
    workdir: Path | None = getattr(request.app.state, "workdir", None)
    if workdir is None:
        raise HTTPException(status_code=503, detail="Server workdir not configured")
    return workdir


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/sbom/generate",
    response_model=SBOMGenerateResponse,
    responses={
        422: {"description": "Critical vulnerabilities found (gate blocked)"},
        503: {"description": "Server workdir not configured"},
    },
    summary="Generate SBOM and optionally run vulnerability scan",
    tags=["sbom"],
)
async def generate_sbom(body: SBOMGenerateRequest, request: Request) -> SBOMGenerateResponse:
    """Generate a CycloneDX or SPDX SBOM from installed packages.

    After generation, optionally run ``osv-scanner`` or ``grype`` for
    vulnerability scanning.  When ``block_on_critical=true`` and critical
    findings are detected, responds with HTTP 422 so CI/CD pipelines can
    gate merges on vulnerability status.

    SBOM artifacts are written to ``.sdd/artifacts/sbom/``.
    """
    from bernstein.core.sbom import (
        SBOMFormat,
        SBOMGateError,
        SBOMGenerator,
        SBOMVulnerabilityGate,
        SBOMVulnSeverity,
    )

    workdir = _get_workdir(request)

    try:
        fmt = SBOMFormat(body.sbom_format)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sbom_format {body.sbom_format!r}. Use 'cyclonedx-json' or 'spdx-json'.",
        ) from None

    generator = SBOMGenerator(workdir, sbom_format=fmt)
    sbom = generator.generate(source=body.source)
    artifact_path = generator.save(sbom)

    logger.info("SBOM generated: %s (%d components)", artifact_path, len(sbom.components))

    scan_response: SBOMScanResultResponse | None = None
    if body.run_scan:
        scan_result = generator.scan(sbom)
        gate = SBOMVulnerabilityGate(
            block_on=[SBOMVulnSeverity.CRITICAL] if body.block_on_critical else []
        )
        passed = gate.passes(scan_result)

        scan_response = SBOMScanResultResponse(
            scanner=scan_result.scanner,
            finding_count=len(scan_result.findings),
            highest_severity=scan_result.highest_severity.value,
            findings=[
                SBOMVulnFindingResponse(
                    component_name=f.component_name,
                    component_version=f.component_version,
                    vuln_id=f.vuln_id,
                    severity=f.severity.value,
                    summary=f.summary,
                    fix_version=f.fix_version,
                    scanner=f.scanner,
                )
                for f in scan_result.findings
            ],
            errors=scan_result.errors,
            passed_gate=passed,
        )

        if body.block_on_critical and not passed:
            logger.warning(
                "SBOM gate blocked for artifact %s: %d critical finding(s)",
                artifact_path,
                sum(1 for f in scan_result.findings if f.severity == SBOMVulnSeverity.CRITICAL),
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "SBOM vulnerability gate blocked: critical findings detected",
                    "finding_count": len(scan_result.findings),
                    "highest_severity": scan_result.highest_severity.value,
                    "findings": [f.to_dict() for f in scan_result.findings if f.severity == SBOMVulnSeverity.CRITICAL],
                },
            )

    return SBOMGenerateResponse(
        serial_number=sbom.serial_number,
        sbom_format=sbom.sbom_format.value,
        component_count=len(sbom.components),
        artifact_path=str(artifact_path),
        scan_result=scan_response,
    )


@router.get(
    "/sbom/artifacts",
    response_model=SBOMListResponse,
    responses={503: {"description": "Server workdir not configured"}},
    summary="List generated SBOM artifact files",
    tags=["sbom"],
)
async def list_sbom_artifacts(request: Request) -> SBOMListResponse:
    """List previously generated SBOM artifact files from ``.sdd/artifacts/sbom/``."""
    workdir = _get_workdir(request)
    artifact_dir = workdir / ".sdd" / "artifacts" / "sbom"

    artifacts: list[SBOMArtifactEntry] = []
    if artifact_dir.exists():
        for entry in sorted(artifact_dir.iterdir()):
            if entry.is_file() and entry.suffix == ".json":
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                artifacts.append(
                    SBOMArtifactEntry(
                        filename=entry.name,
                        path=str(entry),
                        size_bytes=size,
                    )
                )

    return SBOMListResponse(
        artifacts=artifacts,
        artifact_dir=str(artifact_dir),
    )
