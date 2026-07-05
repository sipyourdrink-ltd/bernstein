"""Unit tests for the task-level council runner (``bernstein.adapters.council_runner``).

The SDK (``agents``/``openai``) is fully mocked via ``sys.modules`` - the
same approach as ``tests/unit/adapters/test_openai_agents.py`` - so no
network calls and no real subprocesses happen here.
"""

from __future__ import annotations

import types
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from bernstein.adapters.council_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    CouncilRunResult,
    run_council,
)
from bernstein.adapters.openai_agents_runner import (
    RunnerManifest,
    _is_alibaba_cloud_endpoint,
    _load_council_config,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fake SDK plumbing
# ---------------------------------------------------------------------------


class _FakeUsage:
    """Mimics ``agents.usage.Usage`` embedded on a raw response entry."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeRawResponse:
    """Mimics one ``ModelResponse`` entry of ``RunResult.raw_responses``."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeRunResult:
    """Mimics the two attributes of ``RunResult`` the council reads."""

    def __init__(self, final_output: str, raw_responses: list[Any] | None = None) -> None:
        self.final_output = final_output
        self.raw_responses = raw_responses if raw_responses is not None else []


class _FakeModelSettings:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeChatModel:
    """Mimics ``agents.OpenAIChatCompletionsModel``."""

    def __init__(self, model: str, openai_client: Any) -> None:
        self.model = model
        self.openai_client = openai_client


class _FakeAsyncOpenAI:
    """Mimics ``openai.AsyncOpenAI`` - just records its kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeAgent:
    """Mimics the subset of ``agents.Agent`` the council touches."""

    def __init__(self, **attrs: Any) -> None:
        self.model: Any = attrs.get("model")
        self.model_settings: Any = attrs.get("model_settings")
        self.tools: list[Any] = attrs.get("tools", ["tool-a"])
        self.handoffs: list[Any] = attrs.get("handoffs", [])

    def clone(self, **overrides: Any) -> _FakeAgent:
        merged = {
            "model": self.model,
            "model_settings": self.model_settings,
            "tools": self.tools,
            "handoffs": self.handoffs,
        }
        merged.update(overrides)
        return _FakeAgent(**merged)


def _fake_sdk_modules(
    behaviors: dict[str, Any],
    calls: list[tuple[Any, str, dict[str, Any]]],
) -> dict[str, Any]:
    """Build fake ``agents`` and ``openai`` modules for ``sys.modules``.

    Args:
        behaviors: model id -> either a ``_FakeRunResult`` to return or an
            ``Exception`` instance for ``Runner.run`` to raise.
        calls: mutable list every ``Runner.run`` invocation is appended to
            as ``(agent, prompt, kwargs)``.
    """

    class _FakeRunner:
        @staticmethod
        async def run(agent: _FakeAgent, prompt: str, **kwargs: Any) -> _FakeRunResult:
            calls.append((agent, prompt, kwargs))
            behavior = behaviors[agent.model.model]
            if isinstance(behavior, Exception):
                raise behavior
            return behavior

    agents_mod = types.ModuleType("agents")
    agents_mod.Runner = _FakeRunner  # type: ignore[attr-defined]
    agents_mod.OpenAIChatCompletionsModel = _FakeChatModel  # type: ignore[attr-defined]
    agents_mod.ModelSettings = _FakeModelSettings  # type: ignore[attr-defined]
    openai_mod = types.ModuleType("openai")
    openai_mod.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[attr-defined]
    return {"agents": agents_mod, "openai": openai_mod}


def _manifest(**overrides: Any) -> RunnerManifest:
    kwargs: dict[str, Any] = {
        "session_id": "council-test",
        "prompt": "solve the task",
        "workdir": "/workspace",
        "model": "councils/team.yaml",
    }
    kwargs.update(overrides)
    return RunnerManifest(**kwargs)


def _council_cfg(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "candidates": [{"model": "cand-a"}, {"model": "cand-b"}],
        "judge": {"model": "judge-m"},
        "timeout": 5.0,
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# run_council - happy path
# ---------------------------------------------------------------------------


class TestRunCouncilHappyPath:
    """N candidates dispatch, judge synthesizes, winner returned."""

    def test_final_output_is_judge_synthesis(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": _FakeRunResult("answer from b"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            result = run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        assert isinstance(result, CouncilRunResult)
        assert result.final_output == "synthesized winner"

    def test_every_candidate_and_judge_dispatched(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": _FakeRunResult("answer from b"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        dispatched = [agent.model.model for agent, _, _ in calls]
        assert sorted(dispatched[:2]) == ["cand-a", "cand-b"]
        assert dispatched[2] == "judge-m"

    def test_candidates_receive_task_prompt_verbatim(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": _FakeRunResult("answer from b"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        candidate_prompts = [prompt for agent, prompt, _ in calls if agent.model.model != "judge-m"]
        assert candidate_prompts == ["solve the task", "solve the task"]

    def test_judge_prompt_embeds_task_and_candidate_outputs(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": _FakeRunResult("answer from b"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        judge_prompt = next(prompt for agent, prompt, _ in calls if agent.model.model == "judge-m")
        assert "solve the task" in judge_prompt
        assert "answer from a" in judge_prompt
        assert "answer from b" in judge_prompt
        assert "candidates[0]=cand-a" in judge_prompt
        assert "candidates[1]=cand-b" in judge_prompt

    def test_judge_agent_has_tools_and_handoffs_stripped(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": _FakeRunResult("answer from b"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        base_agent = _FakeAgent(tools=["tool-a", "tool-b"], handoffs=["worker"])
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            run_council(base_agent, "solve the task", _council_cfg(), _manifest())
        judge_agent = next(agent for agent, _, _ in calls if agent.model.model == "judge-m")
        assert judge_agent.tools == []
        assert judge_agent.handoffs == []

    def test_candidate_agents_keep_base_tools(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": _FakeRunResult("answer from b"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        base_agent = _FakeAgent(tools=["tool-a", "tool-b"])
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            run_council(base_agent, "solve the task", _council_cfg(), _manifest())
        candidate_agents = [agent for agent, _, _ in calls if agent.model.model != "judge-m"]
        assert all(agent.tools == ["tool-a", "tool-b"] for agent in candidate_agents)

    def test_member_base_url_and_api_key_env_reach_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-value")
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        cfg = _council_cfg(
            candidates=[
                {
                    "model": "cand-a",
                    "base_url": "https://openrouter.example/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                },
            ],
        )
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            run_council(_FakeAgent(), "solve the task", cfg, _manifest())
        candidate_agent = next(agent for agent, _, _ in calls if agent.model.model == "cand-a")
        client = candidate_agent.model.openai_client
        assert client.kwargs["base_url"] == "https://openrouter.example/v1"
        assert client.kwargs["api_key"] == "or-test-value"


# ---------------------------------------------------------------------------
# run_council - one bad candidate never kills the council
# ---------------------------------------------------------------------------


class TestRunCouncilCandidateContainment:
    """A failing candidate is excluded; the council completes with the rest."""

    def test_execution_failure_excludes_only_that_candidate(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": ValueError("provider exploded"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            result = run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        assert result.final_output == "synthesized winner"
        judge_prompt = next(prompt for agent, prompt, _ in calls if agent.model.model == "judge-m")
        assert "answer from a" in judge_prompt
        assert "cand-b" not in judge_prompt

    def test_execution_failure_excluded_from_member_usage(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "cand-b": ValueError("provider exploded"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            result = run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        models = [entry["model"] for entry in result.member_usage]
        assert models == ["cand-a", "judge-m"]

    def test_setup_failure_missing_credential_env_excludes_candidate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A member whose ``api_key_env`` variable is unset fails during
        setup (before dispatch) and must be contained the same way."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        cfg = _council_cfg(
            candidates=[
                {"model": "cand-a"},
                {"model": "cand-broken", "api_key_env": "OPENROUTER_API_KEY"},
            ],
        )
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            result = run_council(_FakeAgent(), "solve the task", cfg, _manifest())
        assert result.final_output == "synthesized winner"
        # The broken member never reached Runner.run.
        dispatched = [agent.model.model for agent, _, _ in calls]
        assert "cand-broken" not in dispatched
        assert [entry["model"] for entry in result.member_usage] == ["cand-a", "judge-m"]

    def test_setup_failure_rejected_env_name_excludes_candidate(self) -> None:
        """An ``api_key_env`` outside the credential allowlist is a setup
        failure for that one member only."""
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        cfg = _council_cfg(
            candidates=[
                {"model": "cand-a"},
                {"model": "cand-broken", "api_key_env": "GITHUB_TOKEN"},
            ],
        )
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            result = run_council(_FakeAgent(), "solve the task", cfg, _manifest())
        assert result.final_output == "synthesized winner"
        assert [entry["model"] for entry in result.member_usage] == ["cand-a", "judge-m"]

    def test_all_candidates_failing_raises_runtime_error(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": ValueError("boom a"),
            "cand-b": ValueError("boom b"),
            "judge-m": _FakeRunResult("never reached"),
        }
        with (
            patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)),
            pytest.raises(RuntimeError, match="all 2 .*candidates failed"),
        ):
            run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        # The judge must never be dispatched with nothing to synthesize.
        dispatched = [agent.model.model for agent, _, _ in calls]
        assert "judge-m" not in dispatched


# ---------------------------------------------------------------------------
# run_council - malformed council config
# ---------------------------------------------------------------------------


class TestRunCouncilConfigValidation:
    def test_empty_candidates_raises(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        with (
            patch.dict("sys.modules", _fake_sdk_modules({}, calls)),
            pytest.raises(RuntimeError, match="candidates must be a non-empty list"),
        ):
            run_council(_FakeAgent(), "p", _council_cfg(candidates=[]), _manifest())

    def test_missing_judge_raises(self) -> None:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        with (
            patch.dict("sys.modules", _fake_sdk_modules({}, calls)),
            pytest.raises(RuntimeError, match="judge is required"),
        ):
            run_council(_FakeAgent(), "p", _council_cfg(judge=None), _manifest())


# ---------------------------------------------------------------------------
# run_council - per-member usage emission / cost attribution
# ---------------------------------------------------------------------------


class TestRunCouncilMemberUsage:
    """Each member's usage lands attributed to that member's real model id."""

    def _run(self) -> tuple[CouncilRunResult, dict[str, _FakeRunResult]]:
        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a", [_FakeRawResponse(10, 20)]),
            "cand-b": _FakeRunResult("answer from b", [_FakeRawResponse(30, 40)]),
            "judge-m": _FakeRunResult("synthesized winner", [_FakeRawResponse(50, 60)]),
        }
        with patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)):
            result = run_council(_FakeAgent(), "solve the task", _council_cfg(), _manifest())
        return result, behaviors

    def test_one_entry_per_live_candidate_plus_judge(self) -> None:
        result, _ = self._run()
        assert [entry["model"] for entry in result.member_usage] == ["cand-a", "cand-b", "judge-m"]

    def test_labels_identify_each_member(self) -> None:
        result, _ = self._run()
        labels = [entry["label"] for entry in result.member_usage]
        assert labels == ["candidates[0]=cand-a", "candidates[1]=cand-b", "judge=judge-m"]

    def test_each_entry_carries_that_members_own_result(self) -> None:
        result, behaviors = self._run()
        by_model = {entry["model"]: entry["result"] for entry in result.member_usage}
        assert by_model["cand-a"] is behaviors["cand-a"]
        assert by_model["cand-b"] is behaviors["cand-b"]
        assert by_model["judge-m"] is behaviors["judge-m"]

    def test_member_usage_tokens_stay_attributed_per_member(self) -> None:
        result, _ = self._run()
        tokens = {
            entry["model"]: (
                entry["result"].raw_responses[0].usage.input_tokens,
                entry["result"].raw_responses[0].usage.output_tokens,
            )
            for entry in result.member_usage
        }
        assert tokens == {"cand-a": (10, 20), "cand-b": (30, 40), "judge-m": (50, 60)}

    def test_aggregate_raw_responses_cover_whole_council(self) -> None:
        result, behaviors = self._run()
        expected = (
            behaviors["cand-a"].raw_responses + behaviors["cand-b"].raw_responses + behaviors["judge-m"].raw_responses
        )
        assert result.raw_responses == expected


# ---------------------------------------------------------------------------
# _load_council_config
# ---------------------------------------------------------------------------


class TestLoadCouncilConfig:
    def _write_council_file(self, workdir: Path, relpath: str, text: str) -> Path:
        path = workdir / ".bernstein" / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_yaml_parses(self, tmp_path: Path) -> None:
        self._write_council_file(
            tmp_path,
            "councils/team.yaml",
            "candidates:\n  - model: cand-a\n  - model: cand-b\njudge:\n  model: judge-m\ntimeout: 90.0\n",
        )
        manifest = _manifest(workdir=str(tmp_path), model="councils/team.yaml")
        cfg = _load_council_config(manifest)
        assert cfg is not None
        assert [m["model"] for m in cfg["candidates"]] == ["cand-a", "cand-b"]
        assert cfg["judge"] == {"model": "judge-m"}
        assert cfg["timeout"] == pytest.approx(90.0)

    def test_non_yaml_model_returns_none(self, tmp_path: Path) -> None:
        manifest = _manifest(workdir=str(tmp_path), model="gpt-5-mini")
        assert _load_council_config(manifest) is None

    def test_prepopulated_manifest_council_returned_unchanged(self, tmp_path: Path) -> None:
        cfg = _council_cfg()
        manifest = _manifest(workdir=str(tmp_path), model="gpt-5-mini", council=cfg)
        assert _load_council_config(manifest) is cfg

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        manifest = _manifest(workdir=str(tmp_path), model="councils/absent.yaml")
        with pytest.raises(RuntimeError, match="does not exist"):
            _load_council_config(manifest)

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        self._write_council_file(tmp_path, "councils/broken.yaml", "candidates: [unclosed\n")
        manifest = _manifest(workdir=str(tmp_path), model="councils/broken.yaml")
        with pytest.raises(RuntimeError, match="failed to read/parse"):
            _load_council_config(manifest)

    def test_non_mapping_root_raises(self, tmp_path: Path) -> None:
        self._write_council_file(tmp_path, "councils/list.yaml", "- a\n- b\n")
        manifest = _manifest(workdir=str(tmp_path), model="councils/list.yaml")
        with pytest.raises(RuntimeError, match="must be a YAML mapping"):
            _load_council_config(manifest)

    def test_missing_candidates_raises(self, tmp_path: Path) -> None:
        self._write_council_file(tmp_path, "councils/nocand.yaml", "judge:\n  model: judge-m\n")
        manifest = _manifest(workdir=str(tmp_path), model="councils/nocand.yaml")
        with pytest.raises(RuntimeError, match="'candidates' must be a non-empty list"):
            _load_council_config(manifest)

    def test_missing_judge_raises(self, tmp_path: Path) -> None:
        self._write_council_file(tmp_path, "councils/nojudge.yaml", "candidates:\n  - model: cand-a\n")
        manifest = _manifest(workdir=str(tmp_path), model="councils/nojudge.yaml")
        with pytest.raises(RuntimeError, match="'judge' is required"):
            _load_council_config(manifest)

    def test_absolute_path_used_as_is(self, tmp_path: Path) -> None:
        abs_file = tmp_path / "elsewhere" / "team.yml"
        abs_file.parent.mkdir(parents=True)
        abs_file.write_text(
            "candidates:\n  - model: cand-a\njudge:\n  model: judge-m\n",
            encoding="utf-8",
        )
        manifest = _manifest(workdir=str(tmp_path / "unrelated-workdir"), model=str(abs_file))
        cfg = _load_council_config(manifest)
        assert cfg is not None
        assert cfg["judge"] == {"model": "judge-m"}


# ---------------------------------------------------------------------------
# _is_alibaba_cloud_endpoint - hostname-suffix hardening
# ---------------------------------------------------------------------------


class TestIsAlibabaCloudEndpoint:
    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", True),
            ("https://x.maas.aliyuncs.com/v1", True),
            ("dashscope.aliyuncs.com", True),
            ("https://notaliyuncs.com/v1", False),
            ("https://example.com/aliyuncs.com/v1", False),
            ("https://aliyuncs.com.evil.example/v1", False),
            (None, False),
            ("", False),
        ],
    )
    def test_table(self, base_url: str | None, expected: bool) -> None:
        assert _is_alibaba_cloud_endpoint(base_url) is expected


# ---------------------------------------------------------------------------
# Timeout resolution on the council config
# ---------------------------------------------------------------------------


class TestCouncilTimeoutResolution:
    def _timeout_seen_by_wait_for(self, cfg: dict[str, Any]) -> float:
        """Run a one-candidate council and capture asyncio.wait_for's timeout."""
        seen: list[float] = []
        import asyncio as _asyncio

        real_wait_for = _asyncio.wait_for

        async def _spy_wait_for(awaitable: Any, timeout: float) -> Any:
            seen.append(timeout)
            return await real_wait_for(awaitable, timeout)

        calls: list[tuple[Any, str, dict[str, Any]]] = []
        behaviors = {
            "cand-a": _FakeRunResult("answer from a"),
            "judge-m": _FakeRunResult("synthesized winner"),
        }
        with (
            patch.dict("sys.modules", _fake_sdk_modules(behaviors, calls)),
            patch("bernstein.adapters.council_runner.asyncio.wait_for", _spy_wait_for),
        ):
            run_council(_FakeAgent(), "p", cfg, _manifest())
        assert seen
        return seen[0]

    def test_numeric_timeout_used(self) -> None:
        cfg = _council_cfg(candidates=[{"model": "cand-a"}], timeout=7.5)
        assert self._timeout_seen_by_wait_for(cfg) == pytest.approx(7.5)

    def test_missing_timeout_falls_back_to_default(self) -> None:
        cfg = _council_cfg(candidates=[{"model": "cand-a"}])
        del cfg["timeout"]
        assert self._timeout_seen_by_wait_for(cfg) == pytest.approx(DEFAULT_TIMEOUT_SECONDS)

    def test_bool_timeout_rejected_in_favor_of_default(self) -> None:
        """``timeout: true`` in YAML must not silently become a 1.0s budget."""
        cfg = _council_cfg(candidates=[{"model": "cand-a"}], timeout=True)
        assert self._timeout_seen_by_wait_for(cfg) == pytest.approx(DEFAULT_TIMEOUT_SECONDS)
