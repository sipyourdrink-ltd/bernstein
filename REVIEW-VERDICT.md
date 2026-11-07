FIXED: 2 of 2 blocking findings

F1 — Missing release-notes fragment → FIXED: docs/release-notes/fragments/6341-hosted-inference-ingest-adapter.md added

F2 — Core reachability test failure (bernstein.core.observability.hosted_inference_ingest unreachable) → FIXED: Added module to src/bernstein/core/observability/__init__.py __all__ list and added entry to tests/unit/core_reachability_allowlist.txt