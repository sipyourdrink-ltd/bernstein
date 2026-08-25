## Plan approval binds to the rendering the reviewer saw

Both plan decision routes re-read the stored plan at decision time, so an edit
made between rendering and approval was invisible to the approver. A plan now
carries the SHA-256 digest of its deterministic rendering; approve and reject
recompute it and answer 409 on mismatch, so a decision lands only on the exact
plan text the reviewer read (#3839).
