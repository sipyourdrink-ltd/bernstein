"""Local-model worker tier: endpoint conformance and signed certification.

Assembling a mixed fleet (API planner, local workers behind OpenAI-compatible
endpoints) used to be hand-rolled per-role config with no conformance
guarantee: an endpoint that cannot do tool calling or that corrupts unified
diffs surfaces only at run time, at the merge gate. This package makes the
capability check a first-class, chain-anchored artifact:

* :mod:`bernstein.core.endpoints.conformance` -- a deterministic probe subset
  (reachability, chat completion, tool calling, patch format fidelity,
  timeout behavior, context floor) whose per-role verdict is a pure function
  of the endpoint's responses.
* :mod:`bernstein.core.endpoints.certification` -- the signed certification
  receipt: an Ed25519-signed record anchored in the lineage spine and
  mirrored into the HMAC audit chain. Config validation gates merge-critical
  roles on a verifying receipt, never on a boolean in config.
"""

from __future__ import annotations

from bernstein.core.endpoints.certification import (
    CERTIFICATION_RUN_ID,
    CERTIFICATION_SCHEMA_VERSION,
    CertificationVerifyResult,
    EndpointCertification,
    build_endpoint_certification,
    certification_path,
    certified_roles_for_endpoint,
    endpoint_fingerprint,
    load_or_create_endpoint_identity,
    read_endpoint_certification,
    validate_endpoint_assignments,
    verify_endpoint_certification,
)
from bernstein.core.endpoints.conformance import (
    ALL_PROBES,
    CONFORMANCE_SUITE_VERSION,
    LOCAL_TIER_ROLES,
    PATCH_REFERENCE_DIFF,
    PROBE_CHAT_COMPLETION,
    PROBE_CONTEXT_FLOOR,
    PROBE_PATCH_FIDELITY,
    PROBE_REACHABILITY,
    PROBE_TIMEOUT_BEHAVIOR,
    PROBE_TOOL_CALLING,
    ConformanceTranscript,
    ProbeResult,
    RoleVerdict,
    discover_default_model,
    evaluate_roles,
    is_gated_role,
    normalize_base_url,
    required_probes_for_role,
    run_conformance,
)

__all__ = [
    "ALL_PROBES",
    "CERTIFICATION_RUN_ID",
    "CERTIFICATION_SCHEMA_VERSION",
    "CONFORMANCE_SUITE_VERSION",
    "LOCAL_TIER_ROLES",
    "PATCH_REFERENCE_DIFF",
    "PROBE_CHAT_COMPLETION",
    "PROBE_CONTEXT_FLOOR",
    "PROBE_PATCH_FIDELITY",
    "PROBE_REACHABILITY",
    "PROBE_TIMEOUT_BEHAVIOR",
    "PROBE_TOOL_CALLING",
    "CertificationVerifyResult",
    "ConformanceTranscript",
    "EndpointCertification",
    "ProbeResult",
    "RoleVerdict",
    "build_endpoint_certification",
    "certification_path",
    "certified_roles_for_endpoint",
    "discover_default_model",
    "endpoint_fingerprint",
    "evaluate_roles",
    "is_gated_role",
    "load_or_create_endpoint_identity",
    "normalize_base_url",
    "read_endpoint_certification",
    "required_probes_for_role",
    "run_conformance",
    "validate_endpoint_assignments",
    "verify_endpoint_certification",
]
