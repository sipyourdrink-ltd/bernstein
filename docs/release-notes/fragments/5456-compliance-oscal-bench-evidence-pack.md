---
category: features
issue: 5456
title: "compliance: evidence packs carry signed bench bundles keyed by control and OSCAL assessment-results export"
---

Evidence packs now carry signed benchmark bundles (`bench_bundles`) keyed to regulatory controls in `controls.json`. Controls are annotated with empirical pass rates and status (`measured`, `declared-not-measured`, `not-applicable`) along with explicit reasons. An automated NIST OSCAL 1.0.0 `assessment-results` document is embedded in each evidence pack under `oscal/assessment-results.json`. The pack verification engine (`verify_evidence_pack`) validates manifest artifact hashes, detached Ed25519 bundle signatures, and stored run receipt hashes.
