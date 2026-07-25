"""Lineage v1 - Sigstore-style per-artefact transparency log.

See docs/decisions/009-lineage-v1.md for the design rationale.

Public API:

  - LineageEntry - frozen dataclass for a single write event
  - canonicalise, entry_hash - RFC 8785 JCS bytes + sha256 digest
  - AgentCard - minimal A2A v1.0 Agent Card subset
  - generate_keypair, sign_detached, verify_detached - Ed25519 JWS RFC 7515

Storage (LineageStore), the signed-write path (seal_write / SignedLineageLog),
gate, merge, compliance pack, and MCP resource live in sibling modules under
this package and re-export through here.
"""

from bernstein.core.lineage.entry import (
    ARTEFACT_KINDS,
    LINEAGE_ENTRY_VERSION,
    LineageEntry,
    canonicalise,
    entry_hash,
)
from bernstein.core.lineage.gate import GateResult
from bernstein.core.lineage.gate import check as gate_check
from bernstein.core.lineage.identity import (
    AgentCard,
    generate_keypair,
    jws_header_kid,
    sign_detached,
    verify_detached,
)
from bernstein.core.lineage.merge import (
    AgentPolicy,
    FirstWriterPolicy,
    HumanPolicy,
    LineageConflict,
    MergePolicy,
    StewardKey,
    build_merge_entry,
    resolve_policy,
)
from bernstein.core.lineage.signed_write import SignedLineageLog, seal_write
from bernstein.core.lineage.spine import (
    SPINE_ENTRY_VERSION,
    LineageSpine,
    SpineEntry,
    SpineStatus,
    SpineVerifyResult,
    compute_entry_hash,
    content_hash_of,
)
from bernstein.core.lineage.tips import Fork, TipSet, compute_tips, detect_forks
from bernstein.core.lineage.tracker_audit import (
    AuditingTrackerAdapter,
    LineageCtx,
    TrackerActor,
    TrackerAuditEntry,
    TrackerAuditLog,
    wrap_adapter,
)
from bernstein.core.lineage.v2_store import (
    LINEAGE_V2_ENTRY_VERSION,
    ChildBody,
    LineageV2Store,
    ParentRef,
    VerifyResult,
    compute_child_sha,
    is_v2_enabled,
)

__all__ = [
    "ARTEFACT_KINDS",
    "LINEAGE_ENTRY_VERSION",
    "LINEAGE_V2_ENTRY_VERSION",
    "SPINE_ENTRY_VERSION",
    "AgentCard",
    "AgentPolicy",
    "AuditingTrackerAdapter",
    "ChildBody",
    "FirstWriterPolicy",
    "Fork",
    "GateResult",
    "HumanPolicy",
    "LineageConflict",
    "LineageCtx",
    "LineageEntry",
    "LineageSpine",
    "LineageV2Store",
    "MergePolicy",
    "ParentRef",
    "SignedLineageLog",
    "SpineEntry",
    "SpineStatus",
    "SpineVerifyResult",
    "StewardKey",
    "TipSet",
    "TrackerActor",
    "TrackerAuditEntry",
    "TrackerAuditLog",
    "VerifyResult",
    "build_merge_entry",
    "canonicalise",
    "compute_child_sha",
    "compute_entry_hash",
    "compute_tips",
    "content_hash_of",
    "detect_forks",
    "entry_hash",
    "gate_check",
    "generate_keypair",
    "is_v2_enabled",
    "jws_header_kid",
    "resolve_policy",
    "seal_write",
    "sign_detached",
    "verify_detached",
    "wrap_adapter",
]
