## Versioned aliases share canonical request limits

Routes under `/api/v1` share the canonical route's request limits and SSE
concurrency cap. Canonical seed-bucket rules take precedence, while
versioned-only bucket configurations remain supported. SSE slots remain
held until their streams close.
