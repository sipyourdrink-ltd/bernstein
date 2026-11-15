# Read-only incremental audit verification

The library's `AuditLog.verify_incremental()` does not attempt writes or take
the chain writer lock by default, like `bernstein audit verify`. The report
contains the counters for this invocation; reading evidence does not create or
refresh a diagnostic file beside it.

```python
from pathlib import Path

from bernstein.core.security.audit import AuditLog

# Supply the existing trusted key; do not create one to verify an old chain.
log = AuditLog(Path("evidence/audit"), key=trusted_audit_key)
report = log.verify_incremental()
if not report.ok:
    raise ValueError(report.errors)
print(report.tiles_read, report.records_walked)
```

If an application explicitly needs the historical on-disk tile counter, call
`log.verify_incremental(record_tile_reads=True)` against its writable copy.
Only that opt-in permits the best-effort `.tiles-read.json` update after a
clean run. The file never determines a verification verdict. The accessor
`log.last_tile_read_count()` reads that last persisted value, which can be
stale or absent; use the returned report for current measurements.

This change does not create an immutable snapshot of live segments. Verification
still reads and hashes segment bytes, and sealing a growing segment still
replaces its hash tile. See the [audit layout and trust model](../security/audit-log.md).
