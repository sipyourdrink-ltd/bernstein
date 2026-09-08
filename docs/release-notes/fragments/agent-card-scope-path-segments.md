## Agent-card scope matches path segments, not string prefixes

`AgentIdentityCard.in_scope` decided membership with `path.startswith(prefix)`. That answers a different question, and gets it wrong in both directions that matter: `"/src/api-internal/keys.pem"` starts with `"/src/api"` but is a different directory, and `"/src/api/../../etc/passwd"` starts with it while resolving outside the tree entirely.

Membership is now decided on whole path segments, with either separator recognised so a scope written on one host is read the same way on another, and a `..` component refused rather than compared. A scope entry that names no segment (`"/"`, `""`) contains nothing rather than everything - a zero-length prefix matched every path, so one stray entry silently unrestricted the card - and a scope made entirely of such entries admits nothing, which is the fail-closed direction `core/security/guardrail_pipeline.py` already chose for the same defect on the modified-file manifest (#5488).
