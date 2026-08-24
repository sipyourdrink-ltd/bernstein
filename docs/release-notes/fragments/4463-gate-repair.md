## Merge-gate failures get one bounded repair attempt

A merge-gate failure (lint or the affected tests) used to just park the branch, with the exact failure output surviving only in a log. The orchestrator now seeds one repair task carrying that output before falling back to the existing reopen/permanent-fail handling: repair succeeds and the branch merges normally, repair fails and the task fails exactly as before, no second attempt either way. Off switch: `gate_repair_enabled` in `bernstein.yaml` (#4463).
