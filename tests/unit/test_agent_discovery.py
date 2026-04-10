"""Tests for bernstein.core.agent_discovery — agent auto-discovery."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from bernstein.core.agent_discovery import (
    AgentCapabilities,
    DiscoveryResult,
    _detect_aider,
    _detect_claude,
    _detect_codex,
    _detect_gemini,
    _detect_kiro,
    _detect_opencode,
    _detect_qwen,
    _extract_version,
    clear_discovery_cache,
    detect_auth_status,
    discover_agents,
    discover_agents_cached,
    generate_auto_routing_yaml,
    recommend_routing,
    short_model,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _extract_version
# ---------------------------------------------------------------------------


class TestExtractVersion:
    def test_returns_unknown_for_none(self) -> None:
        assert _extract_version(None) == "unknown"

    def test_returns_unknown_for_failed(self) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        assert _extract_version(result) == "unknown"

    def test_extracts_semver(self) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="codex v1.2.3\n", stderr="")
        assert _extract_version(result) == "1.2.3"

    def test_extracts_bare_version(self) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.15.2\n", stderr="")
        assert _extract_version(result) == "0.15.2"

    def test_extracts_from_long_output(self) -> None:
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="claude-code version 2.1.0 (build abc123)\n",
            stderr="",
        )
        assert _extract_version(result) == "2.1.0"


# ---------------------------------------------------------------------------
# _detect_codex
# ---------------------------------------------------------------------------


class TestDetectCodex:
    @patch("bernstein.core.agent_discovery.shutil.which", return_value=None)
    def test_not_found(self, _which: Any) -> None:
        agent, warnings = _detect_codex()
        assert agent is None
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/codex")
    def test_logged_in_via_chatgpt(self, _which: Any, mock_probe: MagicMock) -> None:
        # --version probe
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="codex v1.0.0\n", stderr="")
        # login status probe
        login_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="Logged in using ChatGPT\n", stderr="")
        mock_probe.side_effect = [version_result, login_result]

        with patch.dict("os.environ", {}, clear=True):
            agent, warnings = _detect_codex()

        assert agent is not None
        assert agent.name == "codex"
        assert agent.logged_in is True
        assert agent.login_method == "ChatGPT"
        assert agent.binary == "/usr/local/bin/codex"
        assert "o4-mini" in agent.available_models
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/codex")
    def test_logged_in_via_api_key(self, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="1.0.0\n", stderr="")
        login_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="Not logged in\n", stderr="")
        mock_probe.side_effect = [version_result, login_result]

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test123"}, clear=True):
            agent, warnings = _detect_codex()

        assert agent is not None
        assert agent.logged_in is True
        assert agent.login_method == "API key"
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/codex")
    def test_not_logged_in(self, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="1.0.0\n", stderr="")
        login_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="Not logged in\n", stderr="")
        mock_probe.side_effect = [version_result, login_result]

        with patch.dict("os.environ", {}, clear=True):
            agent, warnings = _detect_codex()

        assert agent is not None
        assert agent.logged_in is False
        assert len(warnings) == 1
        assert "not logged in" in warnings[0]


# ---------------------------------------------------------------------------
# _detect_gemini
# ---------------------------------------------------------------------------


class TestDetectGemini:
    @patch("bernstein.core.agent_discovery.shutil.which", return_value=None)
    def test_not_found(self, _which: Any) -> None:
        agent, warnings = _detect_gemini()
        assert agent is None
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/gemini")
    def test_logged_in_via_api_key(self, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.1.0\n", stderr="")
        mock_probe.return_value = version_result

        with patch.dict("os.environ", {"GOOGLE_API_KEY": "AIza-test"}, clear=True):
            agent, _warnings = _detect_gemini()

        assert agent is not None
        assert agent.name == "gemini"
        assert agent.logged_in is True
        assert agent.login_method == "GOOGLE_API_KEY"
        assert agent.max_context_tokens == 1_000_000

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/gemini")
    @patch("bernstein.core.preflight.gemini_has_auth", return_value=(True, "gcloud auth"))
    def test_logged_in_via_gcloud(self, _auth: Any, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.1.0\n", stderr="")
        mock_probe.return_value = version_result

        agent, _warnings = _detect_gemini()

        assert agent is not None
        assert agent.logged_in is True

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/gemini")
    def test_logged_in_via_config_dir(self, _which: Any, mock_probe: MagicMock, tmp_path: Path) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.1.0\n", stderr="")
        mock_probe.return_value = version_result
        config_dir = tmp_path / ".config" / "gemini"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text("{}")

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("bernstein.core.agent_discovery.Path.home", return_value=tmp_path),
        ):
            agent, _warnings = _detect_gemini()

        assert agent is not None
        assert agent.logged_in is True
        assert agent.login_method == "config"

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/gemini")
    def test_not_logged_in(self, _which: Any, mock_probe: MagicMock, tmp_path: Path) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.1.0\n", stderr="")
        mock_probe.return_value = version_result

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("bernstein.core.agent_discovery.Path.home", return_value=tmp_path),
        ):
            agent, warnings = _detect_gemini()

        assert agent is not None
        assert agent.logged_in is False
        assert any("not logged in" in w for w in warnings)


class TestDetectKiro:
    @patch("bernstein.core.agent_discovery.shutil.which", return_value=None)
    def test_not_found(self, _which: Any) -> None:
        agent, warnings = _detect_kiro()
        assert agent is None
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/kiro-cli")
    def test_detects_logged_in_and_models(self, _which: Any, mock_probe: MagicMock) -> None:
        version = subprocess.CompletedProcess(args=[], returncode=0, stdout="kiro-cli 1.2.3\n", stderr="")
        whoami = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"authMethod":"oauth"}', stderr="")
        models = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='["anthropic/claude-sonnet-4-6","openai/gpt-5.4"]',
            stderr="",
        )
        mock_probe.side_effect = [version, whoami, models]

        agent, warnings = _detect_kiro()

        assert agent is not None
        assert agent.logged_in is True
        assert agent.login_method == "oauth"
        assert agent.available_models[0] == "anthropic/claude-sonnet-4-6"
        assert warnings == []


class TestDetectOpenCode:
    @patch("bernstein.core.agent_discovery.shutil.which", return_value=None)
    def test_not_found(self, _which: Any) -> None:
        agent, warnings = _detect_opencode()
        assert agent is None
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/opencode")
    def test_detects_logged_in_and_models(self, _which: Any, mock_probe: MagicMock) -> None:
        version = subprocess.CompletedProcess(args=[], returncode=0, stdout="opencode 0.8.0\n", stderr="")
        auth = subprocess.CompletedProcess(args=[], returncode=0, stdout="anthropic\nopenai\n", stderr="")
        models = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="openai/gpt-5.4-mini\nanthropic/claude-sonnet-4-6\n",
            stderr="",
        )
        mock_probe.side_effect = [version, auth, models]

        agent, warnings = _detect_opencode()

        assert agent is not None
        assert agent.logged_in is True
        assert agent.available_models[0] == "openai/gpt-5.4-mini"
        assert warnings == []


# ---------------------------------------------------------------------------
# _detect_claude
# ---------------------------------------------------------------------------


class TestDetectClaude:
    @patch("bernstein.core.agent_discovery.shutil.which", return_value=None)
    def test_not_found(self, _which: Any) -> None:
        agent, warnings = _detect_claude()
        assert agent is None
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/claude")
    def test_logged_in_via_api_key(self, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="2.1.0\n", stderr="")
        mock_probe.return_value = version_result

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
            agent, _warnings = _detect_claude()

        assert agent is not None
        assert agent.name == "claude"
        assert agent.logged_in is True
        assert agent.login_method == "API key"
        assert "claude-opus-4-6" in agent.available_models

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/claude")
    def test_logged_in_via_oauth(self, _which: Any, mock_probe: MagicMock, tmp_path: Path) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="2.1.0\n", stderr="")
        mock_probe.return_value = version_result
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("bernstein.core.agent_discovery.Path.home", return_value=tmp_path),
        ):
            agent, _warnings = _detect_claude()

        assert agent is not None
        assert agent.logged_in is True
        assert agent.login_method == "OAuth"


# ---------------------------------------------------------------------------
# _detect_qwen
# ---------------------------------------------------------------------------


class TestDetectQwen:
    @patch("bernstein.core.agent_discovery.shutil.which", return_value=None)
    def test_not_found(self, _which: Any) -> None:
        agent, warnings = _detect_qwen()
        assert agent is None
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", side_effect=["/usr/local/bin/qwen-code", None])
    def test_found_as_qwen_code(self, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.5.0\n", stderr="")
        mock_probe.return_value = version_result

        with patch.dict("os.environ", {"OPENROUTER_API_KEY_PAID": "or-test"}, clear=True):
            agent, _warnings = _detect_qwen()

        assert agent is not None
        assert agent.name == "qwen"
        assert agent.logged_in is True
        assert agent.login_method == "OpenRouter"


# ---------------------------------------------------------------------------
# _detect_aider
# ---------------------------------------------------------------------------


class TestDetectAider:
    @patch("bernstein.core.agent_discovery.shutil.which", return_value=None)
    def test_not_found(self, _which: Any) -> None:
        agent, warnings = _detect_aider()
        assert agent is None
        assert warnings == []

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/aider")
    def test_logged_in_via_api_key(self, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.15.0\n", stderr="")
        mock_probe.return_value = version_result

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            agent, _warnings = _detect_aider()

        assert agent is not None
        assert agent.name == "aider"
        assert agent.logged_in is True
        assert agent.login_method == "API key"

    @patch("bernstein.core.agent_discovery._run_probe")
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/aider")
    def test_logged_in_via_local(self, _which: Any, mock_probe: MagicMock) -> None:
        version_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="0.15.0\n", stderr="")
        mock_probe.return_value = version_result

        with patch.dict("os.environ", {}, clear=True):
            agent, _warnings = _detect_aider()

        assert agent is not None
        assert agent.logged_in is True
        assert agent.login_method == "local"

    @patch("bernstein.core.agent_discovery._run_probe", return_value=None)
    @patch("bernstein.core.agent_discovery.shutil.which", return_value="/usr/local/bin/aider")
    def test_not_logged_in(self, _which: Any, mock_probe: MagicMock) -> None:
        with patch.dict("os.environ", {}, clear=True):
            agent, warnings = _detect_aider()

        assert agent is not None
        assert agent.logged_in is False
        assert len(warnings) == 1
        assert "not authenticated" in warnings[0]


# ---------------------------------------------------------------------------
# discover_agents (integration-level with full mocking)
# ---------------------------------------------------------------------------


class TestDiscoverAgents:
    @patch("bernstein.core.agent_discovery._detect_aider", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_qwen", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_opencode", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_kiro", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_kilo", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_gemini", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_cursor", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_codex", return_value=(None, []))
    @patch("bernstein.core.agent_discovery._detect_claude", return_value=(None, []))
    def test_no_agents(self, *_: Any) -> None:
        result = discover_agents()
        assert result.agents == []
        assert result.warnings == []
        assert result.scan_time_ms >= 0

    def test_aggregates_agents_and_warnings(self) -> None:
        claude = AgentCapabilities(
            name="claude",
            binary="/usr/local/bin/claude",
            version="2.0.0",
            logged_in=True,
            login_method="API key",
            available_models=["claude-sonnet-4-6"],
            default_model="claude-sonnet-4-6",
            supports_headless=True,
            supports_sandbox=False,
            supports_mcp=True,
            max_context_tokens=200_000,
            reasoning_strength="very_high",
            best_for=["architecture"],
            cost_tier="moderate",
        )
        with (
            patch("bernstein.core.agent_discovery._detect_claude", return_value=(claude, [])),
            patch(
                "bernstein.core.agent_discovery._detect_codex",
                return_value=(None, ["codex found but not logged in"]),
            ),
            patch("bernstein.core.agent_discovery._detect_cursor", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_gemini", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_kilo", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_kiro", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_opencode", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_qwen", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_aider", return_value=(None, [])),
        ):
            result = discover_agents()

        assert len(result.agents) == 1
        assert result.agents[0].name == "claude"
        assert len(result.warnings) == 1

    def test_detector_exception_is_swallowed(self) -> None:
        """A detector that raises should not crash the whole scan."""

        def _exploding_detect() -> tuple[None, list[str]]:
            raise RuntimeError("boom")

        with (
            patch("bernstein.core.agent_discovery._detect_claude", side_effect=RuntimeError("boom")),
            patch("bernstein.core.agent_discovery._detect_codex", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_cursor", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_gemini", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_kilo", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_kiro", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_opencode", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_qwen", return_value=(None, [])),
            patch("bernstein.core.agent_discovery._detect_aider", return_value=(None, [])),
        ):
            result = discover_agents()

        assert result.agents == []  # No crash


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestDiscoveryCache:
    def setup_method(self) -> None:
        clear_discovery_cache()

    def teardown_method(self) -> None:
        clear_discovery_cache()

    @patch("bernstein.core.agent_discovery.discover_agents")
    def test_cache_returns_same_result(self, mock_discover: MagicMock) -> None:
        mock_discover.return_value = DiscoveryResult(agents=[], warnings=[])
        r1 = discover_agents_cached()
        r2 = discover_agents_cached()
        assert r1 is r2
        assert mock_discover.call_count == 1

    @patch("bernstein.core.agent_discovery.discover_agents")
    def test_clear_cache_forces_rescan(self, mock_discover: MagicMock) -> None:
        mock_discover.return_value = DiscoveryResult(agents=[], warnings=[])
        discover_agents_cached()
        clear_discovery_cache()
        discover_agents_cached()
        assert mock_discover.call_count == 2


# ---------------------------------------------------------------------------
# recommend_routing
# ---------------------------------------------------------------------------


def _make_agent(name: str, logged_in: bool = True, **kwargs: Any) -> AgentCapabilities:
    defaults: dict[str, Any] = {
        "binary": f"/usr/bin/{name}",
        "version": "1.0.0",
        "logged_in": logged_in,
        "login_method": "API key" if logged_in else "",
        "available_models": ["model-a"],
        "default_model": "model-a",
        "supports_headless": True,
        "supports_sandbox": False,
        "supports_mcp": False,
        "max_context_tokens": 200_000,
        "reasoning_strength": "high",
        "best_for": [],
        "cost_tier": "cheap",
    }
    defaults.update(kwargs)
    return AgentCapabilities(name=name, **defaults)


class TestRecommendRouting:
    def test_empty_discovery(self) -> None:
        discovery = DiscoveryResult(agents=[], warnings=[])
        recs = recommend_routing(discovery)
        assert recs == []

    def test_single_claude_covers_roles(self) -> None:
        claude = _make_agent("claude", reasoning_strength="very_high", cost_tier="moderate")
        discovery = DiscoveryResult(agents=[claude], warnings=[])
        recs = recommend_routing(discovery)
        # Claude should be recommended for architect/security/manager
        roles = {r.role for r in recs}
        assert "architect" in roles
        assert "security" in roles
        assert "manager" in roles

    def test_multi_agent_routing(self) -> None:
        claude = _make_agent("claude", reasoning_strength="very_high", cost_tier="moderate")
        codex = _make_agent("codex", reasoning_strength="high", cost_tier="cheap")
        gemini = _make_agent(
            "gemini",
            reasoning_strength="very_high",
            cost_tier="free",
            best_for=["frontend", "long-context"],
        )
        discovery = DiscoveryResult(agents=[claude, codex, gemini], warnings=[])
        recs = recommend_routing(discovery)
        rec_map = {r.role: r for r in recs}

        assert rec_map["architect"].agent_name == "claude"
        assert rec_map["backend"].agent_name == "claude"  # Sonnet 79.6% SWE-bench > o4-mini 72%
        assert rec_map["frontend"].agent_name == "gemini"
        assert rec_map["qa"].agent_name == "codex"
        assert rec_map["security"].agent_name == "claude"

    def test_skips_not_logged_in(self) -> None:
        claude = _make_agent("claude", logged_in=False)
        codex = _make_agent("codex", logged_in=True)
        discovery = DiscoveryResult(agents=[claude, codex], warnings=[])
        recs = recommend_routing(discovery)
        # Claude is not logged in, so all recommendations should use codex
        for rec in recs:
            assert rec.agent_name == "codex"


# ---------------------------------------------------------------------------
# generate_auto_routing_yaml
# ---------------------------------------------------------------------------


class TestGenerateAutoRoutingYaml:
    def test_empty_discovery(self) -> None:
        discovery = DiscoveryResult(agents=[], warnings=[])
        yaml_str = generate_auto_routing_yaml(discovery)
        assert yaml_str == ""

    def test_generates_routing_block(self) -> None:
        claude = _make_agent("claude", reasoning_strength="very_high", cost_tier="moderate")
        codex = _make_agent("codex", reasoning_strength="high", cost_tier="cheap")
        discovery = DiscoveryResult(agents=[claude, codex], warnings=[])
        yaml_str = generate_auto_routing_yaml(discovery)
        assert "cli: auto" in yaml_str
        assert "routing:" in yaml_str
        assert "architect:" in yaml_str


# ---------------------------------------------------------------------------
# detect_auth_status
# ---------------------------------------------------------------------------


class TestDetectAuthStatus:
    def test_all_agents_not_found(self) -> None:
        discovery = DiscoveryResult(agents=[], warnings=[])

        with patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery):
            result = detect_auth_status()

        # Should include all known agents as (False, False)
        assert result["claude"] == (False, False)
        assert result["codex"] == (False, False)
        assert result["gemini"] == (False, False)
        assert result["kiro"] == (False, False)
        assert result["opencode"] == (False, False)
        assert result["qwen"] == (False, False)
        assert result["aider"] == (False, False)

    def test_mixed_installed_authenticated_status(self) -> None:
        claude = _make_agent("claude", logged_in=True)
        codex = _make_agent("codex", logged_in=False)
        aider = _make_agent("aider", logged_in=True)
        discovery = DiscoveryResult(agents=[claude, codex, aider], warnings=[])

        with patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery):
            result = detect_auth_status()

        assert result["claude"] == (True, True)  # installed, authenticated
        assert result["codex"] == (True, False)  # installed, not authenticated
        assert result["aider"] == (True, True)  # installed, authenticated
        assert result["gemini"] == (False, False)  # not installed
        assert result["kiro"] == (False, False)  # not installed
        assert result["opencode"] == (False, False)  # not installed
        assert result["qwen"] == (False, False)  # not installed

    def test_all_agents_installed_authenticated(self) -> None:
        agents = [
            _make_agent("claude", logged_in=True),
            _make_agent("codex", logged_in=True),
            _make_agent("gemini", logged_in=True),
            _make_agent("kiro", logged_in=True),
            _make_agent("opencode", logged_in=True),
            _make_agent("qwen", logged_in=True),
            _make_agent("aider", logged_in=True),
        ]
        discovery = DiscoveryResult(agents=agents, warnings=[])

        with patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery):
            result = detect_auth_status()

        for agent_name in ["claude", "codex", "gemini", "kiro", "opencode", "qwen", "aider"]:
            assert result[agent_name] == (True, True), f"{agent_name} should be installed and authenticated"


# ---------------------------------------------------------------------------
# short_model
# ---------------------------------------------------------------------------


class TestShortModel:
    def test_known_models(self) -> None:
        assert short_model("claude-opus-4-6") == "opus"
        assert short_model("gemini-3-flash") == "2.5-flash"
        assert short_model("o4-mini") == "o4-mini"

    def test_unknown_model_passthrough(self) -> None:
        assert short_model("unknown-model") == "unknown-model"


# ---------------------------------------------------------------------------
# Router integration: auto_route_task
# ---------------------------------------------------------------------------


def _make_task(title: str, role: str) -> Any:
    """Create a minimal Task for testing."""
    from bernstein.core.models import Task

    return Task(id="test-1", title=title, description=title, role=role)


class TestAutoRouteTask:
    def test_routes_architect_to_strongest(self) -> None:
        from bernstein.core.router import auto_route_task

        claude = _make_agent("claude", reasoning_strength="very_high", cost_tier="moderate")
        codex = _make_agent("codex", reasoning_strength="high", cost_tier="cheap")
        discovery = DiscoveryResult(agents=[claude, codex], warnings=[])

        task = _make_task("Design system", "architect")

        with patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery):
            decision = auto_route_task(task)

        assert decision.agent_name == "claude"
        assert "opus" in decision.model

    def test_routes_qa_to_cheapest(self) -> None:
        from bernstein.core.router import auto_route_task

        claude = _make_agent("claude", reasoning_strength="very_high", cost_tier="moderate")
        codex = _make_agent("codex", reasoning_strength="high", cost_tier="cheap")
        discovery = DiscoveryResult(agents=[claude, codex], warnings=[])

        task = _make_task("Write tests", "qa")

        with patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery):
            decision = auto_route_task(task)

        assert decision.agent_name == "codex"

    def test_fallback_when_no_agents(self) -> None:
        from bernstein.core.router import auto_route_task

        discovery = DiscoveryResult(agents=[], warnings=[])
        task = _make_task("Do something", "backend")

        with patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery):
            decision = auto_route_task(task)

        # Should fall back to claude/sonnet
        assert decision.agent_name == "claude"
        assert decision.model == "sonnet"
