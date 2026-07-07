"""Verification evidence bundles (issue #2362).

Every completed task can emit a content-addressed *evidence bundle*: the
proof-of-done artefacts (test-runner output, coverage, lint, optional
screenshot / recording) that justify a task's completion, stored under a
retention policy, anchored in the lineage spine, and sealed by the HMAC audit
chain. The bundle is not "a status plus a log"; it *is* a signed, spine-anchored
receipt whose integrity verifies offline. Strip the spine and the signature and
the bundle is just a file; anchored and signed it is a chain-verifiable
attestation that recomputes from the stored evidence alone.

See :mod:`bernstein.core.evidence.bundle` for the public surface.
"""

from __future__ import annotations
