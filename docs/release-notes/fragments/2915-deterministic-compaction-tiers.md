## Structural compaction tiers are now reproducible (Issue #2915)

`micro.compact()` and `time_based.compact()` now derive their correlation ids
from a SHA-256 hash of the fold content rather than ``uuid4()``. The same
session, turn, and content always produce the same ``compact-<tier>-<8hex>``
id, which means the ids are stable across runs and trace readers can correlate
events without a database lookup. A ``policy_version`` field is also added to
``TierResult`` so a recorded result always names the policy fold that produced it.
