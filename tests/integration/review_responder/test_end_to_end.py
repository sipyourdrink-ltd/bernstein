"""End-to-end smoke test for the review-responder pipeline.

Wires the listener → bundler → responder pipeline using fakes for
GitHub I/O so the full flow runs in-process and we can assert on the
audit chain, dedup state, and emitted PR replies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from bernstein.core.review_responder import (
    DedupQueue,
    ResponderConfig,
    ReviewResponder,
    RoundBundler,
    WebhookListener,
    normalise_webhook_payload,
)
from bernstein.core.review_responder.gh_client import GhClient
from bernstein.core.review_responder.responder import GateAdvice, RunnerOutcome
from bernstein.core.review_responder.webhook import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TARGET_EVENT,
)
from bernstein.core.security.audit import AuditLog


def _signed_post(client: TestClient, secret: bytes, body: bytes) -> int:
    """POST ``body`` to ``/webhook`` with a valid GitHub-style signature."""
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: sig,
            EVENT_HEADER: TARGET_EVENT,
            "Content-Type": "application/json",
        },
    )
    return resp.status_code


def _envelope(comment_id: int, updated_at: str = "2026-04-25T10:00:00Z") -> dict[str, object]:
    """Build a plausible GitHub webhook envelope."""
    return {
        "action": "created",
        "comment": {
            "id": comment_id,
            "body": "rename foo to bar",
            "path": "src/util.py",
            "line": 42,
            "commit_id": "abc",
            "original_commit_id": "abc",
            "diff_hunk": "@@",
            "user": {"login": "alice"},
            "created_at": updated_at,
            "updated_at": updated_at,
        },
        "pull_request": {"number": 314},
        "repository": {"full_name": "chernistry/bernstein"},
    }


def _stub_runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Stub ``gh`` runner that always returns a 200 OK."""
    return subprocess.CompletedProcess(
        args=["gh", *args],
        returncode=0,
        stdout="{}",
        stderr="",
    )


def test_listener_to_audit_pipeline(tmp_path: Path) -> None:
    """A signed webhook produces a sealed round with an intact audit chain."""
    cfg = ResponderConfig(
        repo="chernistry/bernstein",
        quiet_window_s=0.0,  # seal immediately
        per_round_cost_cap_usd=1.0,
    )
    secret = b"shh"
    bundler = RoundBundler(config=cfg)
    dedup = DedupQueue(state_path=tmp_path / "dedup.json")

    def _on_payload(payload: dict[str, object]) -> None:
        comment = normalise_webhook_payload(payload)
        if dedup.offer(comment):
            bundler.add(comment)

    listener = WebhookListener(secret=secret, on_comment=_on_payload)
    client = TestClient(listener.app)

    # Two webhooks for two distinct comments — the second is a replay of the first.
    body1 = json.dumps(_envelope(1)).encode()
    body2 = json.dumps(_envelope(2, updated_at="2026-04-25T10:01:00Z")).encode()
    body3 = json.dumps(_envelope(1)).encode()  # exact replay

    assert _signed_post(client, secret, body1) == 202
    assert _signed_post(client, secret, body2) == 202
    assert _signed_post(client, secret, body3) == 202

    rounds = bundler.drain(force=True)
    # Replay was suppressed by dedup → only one round, two comments.
    assert len(rounds) == 1
    assert {c.comment_id for c in rounds[0].comments} == {1, 2}

    # Drive the round through the responder and verify the audit chain.
    audit = AuditLog(tmp_path / "audit", key=b"k")
    responder = ReviewResponder(
        config=cfg,
        runner=lambda r, p: RunnerOutcome(commit_sha="abc1234567890def", cost_usd=0.1, summary="ok"),
        audit=audit,
        dedup=dedup,
        gh=GhClient(runner=_stub_runner),
        gate_consult=lambda _r, _o: GateAdvice(allowed=True, reason="ok"),
        diff_provider=lambda _r: None,
    )
    result = responder.run_round(rounds[0])
    assert result.outcome.value == "committed"

    replay = AuditLog(tmp_path / "audit", key=b"k")
    ok, errors = replay.verify()
    assert ok, errors
    events = replay.query(event_type="review_responder.round")
    assert len(events) == 1
    assert sorted(events[0].details["comments"]) == [1, 2]


def test_replay_after_restart_is_no_op(tmp_path: Path) -> None:
    """Re-creating the dedup queue from disk suppresses already-seen comments."""
    state = tmp_path / "dedup.json"
    cfg = ResponderConfig(repo="o/r", quiet_window_s=0.0)
    bundler = RoundBundler(config=cfg)

    q1 = DedupQueue(state_path=state)
    secret = b"k"

    def _on(payload: dict[str, object]) -> None:
        c = normalise_webhook_payload(payload)
        if q1.offer(c):
            bundler.add(c)

    listener = WebhookListener(secret=secret, on_comment=_on)
    client = TestClient(listener.app)
    env = _envelope(7, updated_at="2026-04-25T10:00:00Z")
    env["repository"] = {"full_name": "o/r"}
    body = json.dumps(env).encode()
    assert _signed_post(client, secret, body) == 202

    # Simulate daemon restart — fresh queue reads the same on-disk state.
    q2 = DedupQueue(state_path=state)
    bundler2 = RoundBundler(config=cfg)
    seen: list[int] = []

    def _on2(payload: dict[str, object]) -> None:
        c = normalise_webhook_payload(payload)
        if q2.offer(c):
            bundler2.add(c)
            seen.append(c.comment_id)

    listener2 = WebhookListener(secret=secret, on_comment=_on2)
    client2 = TestClient(listener2.app)
    assert _signed_post(client2, secret, body) == 202
    assert seen == []  # replay suppressed across restart
    assert bundler2.drain(force=True) == []
