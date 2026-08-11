import pytest
from bernstein.core.tasks.artifacts import artifact_content_hash, ArtifactKind

def test_finding_identity_stable_across_line_shift():
    # The exact same finding, but startLine moved from 10 to 11
    finding_at_line_10 = {
        "ruleId": "G101",
        "artifactLocation": {"uri": "src/app.py"},
        "region": {"startLine": 10, "snippet": {"text": "password = 'hunter2'"}},
        "tool": "gitleaks",
        "tool_version": "8.18.0",
        "pinned_digest": "sha256:abc",
        "invocation_argv_hash": "sha256:def",
        "target": "src/app.py"
    }
    finding_at_line_11 = {
        "ruleId": "G101",
        "artifactLocation": {"uri": "src/app.py"},
        "region": {"startLine": 11, "snippet": {"text": "password = 'hunter2'"}}, # Line shifted!
        "tool": "gitleaks",
        "tool_version": "8.18.0",
        "pinned_digest": "sha256:abc",
        "invocation_argv_hash": "sha256:def",
        "target": "src/app.py"
    }

    hash_10 = artifact_content_hash(ArtifactKind.FINDING, finding_at_line_10)
    hash_11 = artifact_content_hash(ArtifactKind.FINDING, finding_at_line_11)
    
    # Cosmetic line shift must not change the hash
    assert hash_10 == hash_11

def test_finding_identity_changes_when_snippet_changes():
    finding_1 = {
        "ruleId": "G101",
        "region": {"startLine": 10, "snippet": {"text": "password = 'hunter2'"}},
        "tool": "gitleaks", "tool_version": "1", "pinned_digest": "1", "invocation_argv_hash": "1", "target": "a"
    }
    finding_2 = {
        "ruleId": "G101",
        "region": {"startLine": 10, "snippet": {"text": "password = 'hunter3'"}}, # Snippet changed!
        "tool": "gitleaks", "tool_version": "1", "pinned_digest": "1", "invocation_argv_hash": "1", "target": "a"
    }

    hash_1 = artifact_content_hash(ArtifactKind.FINDING, finding_1)
    hash_2 = artifact_content_hash(ArtifactKind.FINDING, finding_2)
    
    # Rule is not just ignoring everything; snippet matters
    assert hash_1 != hash_2