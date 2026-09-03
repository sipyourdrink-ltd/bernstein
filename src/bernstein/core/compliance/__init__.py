"""Compliance subpackage.

Houses the EU AI Act Article 12 evidence pack (``pack.py``) and renderer
(``article12.py``).

Back-compat: legacy callers reach for ``from bernstein.core.compliance
import ComplianceConfig`` etc. Those symbols still live in
``bernstein.core.security.compliance``; this package re-exports them so
the dotted import path keeps working even though
``bernstein.core.compliance`` is now a real package (which shadows the
``_CoreRedirectFinder`` redirect from ``core/__init__.py``).
"""

from __future__ import annotations

from bernstein.core.compliance.ai_bom import (
    AIBOM,
    BOM_SCHEMA_URL,
    BOM_SCHEMA_VERSION,
    SUPPORTED_FORMATS,
    AdapterEntry,
    BOMError,
    BOMVerificationReport,
    DataSourceEntry,
    ModelEntry,
    PromptEntry,
    ToolEntry,
    bom_content_hash,
    encode_bom,
    generate_bom,
    verify_bom,
)
from bernstein.core.compliance.article12 import (
    ARTICLE12_PARAGRAPH_MAP,
    CSV_FIELDS,
    ParagraphFn,
    render_csv,
    render_pdf,
)
from bernstein.core.compliance.coverage import (
    CONTROL_EVENT_MAP,
    ControlCoverageResult,
    ControlCoverageStatus,
    get_required_events,
)
from bernstein.core.compliance.pack import build_pack

# Re-export legacy names so ``from bernstein.core.compliance import X``
# keeps working for everything that previously resolved through the
# _CoreRedirectFinder shim.
from bernstein.core.security.compliance import (
    ComplianceConfig,
    CompliancePreset,
    SBOMEntry,
    ai_label_for_file,
    compliance_prerequisite_summary,
    export_evidence_bundle,
    export_soc2_package,
    generate_sbom,
    load_compliance_config,
    parse_period,
    persist_compliance_config,
    resolve_compliance_config,
)

__all__ = [
    "AIBOM",
    "ARTICLE12_PARAGRAPH_MAP",
    "BOM_SCHEMA_URL",
    "BOM_SCHEMA_VERSION",
    "CONTROL_EVENT_MAP",
    "CSV_FIELDS",
    "SUPPORTED_FORMATS",
    "AdapterEntry",
    "BOMError",
    "BOMVerificationReport",
    "ComplianceConfig",
    "CompliancePreset",
    "ControlCoverageResult",
    "ControlCoverageStatus",
    "DataSourceEntry",
    "ModelEntry",
    "ParagraphFn",
    "PromptEntry",
    "SBOMEntry",
    "ToolEntry",
    "ai_label_for_file",
    "bom_content_hash",
    "build_pack",
    "compliance_prerequisite_summary",
    "encode_bom",
    "export_evidence_bundle",
    "export_soc2_package",
    "generate_bom",
    "generate_sbom",
    "get_required_events",
    "load_compliance_config",
    "parse_period",
    "persist_compliance_config",
    "render_csv",
    "render_pdf",
    "resolve_compliance_config",
    "verify_bom",
]
