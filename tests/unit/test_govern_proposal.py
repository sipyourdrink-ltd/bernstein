"""Tests for ``govern discover --assist`` DraftProposal (issue #5020, #4981)."""

from __future__ import annotations

from bernstein.core.govern import DraftProposal, ProposalStatus


def test_draft_proposal_round_trip() -> None:
    """DraftProposal to_dict / from_dict should preserve all fields."""
    dp = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={
            "forbidden": [{"surface": "arn:aws:s3:::public-bucket", "clause": "No public S3 buckets"}],
            "required": [],
            "permitted": [],
        },
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    raw = dp.to_dict()
    dp2 = DraftProposal.from_dict(raw)
    assert dp2.findings_hash == dp.findings_hash
    assert dp2.prompt == dp.prompt
    assert dp2.playbook == dp.playbook
    assert dp2.model == dp.model
    assert dp2.timestamp == dp.timestamp
    assert dp2.status == dp.status
    assert dp2.human_signature == dp.human_signature


def test_draft_proposal_content_hash_deterministic() -> None:
    """Two identical DraftProposals must produce byte-identical canonical bytes."""
    dp1 = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={
            "forbidden": [{"surface": "arn:aws:s3:::public-bucket", "clause": "No public S3 buckets"}],
            "required": [],
            "permitted": [],
        },
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    dp2 = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={
            "forbidden": [{"surface": "arn:aws:s3:::public-bucket", "clause": "No public S3 buckets"}],
            "required": [],
            "permitted": [],
        },
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    assert dp1.content_hash() == dp2.content_hash()
    assert dp1.content_hash().startswith("sha256:")


def test_draft_proposal_to_canonical_bytes_deterministic() -> None:
    """Two identical DraftProposals must produce byte-identical canonical bytes."""
    dp1 = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={
            "forbidden": [{"surface": "arn:aws:s3:::public-bucket", "clause": "No public S3 buckets"}],
            "required": [],
            "permitted": [],
        },
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    dp2 = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={
            "forbidden": [{"surface": "arn:aws:s3:::public-bucket", "clause": "No public S3 buckets"}],
            "required": [],
            "permitted": [],
        },
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    assert dp1.to_canonical_bytes() == dp2.to_canonical_bytes()


def test_draft_proposal_is_signed() -> None:
    """is_signed() should return False for DRAFT status and True for SIGNED with signature."""
    # Draft status, no signature -> not signed
    dp_draft = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={},
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    assert not dp_draft.is_signed()

    # Draft status with signature (should not happen but handle gracefully)
    dp_draft_with_sig = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={},
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature="hmac-signature",
    )
    assert not dp_draft_with_sig.is_signed()  # Status is DRAFT, so not signed

    # Signed status with signature -> signed
    dp_signed = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={},
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.SIGNED,
        human_signature="hmac-signature",
    )
    assert dp_signed.is_signed()

    # Rejected status -> not signed
    dp_rejected = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={},
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.REJECTED,
        human_signature=None,
    )
    assert not dp_rejected.is_signed()


def test_draft_proposal_sign() -> None:
    """sign() should create a new DraftProposal with SIGNED status and signature."""
    dp_draft = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook from findings",
        playbook={
            "forbidden": [{"surface": "arn:aws:s3:::public-bucket", "clause": "No public S3 buckets"}],
            "required": [],
            "permitted": [],
        },
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    dp_signed = dp_draft.sign("human-hmac-signature")

    assert dp_signed.findings_hash == dp_draft.findings_hash
    assert dp_signed.prompt == dp_draft.prompt
    assert dp_signed.playbook == dp_draft.playbook
    assert dp_signed.model == dp_draft.model
    assert dp_signed.timestamp == dp_draft.timestamp
    assert dp_signed.status == ProposalStatus.SIGNED
    assert dp_signed.human_signature == "human-hmac-signature"

    # Original should be unchanged
    assert dp_draft.status == ProposalStatus.DRAFT
    assert dp_draft.human_signature is None


def test_draft_proposal_status_enum() -> None:
    """ProposalStatus enum should have expected values."""
    assert ProposalStatus.DRAFT.value == "draft"
    assert ProposalStatus.SIGNED.value == "signed"
    assert ProposalStatus.REJECTED.value == "rejected"


def test_draft_proposal_findings_hash_reference() -> None:
    """DraftProposal should preserve findings_hash reference."""
    dp = DraftProposal(
        findings_hash="sha256:abcd1234567890",
        prompt="Generate a draft playbook",
        playbook={},
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    assert dp.findings_hash == "sha256:abcd1234567890"

    raw = dp.to_dict()
    assert raw["findings_hash"] == "sha256:abcd1234567890"


def test_draft_proposal_malformed_draft() -> None:
    """Malformed draft (e.g., missing playbook) should still serialize correctly."""
    dp = DraftProposal(
        findings_hash="sha256:abc123def456",
        prompt="Generate a draft playbook",
        playbook=None,  # Malformed but will be treated as dict in real usage
        model="claude-3-opus",
        timestamp=1234567890,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )
    raw = dp.to_dict()
    assert raw["findings_hash"] == "sha256:abc123def456"
    assert raw["prompt"] == "Generate a draft playbook"
    assert raw["playbook"] is None
    assert raw["status"] == "draft"
