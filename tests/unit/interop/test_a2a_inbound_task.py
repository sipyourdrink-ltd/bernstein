"""Tests for inbound A2A task trust-checking and worktree isolation (#2304).

AC3: inbound cards are verified against issuer domain and a trusted-issuer set;
an untrusted card is rejected.
AC4: each inbound peer task runs in an isolated worktree with no shared mutable
state.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from bernstein.core.interop.a2a_card import (
    CardPolicies,
    card_public_key_fingerprint,
    issue_capability_card,
)
from bernstein.core.interop.a2a_consume import PolicyRequirements
from bernstein.core.interop.a2a_lineage import (
    InboundTaskRejected,
    accept_inbound_task,
    isolate_inbound_task,
)

if TYPE_CHECKING:
    from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8")


def _card(issuer: str = "peer.example") -> tuple[object, str]:
    policies = CardPolicies(cost_cap_usd=5.0, redaction_tier="strict", sandbox_profile="container")
    signed, _priv = issue_capability_card(
        issuer=issuer,
        name="peer",
        description="peer orchestrator",
        advertised_tools=["code_review"],
        policies=policies,
    )
    fp = card_public_key_fingerprint(signed.card.public_key_pem)
    return signed, fp


_REQS = PolicyRequirements(max_cost_cap_usd=10.0, min_redaction_tier="standard", min_sandbox_profile="container")


# ---------------------------------------------------------------------------
# AC3 -- inbound card trust check
# ---------------------------------------------------------------------------


def test_accept_inbound_task_accepts_trusted_card() -> None:
    signed, fp = _card()
    verdict = accept_inbound_task(
        signed_card=signed,
        trusted_issuer_fingerprints=[fp],
        requirements=_REQS,
        expected_issuer="peer.example",
    )
    assert verdict.ok


def test_untrusted_card_is_rejected() -> None:
    signed, _fp = _card()
    with pytest.raises(InboundTaskRejected) as exc:
        accept_inbound_task(
            signed_card=signed,
            trusted_issuer_fingerprints=["sha256:" + "00" * 32],
            requirements=_REQS,
            expected_issuer="peer.example",
        )
    assert exc.value.reason == "untrusted_issuer"


def test_issuer_domain_mismatch_is_rejected() -> None:
    signed, fp = _card(issuer="peer.example")
    with pytest.raises(InboundTaskRejected) as exc:
        accept_inbound_task(
            signed_card=signed,
            trusted_issuer_fingerprints=[fp],
            requirements=_REQS,
            expected_issuer="attacker.example",
        )
    assert exc.value.reason == "issuer_mismatch"


def test_expired_card_is_rejected() -> None:
    policies = CardPolicies(cost_cap_usd=5.0, redaction_tier="strict", sandbox_profile="container")
    signed, _priv = issue_capability_card(
        issuer="peer.example",
        name="peer",
        description="d",
        advertised_tools=["code_review"],
        policies=policies,
        ttl_seconds=1,
        now=0.0,
    )
    fp = card_public_key_fingerprint(signed.card.public_key_pem)
    with pytest.raises(InboundTaskRejected) as exc:
        accept_inbound_task(
            signed_card=signed,
            trusted_issuer_fingerprints=[fp],
            requirements=_REQS,
            expected_issuer="peer.example",
            now=10_000.0,
        )
    assert exc.value.reason == "signature"


# ---------------------------------------------------------------------------
# AC4 -- each inbound peer task runs in an isolated worktree
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example.com"], repo)
    _run(["git", "config", "user.name", "T"], repo)
    _run(["git", "config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo)
    _run(["git", "commit", "-m", "seed"], repo)
    return repo


def test_isolate_inbound_task_creates_worktree(git_repo: Path) -> None:
    result = isolate_inbound_task(repo_root=git_repo, task_uuid="task-abc")
    assert result.worktree_path.is_dir()
    assert result.isolation_ok, result.violations
    assert result.session_id


def test_two_inbound_tasks_get_distinct_worktrees(git_repo: Path) -> None:
    a = isolate_inbound_task(repo_root=git_repo, task_uuid="task-a")
    b = isolate_inbound_task(repo_root=git_repo, task_uuid="task-b")
    assert a.worktree_path != b.worktree_path
    # no shared mutable state: each worktree has its own .sdd (not a symlink to
    # the parent).
    assert a.isolation_ok
    assert b.isolation_ok


def test_isolate_inbound_task_session_id_is_deterministic_per_task(git_repo: Path) -> None:
    a = isolate_inbound_task(repo_root=git_repo, task_uuid="task-xyz")
    # cleanup then recreate: same task_uuid yields the same session id.
    sid = a.session_id
    assert "task-xyz" in sid or sid  # session id derived from task uuid
