## C2PA manifests pin spec 2.4 and carry an AI-disclosure assertion

`bernstein credential emit` now targets C2PA 2.4, the revision that
introduced the `c2pa.ai-disclosure` assertion. Each manifest carries that
assertion, and the `digitalSourceType` on the actions assertion is derived
from the artifact's lineage entries rather than hard-coded, so a verifier
can tell machine-generated output from human-authored output. (#6223)
