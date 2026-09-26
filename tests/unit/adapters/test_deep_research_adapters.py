"""Deep-research adapters: gpt-researcher and Tongyi DeepResearch.

Covers the gateway env contract (names only, values never echoed), the command
and environment each adapter hands its runner, the run artifact the runners
write and the adapters verify, and the runners' isolation from bernstein.
"""

from __future__ import annotations

import ast
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from bernstein.adapters import deep_research_artifact as artifact
from bernstein.adapters._contract import OutputMode, strategy_for
from bernstein.adapters.deep_research import (
    GatewayConfigError,
    ResearchTerminalState,
    gateway_env_names,
    load_research_run,
    read_gateway,
)
from bernstein.adapters.gpt_researcher import GPTResearcherAdapter
from bernstein.adapters.registry import get_adapter
from bernstein.adapters.tongyi_deepresearch import TongyiDeepResearchAdapter
from bernstein.adapters.tongyi_deepresearch_runner import visited_urls

ADAPTERS_DIR = Path(artifact.__file__).parent

GATEWAY = {
    "BERNSTEIN_GPT_RESEARCHER_OPENAI_BASE_URL": "http://gw.test/v1",
    "BERNSTEIN_GPT_RESEARCHER_OPENAI_API_KEY": "sk-gpt-researcher-secret",
    "BERNSTEIN_GPT_RESEARCHER_MODEL": "research-smart",
    "BERNSTEIN_TONGYI_DEEPRESEARCH_OPENAI_BASE_URL": "http://gw.test/v1",
    "BERNSTEIN_TONGYI_DEEPRESEARCH_OPENAI_API_KEY": "sk-tongyi-secret",
    "BERNSTEIN_TONGYI_DEEPRESEARCH_MODEL": "tongyi-planner",
    "BERNSTEIN_TONGYI_DEEPRESEARCH_HOME": "/opt/tongyi",
}


# --- gateway env -------------------------------------------------------------


def test_gateway_env_names_are_per_agent() -> None:
    assert gateway_env_names("gpt-researcher") == (
        "BERNSTEIN_GPT_RESEARCHER_OPENAI_BASE_URL",
        "BERNSTEIN_GPT_RESEARCHER_OPENAI_API_KEY",
        "BERNSTEIN_GPT_RESEARCHER_MODEL",
    )
    assert gateway_env_names("tongyi-deepresearch")[0] == "BERNSTEIN_TONGYI_DEEPRESEARCH_OPENAI_BASE_URL"


def test_read_gateway_returns_the_three_values() -> None:
    gw = read_gateway("gpt-researcher", GATEWAY)
    assert (gw.base_url, gw.api_key, gw.model) == ("http://gw.test/v1", "sk-gpt-researcher-secret", "research-smart")


def test_missing_gateway_vars_are_named_and_no_value_is_echoed() -> None:
    env = {"BERNSTEIN_GPT_RESEARCHER_OPENAI_API_KEY": "sk-gpt-researcher-secret"}
    with pytest.raises(GatewayConfigError) as info:
        read_gateway("gpt-researcher", env)
    message = str(info.value)
    assert "BERNSTEIN_GPT_RESEARCHER_OPENAI_BASE_URL" in message
    assert "BERNSTEIN_GPT_RESEARCHER_MODEL" in message
    assert "sk-gpt-researcher-secret" not in message


def test_one_agents_key_does_not_serve_another() -> None:
    with pytest.raises(GatewayConfigError):
        read_gateway("tongyi-deepresearch", {k: v for k, v in GATEWAY.items() if "GPT_RESEARCHER" in k})


# --- gpt-researcher adapter ----------------------------------------------------


def test_gpt_researcher_env_points_every_llm_at_the_gateway() -> None:
    env = GPTResearcherAdapter().build_env(GATEWAY)
    assert env["OPENAI_BASE_URL"] == "http://gw.test/v1"
    assert env["OPENAI_API_KEY"] == "sk-gpt-researcher-secret"
    for role in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM"):
        assert env[role] == "openai:research-smart"
    assert not any(k.startswith("BERNSTEIN_TONGYI") for k in env)


def test_gpt_researcher_command_runs_its_runner_by_path(tmp_path: Path) -> None:
    cmd = GPTResearcherAdapter().build_command(tmp_path / "prompt.txt", tmp_path / "out", GATEWAY)
    assert cmd[1] == str(ADAPTERS_DIR / "gpt_researcher_runner.py")
    assert cmd[cmd.index("--report-type") + 1] == "deep"
    assert "sk-gpt-researcher-secret" not in " ".join(cmd)


def test_gpt_researcher_interpreter_is_configurable(tmp_path: Path) -> None:
    env = {**GATEWAY, "BERNSTEIN_GPT_RESEARCHER_PYTHON": "/opt/gptr/bin/python"}
    assert GPTResearcherAdapter().build_command(tmp_path / "p", tmp_path / "o", env)[0] == "/opt/gptr/bin/python"


# --- Tongyi adapter --------------------------------------------------------------


def test_tongyi_env_points_planner_and_summariser_at_the_gateway() -> None:
    env = TongyiDeepResearchAdapter().build_env(GATEWAY)
    assert env["OPENAI_BASE_URL"] == env["API_BASE"] == "http://gw.test/v1"
    assert env["OPENAI_API_KEY"] == env["API_KEY"] == "sk-tongyi-secret"
    assert env["SUMMARY_MODEL_NAME"] == "tongyi-planner"


def test_tongyi_command_names_the_checkout(tmp_path: Path) -> None:
    cmd = TongyiDeepResearchAdapter().build_command(tmp_path / "p", tmp_path / "o", GATEWAY)
    assert cmd[1] == str(ADAPTERS_DIR / "tongyi_deepresearch_runner.py")
    assert cmd[cmd.index("--repo-dir") + 1] == "/opt/tongyi"
    assert cmd[cmd.index("--model") + 1] == "tongyi-planner"


def test_tongyi_tool_base_urls_reach_the_runner_only_when_set() -> None:
    assert "SERPER_BASE_URL" not in TongyiDeepResearchAdapter().build_env(GATEWAY)
    env = TongyiDeepResearchAdapter().build_env(
        {
            **GATEWAY,
            "BERNSTEIN_TONGYI_DEEPRESEARCH_SERPER_BASE_URL": " http://gw.test/v1 ",
            "BERNSTEIN_TONGYI_DEEPRESEARCH_JINA_BASE_URL": "http://gw.test/v1/web/fetch",
        }
    )
    assert env["SERPER_BASE_URL"] == "http://gw.test/v1"
    assert env["JINA_BASE_URL"] == "http://gw.test/v1/web/fetch"


def test_tongyi_without_a_checkout_is_a_config_error(tmp_path: Path) -> None:
    env = {k: v for k, v in GATEWAY.items() if k != "BERNSTEIN_TONGYI_DEEPRESEARCH_HOME"}
    with pytest.raises(GatewayConfigError, match="BERNSTEIN_TONGYI_DEEPRESEARCH_HOME"):
        TongyiDeepResearchAdapter().build_command(tmp_path / "p", tmp_path / "o", env)


def test_tongyi_counts_every_url_its_visit_tool_opened() -> None:
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": '<tool_call>{"name": "visit", "arguments": {"url": ["https://a.test", "https://b.test"], '
            '"goal": "g"}}</tool_call>',
        },
        {"role": "assistant", "content": '<tool_call>{"name": "search", "arguments": {"query": ["x"]}}</tool_call>'},
        {
            "role": "assistant",
            "content": '<tool_call>{"name": "visit", "arguments": {"url": "https://a.test"}}</tool_call>',
        },
        {"role": "assistant", "content": "<tool_call>not json</tool_call>"},
    ]
    assert visited_urls(messages) == ["https://a.test", "https://b.test"]


# --- run artifact ------------------------------------------------------------------


def _write(tmp_path: Path, **kw: object) -> Path:
    args: dict[str, object] = {
        "agent": "gpt-researcher",
        "report": "# Findings\n",
        "sources": ["https://b.test", "https://a.test", "https://a.test", " "],
        "started_at": 100.0,
        "finished_at": 160.5,
        "state": artifact.STATE_OK,
    }
    args.update(kw)
    artifact.write_run(tmp_path, **args)  # type: ignore[arg-type]
    return tmp_path


def test_a_written_run_verifies_and_counts_distinct_sources(tmp_path: Path) -> None:
    run = load_research_run(_write(tmp_path))
    assert run.state is ResearchTerminalState.OK
    assert run.sources_count == 2
    assert run.wall_seconds == pytest.approx(60.5)
    assert json.loads((tmp_path / artifact.SOURCES_FILE).read_text()) == ["https://a.test", "https://b.test"]


def test_an_edited_report_is_reported_as_tampered(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / artifact.REPORT_FILE).write_text("# Findings, improved\n")
    run = load_research_run(tmp_path)
    assert run.state is ResearchTerminalState.TAMPERED
    assert any("report" in e for e in run.errors)


def test_an_added_source_is_reported_as_tampered(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / artifact.SOURCES_FILE).write_text(json.dumps(["https://a.test", "https://b.test", "https://c.test"]))
    assert load_research_run(tmp_path).state is ResearchTerminalState.TAMPERED


def test_an_empty_report_is_inconclusive_not_ok(tmp_path: Path) -> None:
    assert load_research_run(_write(tmp_path, report="  \n")).state is ResearchTerminalState.INCONCLUSIVE


def test_a_missing_run_record_is_a_driver_failure(tmp_path: Path) -> None:
    run = load_research_run(tmp_path)
    assert run.state is ResearchTerminalState.DRIVER_FAILURE


# --- wiring -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "cls"), [("gpt_researcher", GPTResearcherAdapter), ("tongyi_deepresearch", TongyiDeepResearchAdapter)]
)
def test_adapters_are_registered_with_artifact_output(name: str, cls: type) -> None:
    assert isinstance(get_adapter(name), cls)
    assert strategy_for(name).output_mode is OutputMode.ARTIFACT


@pytest.mark.parametrize(
    "module", ["deep_research_artifact.py", "gpt_researcher_runner.py", "tongyi_deepresearch_runner.py"]
)
def test_runner_side_modules_import_nothing_from_bernstein(module: str) -> None:
    """They run inside the agent's own interpreter, where bernstein is not installed."""
    tree = ast.parse((ADAPTERS_DIR / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("bernstein"), module
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("bernstein") for a in node.names), module


# --- runners, end to end against stub agents ------------------------------------------

_STUB_GPT_RESEARCHER = """
import os
class GPTResearcher:
    def __init__(self, query, report_type):
        assert report_type == "deep" and os.environ["SMART_LLM"] == "openai:m1"
        self.query, self.visited_urls = query, {"https://x.test/2"}
    async def conduct_research(self):
        pass
    async def write_report(self):
        return "# Report\\n" + self.query
    def get_source_urls(self):
        return ["https://x.test/1", "https://x.test/2"]
"""

_STUB_REACT_AGENT = """
class OpenAI:
    def __init__(self, api_key=None, base_url=None, timeout=None):
        self.api_key, self.base_url = api_key, base_url
class MultiTurnReactAgent:
    def __init__(self, llm, function_list):
        self.llm = llm
    def count_tokens(self, messages):
        raise RuntimeError("no local tokenizer")
    def _run(self, data, model):
        client = OpenAI(api_key="EMPTY", base_url="http://127.0.0.1:6001/v1")
        assert (client.base_url, client.api_key) == ("http://gw.test/v1", "k2")
        assert self.count_tokens([{"content": "abcdefgh"}]) == 2
        call = '{"name": "visit", "arguments": {"url": ["https://t.test/1", "https://t.test/2"]}}'
        messages = [{"role": "assistant", "content": "<tool_call>" + call + "</tool_call>"}]
        return {"prediction": "the answer", "termination": "answer", "messages": messages}
"""


def _run_runner(script: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(ADAPTERS_DIR / script), *args],
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_gpt_researcher_runner_imports_the_agent_not_the_adapter_beside_it(tmp_path: Path) -> None:
    """The runner's own directory holds ``gpt_researcher.py`` (the adapter); the
    agent package of the same name must win, or the runner imports bernstein."""
    pkg = tmp_path / "site" / "gpt_researcher"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_STUB_GPT_RESEARCHER)
    (tmp_path / "p.txt").write_text("What is X?")
    proc = subprocess.run(
        [
            sys.executable,
            str(ADAPTERS_DIR / "gpt_researcher_runner.py"),
            "--prompt-file",
            str(tmp_path / "p.txt"),
            "--out-dir",
            str(tmp_path / "out"),
        ],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(tmp_path / "site"), "SMART_LLM": "openai:m1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    run = load_research_run(tmp_path / "out")
    assert run.state is ResearchTerminalState.OK
    assert run.sources_count == 2


def test_gpt_researcher_runner_without_the_agent_exits_2(tmp_path: Path) -> None:
    (tmp_path / "p.txt").write_text("q")
    proc = _run_runner(
        "gpt_researcher_runner.py", ["--prompt-file", str(tmp_path / "p.txt"), "--out-dir", str(tmp_path / "o")], {}
    )
    assert proc.returncode == 2
    assert "No module named 'gpt_researcher'" in proc.stderr
    assert "No module named 'bernstein'" not in proc.stderr


def test_tongyi_runner_binds_the_agent_to_the_gateway(tmp_path: Path) -> None:
    inference = tmp_path / "checkout" / "inference"
    inference.mkdir(parents=True)
    (inference / "react_agent.py").write_text(_STUB_REACT_AGENT)
    (tmp_path / "p.txt").write_text("What is X?")
    proc = _run_runner(
        "tongyi_deepresearch_runner.py",
        [
            "--prompt-file",
            str(tmp_path / "p.txt"),
            "--out-dir",
            str(tmp_path / "out"),
            "--repo-dir",
            str(tmp_path / "checkout"),
            "--model",
            "m2",
        ],
        {"OPENAI_BASE_URL": "http://gw.test/v1", "OPENAI_API_KEY": "k2"},
    )
    assert proc.returncode == 0, proc.stderr
    run = load_research_run(tmp_path / "out")
    assert run.state is ResearchTerminalState.OK
    assert run.sources_count == 2


_STUB_TOOL_SEARCH = """
import http.client, json
def search(query):
    conn = http.client.HTTPSConnection("google.serper.dev")
    conn.request("POST", "/search", json.dumps({"q": query}), {"X-API-KEY": "sk", "Content-Type": "application/json"})
    return json.loads(conn.getresponse().read())
"""

_STUB_TOOL_VISIT = """
class requests:
    @staticmethod
    def get(url, headers=None, timeout=None):
        return url
def read(url):
    return requests.get(f"https://r.jina.ai/{url}", headers={"Authorization": "Bearer j"}, timeout=5)
"""

_STUB_REDIRECTED_AGENT = """
import tool_search, tool_visit
class OpenAI:
    def __init__(self, **kwargs):
        pass
class MultiTurnReactAgent:
    def __init__(self, llm, function_list):
        pass
    def _run(self, data, model):
        assert tool_search.search("q") == {"organic": []}
        assert tool_visit.read("https://a.test/x") == "http://gw.test/v1/web/fetch/https://a.test/x"
        return {"prediction": "ok", "termination": "answer", "messages": []}
"""


def _tongyi_checkout(tmp_path: Path, agent: str, **tools: str) -> Path:
    inference = tmp_path / "checkout" / "inference"
    inference.mkdir(parents=True)
    (inference / "react_agent.py").write_text(agent)
    for name, source in tools.items():
        (inference / f"{name}.py").write_text(source)
    (tmp_path / "p.txt").write_text("What is X?")
    return tmp_path / "checkout"


def _tongyi_args(tmp_path: Path, checkout: Path) -> list[str]:
    return [
        "--prompt-file",
        str(tmp_path / "p.txt"),
        "--out-dir",
        str(tmp_path / "out"),
        "--repo-dir",
        str(checkout),
        "--model",
        "m2",
    ]


def test_tongyi_runner_sends_search_and_visit_to_the_configured_bases(tmp_path: Path) -> None:
    """Serper calls keep their path under the base's path; reader calls get the base in place of r.jina.ai."""
    seen: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, self.headers["X-API-KEY"]))
            body = b'{"organic": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        checkout = _tongyi_checkout(
            tmp_path, _STUB_REDIRECTED_AGENT, tool_search=_STUB_TOOL_SEARCH, tool_visit=_STUB_TOOL_VISIT
        )
        proc = _run_runner(
            "tongyi_deepresearch_runner.py",
            _tongyi_args(tmp_path, checkout),
            {
                "OPENAI_BASE_URL": "http://gw.test/v1",
                "OPENAI_API_KEY": "k2",
                "SERPER_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "JINA_BASE_URL": "http://gw.test/v1/web/fetch/",
            },
        )
    finally:
        server.shutdown()
    assert proc.returncode == 0, proc.stderr
    assert seen == [("/v1/search", "sk")]


def test_tongyi_runner_rejects_a_tool_base_that_is_not_http(tmp_path: Path) -> None:
    checkout = _tongyi_checkout(
        tmp_path, _STUB_REDIRECTED_AGENT, tool_search=_STUB_TOOL_SEARCH, tool_visit=_STUB_TOOL_VISIT
    )
    proc = _run_runner(
        "tongyi_deepresearch_runner.py",
        _tongyi_args(tmp_path, checkout),
        {"OPENAI_BASE_URL": "http://gw.test/v1", "OPENAI_API_KEY": "k2", "SERPER_BASE_URL": "gw.test/v1"},
    )
    assert proc.returncode == 2
    assert "SERPER_BASE_URL" in proc.stderr
