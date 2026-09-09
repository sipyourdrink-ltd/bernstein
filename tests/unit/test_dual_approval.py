"""Tests for dual_approval - two-factor approval for destructive operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bernstein.core.dual_approval import (
    ApprovalChannel,
    ApprovalRequest,
    ApprovalResponse,
    create_approval_request,
    evaluate_approval,
    format_approval_prompt,
    is_destructive,
)

# ---------------------------------------------------------------------------
# is_destructive
# ---------------------------------------------------------------------------


class TestIsDestructive:
    def test_matches_force_push(self) -> None:
        assert is_destructive("git push --force origin main") is True

    def test_matches_hard_reset(self) -> None:
        assert is_destructive("git reset --hard HEAD~3") is True

    def test_matches_database_migrate(self) -> None:
        assert is_destructive("database migrate --prod") is True

    def test_matches_production_deploy(self) -> None:
        assert is_destructive("production deploy v2.1") is True

    def test_matches_drop_table(self) -> None:
        assert is_destructive("DROP TABLE users") is True

    def test_matches_delete_branch(self) -> None:
        assert is_destructive("delete branch feature/old") is True

    def test_case_insensitive(self) -> None:
        assert is_destructive("GIT PUSH --FORCE origin main") is True

    def test_non_destructive_push(self) -> None:
        assert is_destructive("git push origin main") is False

    def test_non_destructive_commit(self) -> None:
        assert is_destructive("git commit -m 'fix'") is False

    def test_non_destructive_empty(self) -> None:
        assert is_destructive("") is False

    def test_non_destructive_select(self) -> None:
        assert is_destructive("SELECT * FROM users") is False


# ---------------------------------------------------------------------------
# create_approval_request
# ---------------------------------------------------------------------------


class TestCreateApprovalRequest:
    def test_creates_request_with_defaults(self) -> None:
        req = create_approval_request("git push --force", "agent-1")
        assert req.operation == "git push --force"
        assert req.requester == "agent-1"
        assert req.required_approvals == 2
        assert req.channels == [ApprovalChannel.CLI, ApprovalChannel.SLACK]
        assert req.request_id  # non-empty UUID
        assert req.created_at
        assert req.expires_at

    def test_custom_channels(self) -> None:
        channels = [ApprovalChannel.WEBHOOK, ApprovalChannel.EMAIL]
        req = create_approval_request("drop table", "admin", channels=channels)
        assert req.channels == channels

    def test_custom_ttl(self) -> None:
        req = create_approval_request("deploy", "ci", ttl_seconds=60)
        created = datetime.fromisoformat(req.created_at)
        expires = datetime.fromisoformat(req.expires_at)
        delta = expires - created
        assert 59 <= delta.total_seconds() <= 61

    def test_reason_populated(self) -> None:
        req = create_approval_request("git reset --hard", "dev")
        assert "git reset --hard" in req.reason


# ---------------------------------------------------------------------------
# evaluate_approval
# ---------------------------------------------------------------------------


def _make_request(
    *,
    required: int = 2,
    ttl_seconds: int = 300,
) -> ApprovalRequest:
    """Helper to build a request with controllable expiry."""
    now = datetime.now(tz=UTC)
    expires = now + timedelta(seconds=ttl_seconds)
    return ApprovalRequest(
        request_id="test-req-1",
        operation="git push --force",
        reason="testing",
        requester="agent-1",
        channels=[ApprovalChannel.CLI, ApprovalChannel.SLACK],
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
        required_approvals=required,
    )


def _make_response(
    *,
    approved: bool = True,
    approver: str = "reviewer-1",
    channel: ApprovalChannel = ApprovalChannel.CLI,
) -> ApprovalResponse:
    now = datetime.now(tz=UTC)
    return ApprovalResponse(
        request_id="test-req-1",
        channel=channel,
        approver=approver,
        approved=approved,
        timestamp=now.isoformat(),
    )


class TestEvaluateApproval:
    def test_approved_with_enough_responses(self) -> None:
        req = _make_request(required=2)
        responses = [
            _make_response(approver="a", channel=ApprovalChannel.CLI),
            _make_response(approver="b", channel=ApprovalChannel.SLACK),
        ]
        status = evaluate_approval(req, responses)
        assert status.is_approved is True
        assert status.is_denied is False
        assert status.is_expired is False

    def test_not_approved_insufficient_responses(self) -> None:
        req = _make_request(required=2)
        responses = [_make_response(approver="a")]
        status = evaluate_approval(req, responses)
        assert status.is_approved is False
        assert status.is_denied is False

    def test_not_approved_zero_responses(self) -> None:
        req = _make_request(required=2)
        status = evaluate_approval(req, [])
        assert status.is_approved is False
        assert status.is_denied is False

    def test_denied_single_denial(self) -> None:
        req = _make_request(required=1)
        responses = [
            _make_response(approved=True, approver="a"),
            _make_response(approved=False, approver="b"),
        ]
        status = evaluate_approval(req, responses)
        assert status.is_denied is True
        assert status.is_approved is False

    def test_expired_request(self) -> None:
        req = _make_request(ttl_seconds=-10)  # already expired
        responses = [
            _make_response(approver="a"),
            _make_response(approver="b"),
        ]
        status = evaluate_approval(req, responses)
        assert status.is_expired is True
        assert status.is_approved is False

    def test_responses_preserved_in_status(self) -> None:
        req = _make_request(required=1)
        responses = [_make_response(approver="a")]
        status = evaluate_approval(req, responses)
        assert len(status.responses) == 1
        assert status.request is req


# ---------------------------------------------------------------------------
# format_approval_prompt
# ---------------------------------------------------------------------------


class TestFormatApprovalPrompt:
    def test_contains_key_fields(self) -> None:
        req = _make_request()
        output = format_approval_prompt(req)
        assert "APPROVAL REQUIRED" in output
        assert req.request_id in output
        assert req.operation in output
        assert req.requester in output
        assert "2 required" in output

    def test_channels_listed(self) -> None:
        req = _make_request()
        output = format_approval_prompt(req)
        assert "cli" in output
        assert "slack" in output


# ---------------------------------------------------------------------------
# Two approvals have to be two
# ---------------------------------------------------------------------------
#
# ``govern/apply.py`` gates every removal-class change on an ApprovalStatus
# from here, and says it delegates "rather than re-implementing approval
# semantics". So whatever this function counts is what stands between a plan
# and a destructive change.


def _response(
    *,
    request_id: str = "req-1",
    channel: ApprovalChannel = ApprovalChannel.CLI,
    approver: str = "bob",
    approved: bool = True,
) -> ApprovalResponse:
    return ApprovalResponse(
        request_id=request_id,
        channel=channel,
        approver=approver,
        approved=approved,
        timestamp=datetime.now(tz=UTC).isoformat(),
    )


def test_one_approver_clicking_twice_is_not_two_approvals() -> None:
    """Two rows on one channel are one party's say-so.

    This is the whole point of the module: a destructive operation needs a
    second sign-off. Counting responses instead of channels let a single
    compromised Slack account satisfy the gate by itself.
    """
    request = create_approval_request("git push --force", requester="alice")
    twice = [
        _response(request_id=request.request_id, channel=ApprovalChannel.SLACK, approver="mallory"),
        _response(request_id=request.request_id, channel=ApprovalChannel.SLACK, approver="mallory"),
    ]

    assert evaluate_approval(request, twice).is_approved is False


def test_approvals_on_two_channels_are_two_approvals() -> None:
    """The control: the documented happy path still approves."""
    request = create_approval_request("git push --force", requester="alice")
    both = [
        _response(request_id=request.request_id, channel=ApprovalChannel.CLI, approver="bob"),
        _response(request_id=request.request_id, channel=ApprovalChannel.SLACK, approver="carol"),
    ]

    assert evaluate_approval(request, both).is_approved is True


def test_approvals_for_a_different_request_do_not_count() -> None:
    """``request_id`` exists to bind a response to what it approves.

    Ignoring it meant approvals collected for one operation satisfied the gate
    on another - a "delete branch" sign-off standing in for a production
    deploy.
    """
    request = create_approval_request("production deploy", requester="alice")
    elsewhere = [
        _response(request_id="some-other-request", channel=ApprovalChannel.CLI, approver="bob"),
        _response(request_id="yet-another", channel=ApprovalChannel.SLACK, approver="carol"),
    ]

    assert evaluate_approval(request, elsewhere).is_approved is False


def test_a_denial_for_a_different_request_does_not_veto_this_one() -> None:
    """Binding cuts both ways, or it is not binding.

    A filter applied only to approvals would let an unrelated denial veto a
    properly approved request - the same confusion, pointed the other way.
    """
    request = create_approval_request("production deploy", requester="alice")
    mixed = [
        _response(request_id=request.request_id, channel=ApprovalChannel.CLI, approver="bob"),
        _response(request_id=request.request_id, channel=ApprovalChannel.SLACK, approver="carol"),
        _response(request_id="unrelated", channel=ApprovalChannel.EMAIL, approver="dave", approved=False),
    ]

    status = evaluate_approval(request, mixed)
    assert status.is_denied is False
    assert status.is_approved is True


def test_a_denial_on_this_request_still_vetoes() -> None:
    """The existing veto rule is unchanged, now scoped to the right request."""
    request = create_approval_request("production deploy", requester="alice")
    mixed = [
        _response(request_id=request.request_id, channel=ApprovalChannel.CLI, approver="bob"),
        _response(request_id=request.request_id, channel=ApprovalChannel.SLACK, approver="carol"),
        _response(request_id=request.request_id, channel=ApprovalChannel.EMAIL, approver="dave", approved=False),
    ]

    status = evaluate_approval(request, mixed)
    assert status.is_denied is True
    assert status.is_approved is False


def test_the_status_still_reports_every_submitted_response() -> None:
    """Filtering decides the verdict; it must not hide what was submitted."""
    request = create_approval_request("production deploy", requester="alice")
    submitted = [
        _response(request_id=request.request_id, channel=ApprovalChannel.CLI),
        _response(request_id="unrelated", channel=ApprovalChannel.SLACK),
    ]

    assert evaluate_approval(request, submitted).responses == submitted
