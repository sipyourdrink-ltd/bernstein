# 501d — Response Caching for Deterministic Tool Calls
**Role:** backend **Priority:** 2 **Scope:** small

Cache git-status, file-reads, ls results for N seconds. Next agent asking same question gets cached response. hash(cmd+cwd+mtime) → cached output.
