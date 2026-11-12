# ADR-010: Audit-Chain Default - Cost Measurement and Migration Path

**Status**: Accepted
**Date**: 2026-07-24
**Context**: Bernstein audit log (`src/bernstein/core/security/audit.py`), issue #2690

---

## Problem

The lineage spine and the replay journal are always on. The HMAC-chained audit
log is not: it is enabled by `--audit`, `BERNSTEIN_AUDIT=1`, or a compliance
preset (`src/bernstein/core/orchestration/orchestrator.py`, the audit-enable
branch around line 814). An operator who never passes `--audit` gets
reproducible runs and a lineage record, but no tamper-evident chain, and the gap
only becomes visible when someone asks for one after the fact.

Making the chain on-by-default is defensible, but it must not be an unmeasured
guess. Before changing the default we need three things: the cost of enabling
the chain, a check that nothing depends on its absence, and a migration path
that never reports a chain-less workspace as tampered.

---

## Measurement

A hermetic micro-benchmark drives `AuditLog` directly - no network, no
orchestrator, no real adapters - with a fixed key and synthetic
scheduling-decision payloads: `scripts/bench_audit_chain.py`. Reproduce with:

```
uv run python scripts/bench_audit_chain.py
```

**Append** compares a real `AuditLog.log` (`chain-on`) against the same
canonical row written straight to a JSONL file with no `prev_hmac`/`hmac` and no
chain-tail recovery (`plain-append`). The gap is the marginal cost attributable
to the chain, on top of a line an audit-logging run would write anyway.
**Verify** measures a full `verify()`, a cold `scan_verified()`, and a warm
(cursor) `scan_verified()` re-scan after one more append.

Representative figures (Python 3.14, macOS, a loaded dev host - wall-clock
numbers are host-dependent; the ratios and byte counts are not):

### Append latency (per event)

| entry size | chain-on mean | chain-on p95 | plain-append mean | chain marginal |
|---|--:|--:|--:|--:|
| small  |  ~80 us |  ~98 us | ~31 us | +~49 us |
| medium |  ~81 us |  ~97 us | ~33 us | +~48 us |
| large  |  ~94 us | ~113 us | ~36 us | +~57 us |

### Bytes written per entry

| entry size | chain-on | plain-append | chain overhead |
|---|--:|--:|--:|
| small  |  376 B |  219 B | **+157 B** |
| medium |  502 B |  345 B | **+157 B** |
| large  | 1603 B | 1446 B | **+157 B** |

### Verify / scan throughput

| events | segments | verify (events/s) | cold scan (events/s) | warm-cursor tail |
|--:|--:|--:|--:|--:|
|  1000 |  1 | ~72,000 | ~56,000 |  ~99 us |
| 10000 |  1 | ~74,000 | ~56,000 |  ~94 us |
|  9000 | 30 | ~75,000 | ~55,000 | ~367 us |
|  9000 | 90 | ~75,000 | ~54,000 | ~947 us |

### Reading the numbers

- **Append cost is negligible per decision.** The chain adds roughly 50 us on
  top of a plain log write. A deterministic orchestrator emits scheduling and
  lifecycle events at human-review timescales, not in a tight loop; even at
  hundreds of decisions per second the chain is well under a millisecond of
  aggregate overhead.
- **Byte overhead is constant, not proportional.** The chain adds a fixed
  **+157 B/entry** - the two 64-hex chain fields (`prev_hmac` + `hmac`) plus
  their JSON framing - regardless of payload size. Growth of the chain file is
  dominated by the payload an operator chose to log, not by the chain. A run
  that emits 10,000 audit events pays about 1.5 MB for tamper-evidence.
- **Verify is linear and fast.** ~72-75k events/s, flat across total events and
  segment count. A full offline verify of a day's chain is sub-second.
- **Warm re-verification is cheap but scales with segment count.** A cursor scan
  re-reads only appended bytes, but still stats every segment: ~99 us at one
  segment rises to ~950 us at 90. In practice a single run writes one live
  segment and retention caps live segments at 90 days, so this is bounded; it is
  recorded as a follow-up below.

---

## What breaks if it is on by default

A targeted review of the audit test surface (the full suite is not run here by
policy):

- **No golden snapshot or replay fixture depends on the chain being absent.**
  The snapshot test (`tests/snapshot/test_audit_jsonl_snapshot.py`) pins the
  on-disk format when the chain is present; nothing asserts `.sdd/audit` is
  missing or that `AuditLog` is unconstructed by default.
- **No orchestrator-level test asserts audit-off-by-default** (`_audit_log is
  None` / `_audit_mode` is unset). The default lives only in the enable branch.
- The one real behavioral change of flipping the default is operational, not a
  test breakage: every run would create `.sdd/audit/` and generate an audit key
  on first boot. That is the surface the migration path below manages.

Confirmation step before any flip: run the audit and orchestrator test layers
with the default flipped, behind the flag, in CI - not a claim to make from a
static scan alone.

---

## Decision

**Keep the chain opt-in in this release. Adopt on-by-default as the target once
the migration steps below are in place. This PR lands the measurement, the
migration-safety invariant (locked by tests), and this record; it does not flip
the default.**

The measurement removes cost as an objection: append overhead is sub-millisecond
and byte overhead is a constant 157 B/entry. The reason not to flip the default
inside a measurement change is scope and operational rollout (key generation on
existing installs), not performance. The flip is a small, separate follow-up
that changes only default resolution and adds a louder first-run notice.

Of the three outcomes the issue admits - default-on, default-on above a run
size, stay opt-in with a louder prompt - a run-size threshold is rejected: the
per-decision cost does not vary enough with run length to justify a threshold,
and a threshold would make "is this run audited?" depend on a size heuristic
that is itself hard to audit.

---

## Migration path to on-by-default

### Enable-surface precedence (env / config)

Target resolution order, highest precedence first:

1. Explicit opt-out - `--no-audit` / `BERNSTEIN_AUDIT=0` (to be added with the
   flip). An operator who turns it off is never overridden by the default.
2. Explicit opt-in - `--audit` / `BERNSTEIN_AUDIT=1` (the `--audit` flag sets the
   env var; see `src/bernstein/cli/run_bootstrap.py`).
3. Compliance preset - `audit_logging` on a preset selected via `--compliance`
   / `BERNSTEIN_COMPLIANCE`. A preset may force the chain on; it does not force
   it off against an explicit opt-in.
4. The default - opt-in today, opt-out (on) after the flip.

An explicit env var or flag always wins over the default. This keeps the flip a
one-line change to step 4 plus the new opt-out at step 1.

### Key-path precedence

The HMAC key lives outside `.sdd/` so a log-writer cannot read or rotate it.
Resolution (from `_default_audit_key_path`):

1. `BERNSTEIN_AUDIT_KEY_PATH` (explicit override).
2. `$XDG_STATE_HOME/bernstein/audit.key`.
3. `~/.local/state/bernstein/audit.key` (XDG default).

On first default-on boot the key is generated with mode `0600` if absent; on
later boots the existing permissions are enforced (a group- or world-readable
key is a hard error).

### Existing-install behavior (the invariant to get right)

A workspace that predates the chain has no `.sdd/audit` segments. Enabling the
chain must not report it as tampered - the append-only chain gives an operator
no way to repair a bogus record after the fact. The existing code already
satisfies this, and it is now locked by `tests/unit/test_audit_chain_migration.py`:

- An absent or empty audit dir recovers the chain tail from **genesis**, not a
  bogus tail; `verify()` over it returns `(True, [])`.
- The **first** chained run anchors its first event to genesis; that run, and a
  fresh reader process, both verify clean.
- Sibling `.sdd/` state (lineage spine, runtime files) is ignored: `verify()`
  reads only the audit dir's own `*.jsonl` / `*.jsonl.gz` segments, so
  pre-existing state cannot be mistaken for chain history.

Net: turning the chain on for the first time starts a fresh chain from genesis
in that workspace. There is nothing to compare the past against, and the
verifier does not pretend otherwise.

### Rollback

Rollback is a config change, not a data migration. Setting `BERNSTEIN_AUDIT=0`
(or `--no-audit`, or dropping the compliance preset) returns to opt-in. The
segments already written stay valid and independently verifiable; nothing is
deleted. Re-enabling later resumes the **same** chain, because chain recovery
walks the existing segments (live, then archived) to find the tip. Because the
chain is append-only and keyed outside the audit dir, neither disabling nor
re-enabling rewrites history.

---

## Consequences

### Benefits

- The default-on decision is now backed by numbers, not intuition.
- The migration-safety invariant is locked by tests, so a future flip cannot
  silently regress a chain-less workspace into a false tamper report.
- The benchmark is reproducible and hermetic, so the cost can be re-measured on
  any host or after any change to the append/verify path.

### Costs

- The chain remains opt-in for now; operators who do not pass `--audit` still
  get no tamper-evidence until the follow-up flip.
- The benchmark's wall-clock figures are host-dependent and must be re-run to
  compare across machines; only the ratios and byte counts transfer.

---

## Follow-ups (recorded, not done here)

1. **Flip the default to on**, adding the `--no-audit` / `BERNSTEIN_AUDIT=0`
   opt-out and a louder first-run notice, once the CI confirmation step above
   passes. Small, isolated change to default resolution.
2. **Collapse the double canonical serialization in `AuditLog.log`.** The append
   path serializes the pre-HMAC entry (to compute the HMAC) and again with the
   HMAC field added (to write the line) - two `json.dumps(sort_keys=True)` per
   append, part of the ~50 us marginal. Collapsing to one requires splicing the
   `hmac` field into an already-serialized string at its sorted position, which
   risks the byte-exact on-disk format the tamper-evidence and snapshot tests
   depend on. Deferred as higher-risk than its payoff.
3. **Bound the warm-cursor scan at O(appended), not O(segments).** A warm
   `scan_verified` still stats every segment each call, so incremental
   re-verification cost grows with segment count (~99 us at 1 segment, ~950 us
   at 90). Candidate: skip the re-stat of segments already marked `complete`.

### Done in this change

- `AuditLog.log` now derives the event timestamp and the daily-file name from a
  single `datetime.now` reading rather than two - one fewer clock read per
  append, and it closes a latent UTC-midnight straddle where an event could be
  filed under a day that disagreed with its own timestamp.
