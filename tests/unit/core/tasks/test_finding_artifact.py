"""Identity rule for the ``finding`` artifact kind (#2953).

The kind exists so an analysis result can be content-addressed *and* bound to
the invocation that produced it. Both halves are load-bearing: the projection
must survive a cosmetic edit, and it must refuse to mint an identity for a
finding that names no tool, no version and no invocation.
"""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.core.tasks.artifacts import (
    ArtifactKind,
    CanonicalisationError,
    artifact_content_hash,
)


def _finding(**overrides: Any) -> dict[str, Any]:
    """A fully-bound finding; tests override exactly the field under test."""
    payload: dict[str, Any] = {
        "ruleId": "G101",
        "artifactLocation": {"uri": "src/app.py"},
        "region": {"startLine": 10, "snippet": {"text": "password = 'hunter2'"}},
        "tool": "gitleaks",
        "tool_version": "8.18.0",
        "pinned_digest": "sha256:abc",
        "invocation_argv_hash": "sha256:def",
        "target": "src/app.py",
    }
    payload.update(overrides)
    return payload


def test_finding_identity_stable_across_line_shift():
    # The exact same finding, but startLine moved from 10 to 11
    finding_at_line_10 = _finding()
    finding_at_line_11 = _finding(
        region={"startLine": 11, "snippet": {"text": "password = 'hunter2'"}},  # Line shifted!
    )

    hash_10 = artifact_content_hash(ArtifactKind.FINDING, finding_at_line_10)
    hash_11 = artifact_content_hash(ArtifactKind.FINDING, finding_at_line_11)

    # Cosmetic line shift must not change the hash
    assert hash_10 == hash_11


def test_finding_identity_changes_when_snippet_changes():
    finding_1 = _finding()
    finding_2 = _finding(
        region={"startLine": 10, "snippet": {"text": "password = 'hunter3'"}},  # Snippet changed!
    )

    hash_1 = artifact_content_hash(ArtifactKind.FINDING, finding_1)
    hash_2 = artifact_content_hash(ArtifactKind.FINDING, finding_2)

    # Rule is not just ignoring everything; snippet matters
    assert hash_1 != hash_2


def test_windows_path_separators_do_not_change_finding_identity():
    posix = _finding(artifactLocation={"uri": "src/app.py"})
    windows = _finding(artifactLocation={"uri": "src\\app.py"})

    assert artifact_content_hash(ArtifactKind.FINDING, posix) == artifact_content_hash(ArtifactKind.FINDING, windows)


@pytest.mark.parametrize("omitted", ["tool", "tool_version", "invocation_argv_hash", "target", "ruleId"])
def test_unbound_finding_is_rejected_not_given_an_identity(omitted: str):
    """A finding that names no tool/version/invocation must not hash at all.

    Defaulting the missing field to ``""`` would hand an unbound finding a
    stable, valid-looking identity - and the binding is the whole point of the
    kind.
    """
    payload = _finding()
    del payload[omitted]

    with pytest.raises(CanonicalisationError, match=omitted):
        artifact_content_hash(ArtifactKind.FINDING, payload)


def test_finding_without_a_location_is_rejected():
    """No ``artifactLocation.uri`` means the finding points at nothing."""
    no_block = _finding()
    del no_block["artifactLocation"]
    with pytest.raises(CanonicalisationError, match="artifactLocation"):
        artifact_content_hash(ArtifactKind.FINDING, no_block)

    with pytest.raises(CanonicalisationError, match=r"artifactLocation\.uri"):
        artifact_content_hash(ArtifactKind.FINDING, _finding(artifactLocation={}))


@pytest.mark.parametrize("blank", ["tool", "tool_version", "invocation_argv_hash", "target", "ruleId"])
def test_blank_binding_field_is_rejected(blank: str):
    with pytest.raises(CanonicalisationError, match=blank):
        artifact_content_hash(ArtifactKind.FINDING, _finding(**{blank: ""}))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("tool_version", 8),
        ("ruleId", 101),
        ("target", None),
        ("tool", ["gitleaks"]),
    ],
)
def test_non_string_binding_field_is_rejected_not_coerced(field_name: str, value: Any):
    """``str(8)`` and ``"8"`` are two bindings; they must not share one identity."""
    with pytest.raises(CanonicalisationError, match=field_name):
        artifact_content_hash(ArtifactKind.FINDING, _finding(**{field_name: value}))


def test_unknown_finding_field_is_rejected_not_silently_dropped():
    """A misspelt binding key must fail loudly, not unbind the finding quietly."""
    payload = _finding()
    payload["toolVersion"] = payload.pop("tool_version")

    with pytest.raises(CanonicalisationError, match="toolVersion"):
        artifact_content_hash(ArtifactKind.FINDING, payload)


def test_empty_pinned_digest_is_the_one_legal_blank():
    """A ``deterministic``-tier adapter has no feed to pin and says so explicitly."""
    unpinned = artifact_content_hash(ArtifactKind.FINDING, _finding(pinned_digest=""))
    pinned = artifact_content_hash(ArtifactKind.FINDING, _finding(pinned_digest="sha256:abc"))

    assert unpinned != pinned


def test_finding_with_no_snippet_still_has_an_identity():
    """Not every finding has source text - a port observation has none."""
    no_snippet = artifact_content_hash(ArtifactKind.FINDING, _finding(region={}))
    empty_snippet = artifact_content_hash(ArtifactKind.FINDING, _finding(region={"snippet": {"text": ""}}))

    assert no_snippet == empty_snippet


def test_finding_payload_must_be_a_mapping():
    with pytest.raises(CanonicalisationError, match="mapping"):
        artifact_content_hash(ArtifactKind.FINDING, ["not", "a", "mapping"])
