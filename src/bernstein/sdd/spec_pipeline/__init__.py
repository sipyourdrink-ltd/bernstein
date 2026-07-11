"""Spec pipeline: compile a requirements document into a gated task graph.

Issue #2361. A three-stage pipeline with exactly one model call turns a
requirements document into a task graph whose provenance is verifiable end to
end:

1. **Draft** (:mod:`bernstein.sdd.spec_pipeline.draft`) -- a single drafter
   call extracts EARS-shaped acceptance lines into a content-addressed
   :class:`RequirementSet`. This is the only stage that may touch a model.
2. **Approve** (:mod:`bernstein.sdd.spec_pipeline.receipt`) -- the operator
   approves the requirement set through the plan-approval gate; the receipt
   binds the requirement-set hash into the HMAC audit chain.
3. **Compile** (:mod:`bernstein.sdd.spec_pipeline.compiler`) -- approved
   requirements compile to a task graph as a pure, deterministic transformation
   (no model call). Each node carries the content hashes of the requirement
   lines it implements, so every artefact traces back to spec lines.

Spec edits produce a diffable graph delta
(:mod:`bernstein.sdd.spec_pipeline.delta`): only the subgraph touching a
changed requirement re-plans; every other node keeps its content-addressed
identity.
"""

from __future__ import annotations

from bernstein.sdd.spec_pipeline.compiler import (
    CoverageResult,
    TaskGraph,
    TaskNode,
    compile_requirements,
    infer_role,
    lineage_coverage,
)
from bernstein.sdd.spec_pipeline.delta import GraphDelta, graph_delta
from bernstein.sdd.spec_pipeline.draft import (
    Drafter,
    InstrumentedDrafter,
    StructuralDrafter,
    draft_requirements,
)
from bernstein.sdd.spec_pipeline.receipt import (
    RequirementSetReceipt,
    approve_requirement_set,
)
from bernstein.sdd.spec_pipeline.requirements import (
    EarsKind,
    Requirement,
    RequirementSet,
    build_requirement_set,
    canonical_text,
    classify_ears,
    hash_text,
    is_ears,
)

__all__ = [
    "CoverageResult",
    "Drafter",
    "EarsKind",
    "GraphDelta",
    "InstrumentedDrafter",
    "Requirement",
    "RequirementSet",
    "RequirementSetReceipt",
    "StructuralDrafter",
    "TaskGraph",
    "TaskNode",
    "approve_requirement_set",
    "build_requirement_set",
    "canonical_text",
    "classify_ears",
    "compile_requirements",
    "draft_requirements",
    "graph_delta",
    "hash_text",
    "infer_role",
    "is_ears",
    "lineage_coverage",
]
