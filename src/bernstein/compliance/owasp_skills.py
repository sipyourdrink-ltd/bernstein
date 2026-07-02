"""OWASP Agentic Skills Top 10 (AST01-AST10) control map.

Operators who install and run third-party skills need to show, from
their own run evidence, which skill-supply-chain controls a Bernstein
run already covers. This module is the skills-catalogue analogue of the
EU AI Act clause map in ``evidence_pack.py`` and the ASI map in
``owasp_asi.py``.

The map is anchored on two Bernstein levers working together:

* the Ed25519-signed install identity and the per-entry detached-JWS
  signature the skill catalog verifies before install, and
* the HMAC audit chain, into which every catalog action
  (``skill.catalog.fetch|install|upgrade|uninstall|sync``) is written.

Every ``selector`` cites a literal ``event_type`` string a Bernstein run
actually writes, so the coverage claim is only as strong as the signed
catalog plus the tamper-evident chain that backs it. Remove either lever
and the map loses its meaning, not merely its log.

Honesty rule: where Bernstein only partially addresses a control the
entry is marked ``"partial"`` and the gap is stated. Skill-catalogue
coverage is genuinely partial for several AST classes (runtime skill
sandboxing, per-skill data-scope enforcement), and the map says so
rather than over-claiming.

The block mirrors the ``_STANDARD_MAPS`` shape exactly and is consumed
by ``evidence_pack.build_evidence_pack`` under the ``owasp-skills``
standard.
"""

from __future__ import annotations

from typing import Any

#: Standard id used on the ``--standard`` flag and in manifests.
STANDARD_ID: str = "owasp-skills"

#: Human-readable catalogue name emitted into ``controls.json``.
REGULATION: str = "OWASP Agentic Skills Top 10 (AST01-AST10)"

# ---------------------------------------------------------------------------
# AST01-AST10 control map
# ---------------------------------------------------------------------------

CONTROLS: list[dict[str, Any]] = [
    {
        "control_id": "AST01",
        "requirement": (
            "Untrusted / unsigned skill install: the catalog verifies an "
            "Ed25519 detached signature over each entry's canonical bytes "
            "against the catalog signer public key before install; an "
            "unverified entry installs only with an explicit override."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.install,skill.catalog.fetch",
        "status": "mapped",
    },
    {
        "control_id": "AST02",
        "requirement": (
            "Skill tampering after publish: catalog entries are content-"
            "addressed and lineage-logged; post-fetch tampering surfaces as "
            "a lineage-tamper detection against the recorded content hash."
        ),
        "artefact": "lineage/log.jsonl",
        "selector": "lineage_tamper_detected,content_hash",
        "status": "mapped",
    },
    {
        "control_id": "AST03",
        "requirement": (
            "Skill provenance / pinning: install, upgrade and uninstall are "
            "all chained with the resolved source spec, giving a pinned, "
            "auditable history of which skill version ran when."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.install,skill.catalog.upgrade,skill.catalog.uninstall",
        "status": "mapped",
    },
    {
        "control_id": "AST04",
        "requirement": (
            "Excessive skill capability (trifecta): a skill-invoked tool "
            "chain is screened by the lethal-trifecta capability matrix; a "
            "chain holding private-data + untrusted-content + external-egress "
            "is denied and recorded."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal",
        "status": "mapped",
    },
    {
        "control_id": "AST05",
        "requirement": (
            "Unsafe skill code execution: skill-invoked commands run under a "
            "sandbox profile and command allowlist; a sandbox-escape attempt "
            "is detected and recorded as a security incident. Per-skill "
            "runtime sandboxing (distinct from the shared command sandbox) is "
            "not yet enforced."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "sandbox_escape_attempt,command",
        "status": "partial",
    },
    {
        "control_id": "AST06",
        "requirement": (
            "Skill data-scope / exfiltration: cost and egress are bounded by "
            "per-run caps and the trifecta egress axis, and redaction tiers "
            "apply before artefacts leave the boundary. Per-skill data-scope "
            "enforcement (a skill only reads what it declared) is not yet a "
            "first-class chained control."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal,budget.exhausted",
        "status": "partial",
    },
    {
        "control_id": "AST07",
        "requirement": (
            "Skill identity / impersonation: the catalog signer identity is "
            "an Ed25519 public key; entries that do not verify against it are "
            "refused, so a skill cannot impersonate a trusted publisher."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.install,skill.catalog.sync",
        "status": "mapped",
    },
    {
        "control_id": "AST08",
        "requirement": (
            "Skill catalog drift / unbounded install: catalog sync is chained "
            "so unexpected additions are visible; per-run cost caps bound the "
            "spend a runaway skill can incur."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.sync,budget.warning,budget.exhausted",
        "status": "partial",
    },
    {
        "control_id": "AST09",
        "requirement": (
            "Skill observability gap: every catalog action and every step a "
            "skill triggers is written to the HMAC-chained audit log, so a "
            "skill's activity is reconstructable offline."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.fetch,skill.catalog.install,task.transition,agent.transition",
        "status": "mapped",
    },
    {
        "control_id": "AST10",
        "requirement": (
            "Skill misalignment / prompt-injection via skill content: skill "
            "output flows through the on-by-default OWASP ASI guardrail pack "
            "and promptware scanner; refusals are recorded. Skill-specific "
            "alignment scoring is heuristic, not a chained metric."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal,review_pipeline.stage",
        "status": "partial",
    },
]

#: Controls whose evidence is out of scope for the read-only evidence pack.
DEFERRED: list[str] = [
    "AST05 per-skill runtime sandboxing distinct from the shared command sandbox.",
    "AST06 per-skill declared-data-scope enforcement as a first-class chained control.",
]


def control_map() -> dict[str, Any]:
    """Return the OWASP AST control-map block in ``_STANDARD_MAPS`` shape.

    The list is copied so a caller mutating the returned dict cannot
    corrupt the module-level catalogue.
    """
    return {
        "regulation": REGULATION,
        "controls": [dict(c) for c in CONTROLS],
        "deferred": list(DEFERRED),
    }


__all__ = [
    "CONTROLS",
    "DEFERRED",
    "REGULATION",
    "STANDARD_ID",
    "control_map",
]
