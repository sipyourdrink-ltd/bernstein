"""FINOS AI Governance Framework mitigation catalogue.

The one place in this tree that says which FINOS AIGF mitigation ids exist
and what each one is called. It exists because the cross-framework
references in :mod:`bernstein.compliance.controls` used to carry an
``AIGF-GOV-01``-style vocabulary that FINOS does not publish: an auditor
following such a reference back to the framework finds nothing, which is
worse than carrying no reference at all.

The framework numbers its mitigations ``mi-1`` .. ``mi-23`` (file names and
``sequence`` front-matter in ``docs/_mitigations/``) and its risks ``ri-N``.
Each mitigation is typed ``PREV`` (preventative) or ``DET`` (detective).
The titles below are the ``title:`` front-matter values, copied verbatim.

Source: https://github.com/finos/ai-governance-framework, ``docs/_mitigations/``
at commit ``aabbffbe02a4aae8e6d5f8534d63edf9248fa302`` (read 2026-09-21).
Community Specification License v1.0.

Note for readers of ``docs/compliance/finos-aigf-mapping.md``: that document
predates this module and still uses a ``CTRL-*`` / ``AIR-*`` vocabulary from
the framework's earlier "AI Readiness" naming. Reconciling it is tracked
in #6148; this module is the vocabulary the code cites.
"""

from __future__ import annotations

#: Mitigation id -> (title, type). Verbatim from the upstream front matter.
MITIGATIONS: dict[str, tuple[str, str]] = {
    "mi-1": ("AI Data Leakage Prevention and Detection", "DET"),
    "mi-2": ("Data Filtering From External Knowledge Bases", "PREV"),
    "mi-3": ("User/App/Model Firewalling/Filtering", "PREV"),
    "mi-4": ("AI System Observability", "DET"),
    "mi-5": ("System Acceptance Testing", "PREV"),
    "mi-6": ("Data Quality & Classification/Sensitivity", "PREV"),
    "mi-7": ("Legal and Contractual Frameworks for AI Systems", "PREV"),
    "mi-8": ("Quality of Service (QoS) and DDoS Prevention for AI Systems", "PREV"),
    "mi-9": ("AI System Alerting and Denial of Wallet (DoW) / Spend Monitoring", "DET"),
    "mi-10": ("AI Model Version Pinning", "PREV"),
    "mi-11": ("Human Feedback Loop for AI Systems", "DET"),
    "mi-12": ("Role-Based Access Control for AI Data", "PREV"),
    "mi-13": ("Providing Citations and Source Traceability for AI-Generated Information", "DET"),
    "mi-14": ("Encryption of AI Data at Rest", "PREV"),
    "mi-15": ("Using Large Language Models for Automated Evaluation (LLM-as-a-Judge)", "DET"),
    "mi-16": ("Preserving Source Data Access Controls in AI Systems", "DET"),
    "mi-17": ("AI Firewall Implementation and Management", "PREV"),
    "mi-18": ("Agent Authority Least Privilege Framework", "PREV"),
    "mi-19": ("Tool Chain Validation and Sanitization", "PREV"),
    "mi-20": ("MCP Server Security Governance", "PREV"),
    "mi-21": ("Agent Decision Audit and Explainability", "DET"),
    "mi-22": ("Multi-Agent Isolation and Segmentation", "PREV"),
    "mi-23": ("Agentic System Credential Protection Framework", "PREV"),
}

#: Human-readable catalogue name.
REGULATION: str = "FINOS AI Governance Framework (mitigations mi-1..mi-23)"

#: Upstream source, pinned so a reader can diff this catalogue against it.
SPEC_URL: str = "https://github.com/finos/ai-governance-framework"
SPEC_COMMIT: str = "aabbffbe02a4aae8e6d5f8534d63edf9248fa302"


def mitigation_titles() -> dict[str, str]:
    """Map each mitigation id to its upstream title."""
    return {mid: title for mid, (title, _type) in MITIGATIONS.items()}


def reference_label(mitigation_id: str) -> str:
    """Return ``"<id> - <upstream title>"`` for use in a crosswalk.

    Raises ``KeyError`` on an id the framework does not publish, so a
    reference an auditor could not resolve fails at import time rather than
    shipping as a compliance claim.
    """
    entry = MITIGATIONS.get(mitigation_id)
    if entry is None:
        raise KeyError(f"{mitigation_id!r} is not a FINOS AIGF mitigation; known ids: {', '.join(sorted(MITIGATIONS))}")
    return f"{mitigation_id} - {entry[0]}"


__all__ = [
    "MITIGATIONS",
    "REGULATION",
    "SPEC_COMMIT",
    "SPEC_URL",
    "mitigation_titles",
    "reference_label",
]
