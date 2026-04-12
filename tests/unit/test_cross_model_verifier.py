"""Tests for cross-model verification pipeline."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.cross_model_verifier import (
    CrossModelVerdict,
    CrossModelVerifierConfig,
    _build_prompt,
    _get_diff,
    _parse_response,
    run_cross_model_verification_sync,
    select_reviewer_model,
    verify_with_cross_model,
)
from bernstein.core.models import Task

if TYPE_CHECKING:
    from pathlib import Path


def _make_task(
    *,
    id: str = "T-001",
    title: str = "Add login endpoint",
    description: str = "Implement POST /auth/login",
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role="backend",
        owned_files=owned_files or [],
    )


# ---------------------------------------------------------------------------
# select_reviewer_model
# ---------------------------------------------------------------------------


class TestSelectReviewerModel:
    def test_claude_writer_gets_gemini_reviewer(self) -> None:
        reviewer = select_reviewer_model("anthropic/claude-sonnet-4-20250514")
        assert "gemini" in reviewer

    def test_gemini_writer_gets_claude_reviewer(self) -> None:
        reviewer = select_reviewer_model("google/gemini-flash-1.5")
        assert "claude" in reviewer

    def test_codex_writer_gets_claude_reviewer(self) -> None:
        reviewer = select_reviewer_model("openai/codex-mini")
        assert "claude" in reviewer

    def test_override_takes_priority(self) -> None:
        override = "openai/gpt-5.4-mini"
        reviewer = select_reviewer_model("claude-sonnet", override=override)
        assert reviewer == override

    def test_unknown_writer_returns_default(self) -> None:
        reviewer = select_reviewer_model("some/unknown-model-xyz")
        assert reviewer  # returns non-empty default


# ---------------------------------------------------------------------------
# _get_diff
# ---------------------------------------------------------------------------


class TestGetDiff:
    def test_returns_diff_output(self, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.stdout = "diff --git a/foo.py b/foo.py\n+print('hello')\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            diff = _get_diff(tmp_path, [])
        assert "foo.py" in diff
        mock_run.assert_called_once()

    def test_falls_back_when_head1_empty(self, tmp_path: Path) -> None:
        empty_result = MagicMock()
        empty_result.stdout = ""
        fallback_result = MagicMock()
        fallback_result.stdout = "+fallback diff\n"
        with patch("subprocess.run", side_effect=[empty_result, fallback_result]):
            diff = _get_diff(tmp_path, [])
        assert "fallback" in diff

    def test_returns_placeholder_on_oserror(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=OSError("no git")):
            diff = _get_diff(tmp_path, [])
        assert diff == "(failed to get git diff)"

    def test_returns_placeholder_on_timeout(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            diff = _get_diff(tmp_path, [])
        assert diff == "(failed to get git diff)"

    def test_passes_owned_files_to_git(self, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.stdout = "+line\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _get_diff(tmp_path, ["src/foo.py", "src/bar.py"])
        cmd = mock_run.call_args[0][0]
        assert "src/foo.py" in cmd
        assert "src/bar.py" in cmd


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_approve_verdict(self) -> None:
        raw = json.dumps({"verdict": "approve", "feedback": "Looks good.", "issues": []})
        verdict = _parse_response(raw, "google/gemini-flash-1.5")
        assert verdict.verdict == "approve"
        assert verdict.feedback == "Looks good."
        assert verdict.issues == []

    def test_request_changes_verdict(self) -> None:
        raw = json.dumps(
            {
                "verdict": "request_changes",
                "feedback": "SQL injection risk.",
                "issues": ["Unsanitised input in query"],
            }
        )
        verdict = _parse_response(raw, "google/gemini-flash-1.5")
        assert verdict.verdict == "request_changes"
        assert "SQL injection" in verdict.feedback
        assert len(verdict.issues) == 1

    def test_strips_markdown_fences(self) -> None:
        raw = "```json\n" + json.dumps({"verdict": "approve", "feedback": "OK", "issues": []}) + "\n```"
        verdict = _parse_response(raw, "g/m")
        assert verdict.verdict == "approve"

    def test_extracts_json_from_surrounding_text(self) -> None:
        inner = json.dumps({"verdict": "approve", "feedback": "Fine", "issues": []})
        raw = f"Here is my review:\n{inner}\nEnd."
        verdict = _parse_response(raw, "g/m")
        assert verdict.verdict == "approve"

    def test_defaults_to_approve_on_invalid_json(self) -> None:
        verdict = _parse_response("not json at all", "g/m")
        assert verdict.verdict == "approve"
        assert "unparseable" in verdict.feedback.lower()

    def test_reviewer_model_propagated(self) -> None:
        raw = json.dumps({"verdict": "approve", "feedback": "Fine", "issues": []})
        verdict = _parse_response(raw, "my/model")
        assert verdict.reviewer_model == "my/model"

    def test_unknown_verdict_defaults_to_approve(self) -> None:
        raw = json.dumps({"verdict": "UNKNOWN", "feedback": "Hmm", "issues": []})
        verdict = _parse_response(raw, "g/m")
        assert verdict.verdict == "approve"


# ---------------------------------------------------------------------------
# verify_with_cross_model (async)
# ---------------------------------------------------------------------------


class TestVerifyWithCrossModel:
    @pytest.mark.asyncio
    async def test_approve_path(self, tmp_path: Path) -> None:
        task = _make_task()
        config = CrossModelVerifierConfig(enabled=True)
        diff_response = MagicMock(stdout="+print('hello')\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "Fine", "issues": []})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch(
                "bernstein.core.cross_model_verifier.call_llm",
                new=AsyncMock(return_value=approve_json),
            ),
        ):
            verdict = await verify_with_cross_model(task, tmp_path, "claude-sonnet", config)

        assert verdict.verdict == "approve"

    @pytest.mark.asyncio
    async def test_request_changes_path(self, tmp_path: Path) -> None:
        task = _make_task()
        config = CrossModelVerifierConfig(enabled=True)
        diff_response = MagicMock(stdout="+dangerous_code()\n")
        reject_json = json.dumps(
            {
                "verdict": "request_changes",
                "feedback": "Dangerous call.",
                "issues": ["Unvalidated input"],
            }
        )

        with (
            patch("subprocess.run", return_value=diff_response),
            patch(
                "bernstein.core.cross_model_verifier.call_llm",
                new=AsyncMock(return_value=reject_json),
            ),
        ):
            verdict = await verify_with_cross_model(task, tmp_path, "claude-sonnet", config)

        assert verdict.verdict == "request_changes"
        assert verdict.issues == ["Unvalidated input"]

    @pytest.mark.asyncio
    async def test_llm_failure_defaults_to_approve(self, tmp_path: Path) -> None:
        task = _make_task()
        config = CrossModelVerifierConfig(enabled=True)
        diff_response = MagicMock(stdout="+x\n")

        with (
            patch("subprocess.run", return_value=diff_response),
            patch(
                "bernstein.core.cross_model_verifier.call_llm",
                new=AsyncMock(side_effect=RuntimeError("API down")),
            ),
        ):
            verdict = await verify_with_cross_model(task, tmp_path, "claude-sonnet", config)

        assert verdict.verdict == "approve"
        assert "failed" in verdict.feedback.lower()

    @pytest.mark.asyncio
    async def test_diff_truncated_at_max_chars(self, tmp_path: Path) -> None:
        task = _make_task()
        config = CrossModelVerifierConfig(enabled=True, max_diff_chars=50)
        long_diff = "+" + "x" * 200
        diff_response = MagicMock(stdout=long_diff)
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "issues": []})

        captured_prompt: list[str] = []

        async def fake_llm(prompt: str, **kwargs: object) -> str:
            captured_prompt.append(prompt)
            return approve_json

        with (
            patch("subprocess.run", return_value=diff_response),
            patch("bernstein.core.quality.cross_model_verifier.call_llm", new=fake_llm),
        ):
            await verify_with_cross_model(task, tmp_path, "claude-sonnet", config)

        assert "(truncated)" in captured_prompt[0]

    @pytest.mark.asyncio
    async def test_reviewer_model_in_verdict(self, tmp_path: Path) -> None:
        task = _make_task()
        config = CrossModelVerifierConfig(enabled=True, reviewer_model="openai/gpt-5.4-mini")
        diff_response = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "issues": []})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch(
                "bernstein.core.cross_model_verifier.call_llm",
                new=AsyncMock(return_value=approve_json),
            ),
        ):
            verdict = await verify_with_cross_model(task, tmp_path, "claude-sonnet", config)

        assert verdict.reviewer_model == "openai/gpt-5.4-mini"


# ---------------------------------------------------------------------------
# run_cross_model_verification_sync
# ---------------------------------------------------------------------------


class TestRunCrossModelVerificationSync:
    def test_sync_wrapper_returns_verdict(self, tmp_path: Path) -> None:
        task = _make_task()
        config = CrossModelVerifierConfig(enabled=True)
        diff_response = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "issues": []})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch(
                "bernstein.core.cross_model_verifier.call_llm",
                new=AsyncMock(return_value=approve_json),
            ),
        ):
            verdict = run_cross_model_verification_sync(task, tmp_path, "claude-sonnet", config)

        assert isinstance(verdict, CrossModelVerdict)
        assert verdict.verdict == "approve"


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_contains_task_title(self) -> None:
        task = _make_task(title="My special task")
        prompt = _build_prompt(task, "diff content")
        assert "My special task" in prompt

    def test_contains_diff(self) -> None:
        task = _make_task()
        prompt = _build_prompt(task, "+added line\n-removed line")
        assert "+added line" in prompt

    def test_description_truncated_to_2000(self) -> None:
        # Description with a long unique tail
        tail = "ZZZTRUNCATIONMARKER" * 100  # distinctive, won't appear in template
        long_desc = "A" * 2000 + tail
        task = _make_task(description=long_desc)
        prompt = _build_prompt(task, "diff")
        assert "A" * 2000 in prompt
        assert "ZZZTRUNCATIONMARKER" not in prompt

    def test_prompt_includes_style_check(self) -> None:
        task = _make_task()
        prompt = _build_prompt(task, "diff")
        assert "Style" in prompt or "style" in prompt

    def test_prompt_includes_scope_check(self) -> None:
        task = _make_task()
        prompt = _build_prompt(task, "diff")
        assert "Scope" in prompt or "scope" in prompt


# ---------------------------------------------------------------------------
# CrossModelVerifierConfig defaults
# ---------------------------------------------------------------------------


class TestCrossModelVerifierConfigDefaults:
    def test_enabled_by_default(self) -> None:
        config = CrossModelVerifierConfig()
        assert config.enabled is True

    def test_can_disable_explicitly(self) -> None:
        config = CrossModelVerifierConfig(enabled=False)
        assert config.enabled is False

    def test_voting_config_defaults_to_none(self) -> None:
        config = CrossModelVerifierConfig()
        assert config.voting_config is None


# ---------------------------------------------------------------------------
# Multi-voter path (voting_config set)
# ---------------------------------------------------------------------------


class TestMultiVoterVerification:
    @pytest.mark.asyncio
    async def test_quorum_2_of_2_both_approve(self, tmp_path: Path) -> None:
        from bernstein.core.voting import VotingConfig, VotingStrategy

        task = _make_task()
        voting_cfg = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=2)
        config = CrossModelVerifierConfig(voting_config=voting_cfg)

        diff_response = MagicMock(stdout="+code\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            verdict = await verify_with_cross_model(
                task,
                tmp_path,
                "claude-sonnet",
                config,
                voter_models=["google/gemini-flash-1.5", "anthropic/claude-haiku-4-5-20251001"],
            )

        assert verdict.verdict == "approve"

    @pytest.mark.asyncio
    async def test_quorum_2_of_2_one_reject_fails(self, tmp_path: Path) -> None:
        from bernstein.core.voting import VotingConfig, VotingStrategy

        task = _make_task()
        voting_cfg = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=2)
        config = CrossModelVerifierConfig(voting_config=voting_cfg)

        diff_response = MagicMock(stdout="+code\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})
        reject_json = json.dumps({"verdict": "request_changes", "feedback": "Bug", "confidence": 0.85})

        call_count = 0

        async def alternating_llm(*args: object, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            return approve_json if call_count == 1 else reject_json

        with (
            patch("subprocess.run", return_value=diff_response),
            patch("bernstein.core.voting.call_llm", new=alternating_llm),
        ):
            verdict = await verify_with_cross_model(
                task,
                tmp_path,
                "claude-sonnet",
                config,
                voter_models=["google/gemini-flash-1.5", "anthropic/claude-haiku-4-5-20251001"],
            )

        assert verdict.verdict == "request_changes"

    @pytest.mark.asyncio
    async def test_multi_voter_reviewer_model_is_all_voters(self, tmp_path: Path) -> None:
        from bernstein.core.voting import VotingConfig, VotingStrategy

        task = _make_task()
        voting_cfg = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=2)
        config = CrossModelVerifierConfig(voting_config=voting_cfg)

        diff_response = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            verdict = await verify_with_cross_model(
                task,
                tmp_path,
                "claude-sonnet",
                config,
                voter_models=["m1", "m2"],
            )

        assert "m1" in verdict.reviewer_model
        assert "m2" in verdict.reviewer_model

    @pytest.mark.asyncio
    async def test_no_voter_models_falls_back_to_single_reviewer(self, tmp_path: Path) -> None:
        """voting_config set but no voter_models → single-reviewer fallback."""
        from bernstein.core.voting import VotingConfig, VotingStrategy

        task = _make_task()
        voting_cfg = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=2)
        config = CrossModelVerifierConfig(voting_config=voting_cfg)

        diff_response = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "Fine", "issues": []})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch(
                "bernstein.core.cross_model_verifier.call_llm",
                new=AsyncMock(return_value=approve_json),
            ),
        ):
            verdict = await verify_with_cross_model(task, tmp_path, "claude-sonnet", config)

        assert verdict.verdict == "approve"
