# Cross-tier verification

The opt-in Python API `bernstein.core.verification.run_verify` checks task
results from edge, search, research and sandbox backends. It does not dispatch
inference or select a provider. The coordinator supplies approved inputs and
judge configuration. Calling `run_verify` without stores is a local check:
`record.ok` describes the check, while `record.recorded` remains false.

## Candidate and trust contracts

- **Edge and sandbox:** supply `workdir` (the repository containing the base),
  `base_commit` (the full 40- or 64-character lowercase commit ID), `diff` (a
  Git-compatible patch against that commit), and `backend`. Each attempt exports
  that exact commit with `git archive`, applies the patch in a temporary
  directory and checks that candidate. The caller's dirty files and index are
  never the baseline. `pre_applied=True` is rejected; there is no attestation
  contract for caller-applied changes. Changed paths come from the patch, not
  the backend's `changed_files` claim. Unsupported patches or unavailable bases
  fail closed. The temporary tree is deleted after the attempt, including a
  failed attempt. Submodules/dependencies must already be available in the
  execution environment; no checkout, installation or network fetch is done.
- **Edge:** runs the current interpreter's `ruff` on surviving changed Python
  files and `pytest` on coordinator-supplied `test_paths`, or on surviving
  changed modules named `test_*.py` or `*_test.py`. Supply affected tests through
  `test_paths` when source changes require unchanged tests. `latest_cache.py`
  is a source file. No selected tests is explicitly `pytest_status=not_selected`
  (a lint-only PASS); selected tests that collect nothing (exit 5) are FAIL.
  This is bounded verification, not a claim that the full suite ran.
- **Sandbox:** `verify_command` is an approved argv list run in the candidate.
  A string is a literal executable name, not shell syntax. These checks execute
  code with the caller's permissions: temporary-directory isolation is not an
  adversarial-code security boundary. Use an appropriately isolated executor.
- **Search:** construct `SearchVerifier(expected_digests=..., manifest_id=...)`
  from an independently retained, trusted capture manifest. The map binds exact
  URLs to lowercase SHA-256 hex digests of the content's exact UTF-8 bytes.
  Never build expected values from the untrusted backend response. Results
  require URL/content fields; nonempty content must match its expected digest.
  Missing/invalid expected data or mismatches are FAIL; empty content is RETRY.
  Evidence retains the manifest identity and its canonical hash. Checksums
  establish correspondence to that manifest, not the truth of a web page.
- **Research:** an approved callable `judge_fn(report, rubric) -> float` and a
  nonempty `judge_id` are mandatory. Scores and thresholds must be finite and
  in [0, 1]. Below-threshold scores are FAIL; invocation errors are RETRY.
  There is no structural-only PASS. Judge identity, report/rubric hashes,
  threshold and finite score are evidence. The callable's provenance and
  approval belong to the coordinator; this API cannot authenticate a model
  merely from its label. No live judge is called by the regression suite.

## Retry ownership

`run_with_retries` alone owns exhaustion: edge 2 retries with no delay, search
1 retry at 2 seconds, research 3 retries with capped exponential backoff, and
sandbox 2 retries with linear backoff. Built-in and custom verifiers return
RETRY for transient failures regardless of the attempt number. A FAIL stops
immediately. The runner preserves every actual verdict in `record.attempts`,
including the final RETRY, and derives a separate terminal FAIL on exhaustion.
It reruns verification of the same result; generating a new task result or
escalating to another executor is the caller's responsibility. There is no
implicit redispatch, provider change or nested retry loop.

## Durable recording and failure

Pass both a real `LineageSpine` and a real `AuditChainStore`, with `run_id`
matching the spine's run directory. Incomplete storage configuration or a
mismatched run raises `ValueError` before checks run.

The complete schema-v1 envelope (run identity, final verdict, actual attempts,
evidence, timing and reasons) is atomically written and read back at:

```
<spine.run_dir>/verification/<envelope-sha256>.json
```

The file uses the existing `py-json-v1` profile: UTF-8, unescaped Unicode,
sorted keys and compact separators. Non-finite JSON values are rejected.
`verify_evidence_hash` uses the same canonical hash function as the verifier
ladder. The envelope hash differs from each attempt's `evidence_hash`, which
covers only its evidence dict. Do not substitute RFC 8785/JCS or normalize
Unicode or numeric types when recomputing these hashes.

`LineageSpine.record(content=...)` binds the envelope's bytes, actor, backend,
step and timestamp. The `verify_outcome` audit event is appended using
`log_with_prev_digest`; it binds run, tier, backend, verdict, evidence hash,
envelope hash/path, attempt count and the returned spine entry hash. A recorded
result carries `artifact_path`, `spine_entry_hash` and `audit_event_hash`.
Retain the envelope with both stores. The stores alone do not contain raw
evidence; deleting the envelope makes offline evidence reconstruction
impossible. Subprocess output is capped at 2,000 characters per stream; keep
full logs separately if needed. Store directories are trusted operator-owned
paths; evidence files use the existing atomic writer's owner-only mode.

Writes across these stores are not transactional. On a write/readback error,
`VerificationRecordingError` is raised with the checked result in `.record`
and `recorded=False`, even if the check itself passed. Partial artifact/spine
writes are preserved, and available paths/anchors are returned in the error's
record. Reconcile that evidence before any explicit new attempt; the API does
not replay a partially recorded run automatically or advertise it as recorded.

## Runtime integration

`run_janitor(..., tier_verifications={task_id: VerificationRequest(...)})`
opts individual tasks into this check and recording contract. The request
contains a configured verifier, task result and both stores. Its spine must
use the task ID as its run ID. The janitor adds a `cross_tier:verification`
signal and blocks acceptance on verification or recording failure. Without
this argument, main's existing completion signals, attribution, guardrails
and ladder behavior are unchanged. Existing checks still run and can block an
otherwise passing cross-tier result. This integration is exercised against
real stores in `tests/unit/verification/test_end_to_end.py`.
