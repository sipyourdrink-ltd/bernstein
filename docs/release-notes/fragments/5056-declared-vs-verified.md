## Compliance docs separate a declared posture from a verified control

`docs/operations/compliance.md` listed SOC 2, ISO 27001, PCI-DSS and NIST
800-53 as "Shipped" in the same column as the EU AI Act, whose check walks
every HMAC link in a recorded chain and aborts on a break. The checks behind
the other four read configuration, and most are satisfied by a key being
present at all — `check_auth_configured` is `"auth" in config`, so an empty
`auth:` section passes it.

The table gains a **What a pass asserts** column: *verified from evidence* for
the EU AI Act and HIPAA surfaces, *declared posture* for the policy library. The
library's module docstring says the same thing where a caller rendering its
results will read it.

The distinction is measured, not asserted: 14 of the 23 policy-library checks
pass on a configuration that declares every key and configures none of them,
and `tests/unit/test_compliance_assertion_classes.py` pins that count. Strengthen
a check and the count moves, which forces the prose to be rewritten with it.

No check changed behaviour — Option B in the issue is a separate change. A
declared-posture lint still catches the common real failure, which is that
nobody configured it at all (#5056).
