# HMAC-chained audit log: operator guide

bernstein writes a tamper-evident, append-only audit log for every
orchestrator action. each entry is HMAC-SHA256-signed per
[RFC 2104](https://datatracker.ietf.org/doc/html/rfc2104) (HMAC) using
[FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) SHA-256, and
chained to the previous entry's hmac, so any after-the-fact edit invalidates
every following hmac. this page tells an SRE or security operator how to
run that surface in production: where the key lives, how to rotate it,
how to verify a snapshot, how to ship to a SIEM, and what to do when a
chain breaks.

short version: keep the key off the audit volume, run
`bernstein audit verify` from cron, fail the run if the exit code is
non-zero.

## Record format

logs live under `.sdd/audit/` as one JSONL file per UTC day, e.g.
`.sdd/audit/2026-05-07.jsonl`. each line is one event:

```json
{
  "timestamp": "2026-05-07T14:30:00.000000Z",
  "event_type": "task.transition",
  "actor": "orchestrator",
  "resource_type": "task",
  "resource_id": "TASK-001",
  "details": {"from_status": "open", "to_status": "claimed"},
  "prev_hmac": "0000…0000",
  "hmac": "d4e5f6…"
}
```

the `hmac` field is computed over the canonical JSON payload (every
field above except `hmac` itself) concatenated with the previous
entry's `hmac`. pseudocode:

```
payload   = prev_hmac + json.dumps(entry_without_hmac, sort_keys=True)
entry.hmac = HMAC_SHA256(audit_key, payload).hexdigest()
```

the very first event in a chain uses a genesis `prev_hmac` of 64
zeros. daily rotation never resets the chain - the next file's first
entry uses the last `hmac` from yesterday.

source: `src/bernstein/core/security/audit.py`.

### auto-approve decisions

the tool-call approval gate runs a deterministic command/tool
classifier (`src/bernstein/core/security/auto_approve.py`) before a tool
call reaches the operator queue. each decision the gate acts on is
recorded as an `auto_approve_decision` event so an auditor can prove,
after the fact, which calls were rejected or auto-approved and which
pattern drove the verdict:

```json
{
  "event_type": "auto_approve_decision",
  "actor": "backend",
  "resource_type": "tool_call",
  "resource_id": "Bash",
  "details": {
    "decision": "deny",
    "tool": "Bash",
    "reason": "Dangerous command detected: 'rm -rf /tmp/x'",
    "matched_pattern": "\\brm\\s+-rf\\b",
    "command": "rm -rf /tmp/x"
  }
}
```

precedence is deny-wins and the posture is fail-closed:

- a deny-listed command (e.g. `rm -rf`, `git push --force`, `DROP
  TABLE`, `curl ... | bash`) is rejected by the production path
  regardless of interactive mode, and the rejection is written to the
  chain with its matched pattern.
- a safe command is only auto-approved when the operator opts in via
  `approvals.smart_auto_approve: true` in `bernstein.yaml`. with the
  default (`false`) a safe verdict still goes to the queue.
- an ambiguous (`ask`) verdict, or any classifier error, never
  auto-approves - it falls through to human review.

source: `src/bernstein/core/approval/gate.py`.

## Key management

the HMAC key is loaded from, in order:

1. the `BERNSTEIN_AUDIT_KEY_PATH` environment variable, if set.
2. `$XDG_STATE_HOME/bernstein/audit.key` if `XDG_STATE_HOME` is set.
3. `~/.local/state/bernstein/audit.key` (XDG default).

a legacy fallback at `<sdd>/../config/audit-key` is still read for
verification only - bernstein logs a warning when it is used. migrate
off it.

design intent: the key sits **outside** `.sdd/audit/` so an attacker
who can write `.sdd/audit/*.jsonl` cannot also read or rotate the
signing key. don't undo this - never colocate the key with the log
volume, and don't bake the key into a docker image layer.

### Permissions

bernstein refuses to load a key file with mode looser than `0600`. on
posix systems a group- or world-readable key raises
`AuditKeyPermissionError` and the orchestrator refuses to start. on
windows the bit-check is skipped (NTFS uses ACLs).

```bash
chmod 0600 ~/.local/state/bernstein/audit.key
ls -l ~/.local/state/bernstein/audit.key
# -rw------- 1 ops ops 64 May  7 14:30 audit.key
```

### First boot

if no key file exists, bernstein generates one (32 bytes of
`secrets.token_hex`) at the resolved path on first startup, with the
parent directory created `0700` and the file `0600`. that auto-bootstrap
is fine for a developer laptop. for a production deploy, materialise
the key from your secrets manager and write it yourself before the
orchestrator first starts - that way the key value is owned by your
KMS / vault, not by whichever host happened to boot first.

```bash
# pull from vault, write to the canonical path, lock it down
mkdir -p ~/.local/state/bernstein
vault kv get -field=audit_key secret/bernstein/audit \
  > ~/.local/state/bernstein/audit.key
chmod 0600 ~/.local/state/bernstein/audit.key
```

### Externalising to a KMS / vault

bernstein does not call out to a KMS for every entry - that would put
a network hop on the audit write path. the supported pattern is:

- KMS / vault holds the canonical key.
- a sidecar (vault-agent, sops, your config-mgmt tool) projects it
  into the file at `BERNSTEIN_AUDIT_KEY_PATH` with `0600`.
- the orchestrator reads the file once at startup; rotate the
  projected file and restart to roll keys.

if you need full HSM-backed signing, that's an enhancement, not a
shipped feature - open an issue.

### Rotating the key without breaking the chain

the chain is keyed by **the secret used to generate the hmacs that
already exist on disk**. swapping the key invalidates every hmac
written before the swap. operationally, rotation is therefore a
"close current chain, open a new one" event, not an in-place update.

procedure:

1. drain in-flight tasks or stop the orchestrator: `bernstein stop`.
2. snapshot the current chain for forensic retention:

   ```bash
   bernstein audit verify --hmac-only        # confirm clean before rollover
   bernstein audit seal                      # store the merkle root
   tar czf audit-pre-rotate-$(date -u +%F).tgz .sdd/audit/
   ```

3. archive `.sdd/audit/` somewhere read-only and clear the live
   directory (or move the orchestrator to a fresh `.sdd/`). the
   sealed merkle root + the archived JSONL still verify with the
   **old** key - keep it alongside the archive.
4. write the new key to `BERNSTEIN_AUDIT_KEY_PATH` (`0600`).
5. start the orchestrator. the next entry begins a new chain with a
   genesis `prev_hmac`.

the practical rotation cadence is annual or on personnel/key
compromise events, not weekly - the cost is a chain split, not zero.
schedule it alongside SOC 2 / ISO renewals.

if you want a "soft rotation" with no chain break, the only honest
answer is "we don't ship that today". the chain is intentionally
keyed by a single secret to keep verification simple for auditors.

## Verifying the chain

the public verb is `bernstein audit verify`:

```bash
bernstein audit verify              # hmac chain + merkle seal
bernstein audit verify --hmac-only  # hmac chain only
bernstein audit verify --merkle-only
```

exit codes:

| code | meaning                                            |
|------|----------------------------------------------------|
| 0    | chain intact (and merkle seal matches, if checked) |
| 1    | one or more verification errors                    |

a healthy run prints a green panel:

```
╭───────────────────────────────────╮
│ HMAC Chain Verification Passed    │
╰───────────────────────────────────╯
```

a tamper hit prints a red panel and one line per mismatched entry -
filename, line number, expected vs stored prefix:

```
╭───────────────────────────────────╮
│ HMAC Chain Verification FAILED    │
╰───────────────────────────────────╯
  ! 2026-05-07.jsonl:42: HMAC mismatch (expected a1b2c3d4… got deadbeef…)
  ! 2026-05-07.jsonl:43: prev_hmac mismatch (expected a1b2c3d4… got deadbeef…)
```

run from cron (every 15 min is fine - the verify is read-only and a
day's worth of events checks in milliseconds):

```cron
*/15 * * * * cd /srv/bernstein && bernstein audit verify --hmac-only \
  >> /var/log/bernstein/audit-verify.log 2>&1 || \
  /usr/local/bin/page-on-call.sh "bernstein audit verify failed"
```

### Startup self-check

on every orchestrator start, bernstein re-verifies the last 100
entries automatically (`verify_on_startup` in
`src/bernstein/core/security/audit_integrity.py`). a key with
loose permissions raises `AuditKeyPermissionError` and the
orchestrator **refuses to start** - that's by design. clean up the
permissions, don't bypass the check.

### Verifying without the CLI

if you only have python and the audit volume (e.g. an offline
auditor laptop), call the module directly:

```python
from pathlib import Path
from bernstein.core.security.audit_integrity import verify_audit_integrity

result = verify_audit_integrity(Path(".sdd/audit"), count=10_000)
print(result.valid, result.entries_checked, result.errors)
```

### Merkle seal (second integrity layer)

`bernstein audit seal` writes a Merkle root over the daily `*.jsonl`
files into `.sdd/audit/merkle/seal-<ISO>.json`. it is a file-level
integrity layer on top of the per-line HMAC chain: it detects a
deleted, inserted, reordered, or content-tampered daily file.

what the seal binds:

- **leaf per file** - each file's leaf is the hash of its *whole*
  canonical byte content, so flipping any byte of any line (not just
  the last) changes the leaf and trips `TAMPERED` on verify.
- **domain-separated tree** - leaves are hashed `H(0x00 || bytes)` and
  internal nodes `H(0x01 || left || right)` (RFC-6962 style). a lone
  node at an odd level is promoted unchanged rather than paired with
  itself, so a leaf set with its last entry duplicated cannot collide
  with the un-duplicated set.
- **chain precheck** - `audit seal` verifies the HMAC chain before
  writing the root. a broken chain raises and **no seal is written**,
  so re-sealing can never launder a pre-existing tamper into a fresh
  root. seal a known-broken chain on purpose only via the recovery
  procedure below.

scheme versioning: seals carry a `"scheme"` field. verification
dispatches on it, so seals written before this hardening (`scheme` 1,
or absent) still verify under their original rule. roots produced
before and after the upgrade are **not comparable** - re-seal once
after upgrading, and keep any pinned pre-upgrade root labelled as the
old scheme. this is a scheme upgrade, not a tamper alert.

## Replaying a log

the `query` verb walks all JSONL files under `.sdd/audit/` and
streams matching events back as a table - handy for "show me what
this actor did between these two timestamps":

```bash
bernstein audit query --actor orchestrator --since 2026-05-07
bernstein audit query --event-type task.transition --limit 200
bernstein audit show --limit 50           # tail of the live log
```

note that `query` does not re-verify hmacs - pair it with
`bernstein audit verify` if the question is "is this trustworthy",
not "what happened".

## Slicing a deterministic subset

`bernstein audit slice` writes a deterministic JSONL subset of the
audit log between two HMAC anchors. The output is byte-stable -
each line is `sort_keys`-serialised - so a downstream replayer can
hash the slice directly. The HMAC chain inside the slice is
re-verified before the file is written; a structural mismatch
aborts the export.

```bash
# Slice a closed range
bernstein audit slice --from <hash> --to <hash> -o /tmp/slice.jsonl

# Head-anchored: from genesis through a known checkpoint
bernstein audit slice --to <hash> -o /tmp/head.jsonl

# Tail-anchored: from a known checkpoint through the latest entry
bernstein audit slice --from <hash> -o /tmp/tail.jsonl
```

The slice CLI pairs with the multi-tenant export in
[Multi-tenant audit slices](audit-multitenant.md) when an auditor
needs a tenant-scoped, signed bundle rather than the raw JSONL.

## Retention and rotation

daily rotation happens on every `log()` call (the file path is
derived from today's UTC date). there is no in-process compaction;
old files are gzipped into `.sdd/audit/archive/` by
`AuditLog.archive(RetentionPolicy(retention_days=…))`, default 90
days. invoke it from a maintenance job or a periodic timer.

a minimal `logrotate` snippet, if you'd rather not run the python
archiver:

```
/srv/bernstein/.sdd/audit/*.jsonl {
    daily
    rotate 90
    compress
    nocreate
    missingok
    notifempty
    # do NOT use copytruncate - the orchestrator appends, truncating
    # mid-write would corrupt the in-memory chain pointer
}
```

avoid `copytruncate`. the orchestrator holds an open append handle
and a python-side `_prev_hmac`; truncating under it produces a chain
that disagrees with itself across a process restart.

archive format guarantees: gzipped JSONL, the chain still verifies
end-to-end across uncompressed + archived files as long as you feed
both into the verifier.

## Shipping to a SIEM

bernstein ships in-process exporters for splunk HEC, elasticsearch,
cloudwatch logs, syslog (RFC 5424), generic webhook, and a local file
sink. they live in
`src/bernstein/core/security/audit_export.py` and consume the same
`AuditEntry` records the chain emits. configure via `bernstein.yaml`:

```yaml
audit_export:
  target: splunk         # splunk | elasticsearch | cloudwatch | syslog | webhook | file
  batch_size: 100
  flush_interval_s: 30
  splunk:
    endpoint: https://splunk.example.com:8088
    token: ${SPLUNK_HEC_TOKEN}
    index: bernstein-audit
    sourcetype: bernstein:audit
```

webhook payload, one batch:

```json
[
  {
    "timestamp": 1715091000.123,
    "event_type": "task.transition",
    "actor": "orchestrator",
    "resource": "task/TASK-001",
    "action": "claim",
    "outcome": "success",
    "details": {"from_status": "open", "to_status": "claimed"},
    "hmac": "d4e5f6…",
    "source": "bernstein-audit"
  }
]
```

retry behaviour: failed batches retry with exponential backoff
(`max_retries`, `retry_backoff_s`) before being counted in
`total_failed`. the chain on disk is the source of truth - SIEM is
a mirror, not the master copy. if the SIEM drops a batch, replay
from `.sdd/audit/` with `bernstein audit query`.

### Sample dashboards

splunk:
```
index=bernstein-audit | stats count by event_type, actor
index=bernstein-audit event_type=task.transition outcome=failure
```

datadog (via webhook → log intake):
```
service:bernstein-audit @event_type:task.transition @outcome:failure
```

## When the chain breaks

a real tamper looks like one or both of:

- `prev_hmac mismatch` - someone deleted or reordered an entry.
- `HMAC mismatch` - someone edited an entry's payload after the fact.

rare benign causes:

- partial write at process kill (last line truncated). expected to be
  unparseable JSON, not an hmac mismatch - both verifier paths log
  this distinctly.
- key rotation done without archive-then-clear (see above).
- legacy log written under the old `.hmac_key` filename, now read
  with the XDG-default key.

### Recovery procedure

1. **freeze the volume**. don't `rm`, don't restart the orchestrator
   yet. snapshot `.sdd/audit/` somewhere read-only.
2. capture full verification output:

   ```bash
   bernstein audit verify > /tmp/audit-verify.txt 2>&1; echo $?
   ```

3. diff the suspect line range against your SIEM mirror - if the
   webhook / splunk export was healthy, the SIEM has the original
   payload and you can identify exactly which fields were edited.
4. seal the broken chain so the corruption is itself tamper-evident
   forensically. a plain `audit seal` refuses a broken chain (so
   re-sealing can't hide a tamper) - pass `--allow-broken-chain` to
   capture the corrupted state on purpose:

   ```bash
   bernstein audit seal --allow-broken-chain
   ```

5. file an incident, preserve the snapshot, and start a fresh chain
   per the rotation procedure above. **do not** rewrite hmacs to
   "fix" the chain - that destroys the evidence.
6. if the orchestrator can't start because of `AuditKeyPermissionError`,
   tighten the key permissions and try again rather than pointing at
   a different key - the existing log was signed with the original.

## Cross-references

- [SOC 2 audit mode quick start](AUDIT.md)
- [Multi-tenant audit-chain export](audit-multitenant.md)
- [DSSE / in-toto envelope](audit-dsse-envelope.md) - third-party-verifiable
  wrapper over the Article 12 bundle.
- [Standard verifiable audit receipts](audit-receipt.md) - project a chain
  range into COSE_Sign1, in-toto, and RFC 6962 transparency envelopes an
  off-the-shelf verifier validates with no Bernstein install.
- [EU AI Act Article 12 evidence pack](../compliance/eu-ai-act-article-12-bundle.md)
- [Security hardening guide](security-hardening.md)
- [Disaster recovery runbook](../operations/disaster-recovery.md)
- [Enterprise evaluation checklist](../ENTERPRISE.md#audit-and-recovery)
- source: `src/bernstein/core/security/audit.py`,
  `audit_integrity.py`, `audit_export.py`.
