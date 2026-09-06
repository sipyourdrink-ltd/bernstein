# Rego variants for intent-vs-args experiment (#5065)
# OPA package: bernstein.authz.intent_experiment

package bernstein.authz.intent_experiment

import future.keywords.if
import future.keywords.in

# Role-only variant: baseline control
# No args_digest, no intent_digest - pure role + resource check
role_only_allow if {
    input.role == "admin"
}

role_only_allow if {
    input.role == "coder"
    startswith(input.resource, "file:///data/")
}

role_only_allow if {
    input.role == "reader"
    endswith(input.resource, ".csv")
}

role_only_allow if {
    input.role == "operator"
    contains(input.resource, "vault")
}

role_only_deny["role_only: no matching role rule"] if {
    not role_only_allow
}

# Argument-aware variant: adds args_digest binding
# Same args_digest = same decision regardless of intent text
argument_aware_allow if {
    role_only_allow
    # args_digest must match an reviewed invocation
    args_digest_on_allowlist(input.args_digest)
}

args_digest_on_allowlist(digest) if {
    # Approved digests from corpus review
    approved_digests := {
        "sha256:a3f5c8d9e1b2c4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4",
        "sha256:b4e6d9c0f2a3b5c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c",
        "sha256:c5f7e0d1g3b4c6d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d",
    }
    digest in approved_digests
}

argument_aware_deny["argument_aware: args_digest not on approved allowlist"] if {
    not argument_aware_allow
}

# Intent-aware variant: adds intent_digest binding
# Different intent (benign vs harmful) => different intent_digest => different decision
intent_aware_allow if {
    argument_aware_allow
    intent_digest_on_allowlist(input.intent_digest, input.declared_intent)
}

# Allowlist check for intent digest - includes semantic review of declared_intent
intent_digest_on_allowlist(intent_digest, declared_intent) if {
    benign_intents := {
        {"intent_digest": "sha256:e5c8d9e1b2c4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e4f6a8b0d2e5", "text": "reading quarterly summary for business review"},
        {"intent_digest": "sha256:f6d9e0f2a3b5c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0d", "text": "rotating expired API keys during maintenance window"},
        {"intent_digest": "sha256:a7e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8", "text": "deploying verified container image to production cluster"},
    }
    some item in benign_intents
    item.intent_digest == intent_digest
    item.text == declared_intent
    not contains_malicious_pattern(declared_intent)
}

contains_malicious_pattern(text) if {
    malicious_patterns := [
        "dark web", "sell", "extract", "pivot", "exploit",
        "persistence", "corrupt", "evasion", "surveillance",
        "ransomware", "beacon", "C2", "recon",
    ]
    some pattern in malicious_patterns
    contains(text, pattern)
}

intent_aware_deny["intent_aware: declared intent matches harmful pattern"] if {
    intent_aware_allow
    contains_malicious_pattern(input.declared_intent)
}

intent_aware_deny["intent_aware: intent_digest not on approved allowlist"] if {
    not intent_aware_allow
}

# Export allow/deny decisions for each variant
allow if {
    intent_aware_allow
}

deny[msg] if {
    intent_aware_deny[msg]
}
