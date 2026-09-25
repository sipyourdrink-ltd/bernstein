"""Standalone runner for Tongyi DeepResearch, launched by path.

Runs inside the interpreter the agent's requirements are installed in, with
``--repo-dir`` naming its checkout; bernstein is not importable there, so this
imports only the standard library, the agent, and :mod:`deep_research_artifact`
(loaded by file path).

Two changes to the agent, both made here rather than in the checkout:

* its planner client is built for a local inference server
  (``http://127.0.0.1:<port>/v1``, key ``EMPTY``); the client factory in
  ``react_agent`` is replaced by one bound to ``OPENAI_BASE_URL`` and
  ``OPENAI_API_KEY``, so the planner goes through the gateway;
* its context counter loads the served model's tokenizer from a local path;
  a gateway model has none, so the count is estimated from characters
  (4 per token), which keeps the agent's context-limit fallback working.

Exit codes: 0 ok, 3 inconclusive (no answer), 1 the agent failed, 2 the
checkout or its requirements are missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

AGENT = "tongyi-deepresearch"
_TOOL_CALL = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)
_CHARS_PER_TOKEN = 4


def _isolate_and_load_artifact() -> Any:
    """Drop this directory from ``sys.path``; load the artifact module by path.

    Python puts a script's own directory first on ``sys.path``. Here that is
    bernstein's adapter directory, which holds a module named after the agent
    package (``gpt_researcher.py``); left in place, ``import gpt_researcher``
    would load bernstein's adapter instead of the agent.
    """
    here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != here]
    spec = importlib.util.spec_from_file_location("deep_research_artifact", here / "deep_research_artifact.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"deep_research_artifact.py missing beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _loads(text: str) -> Any:
    try:
        import json5  # type: ignore[import-not-found]

        return json5.loads(text)
    except ImportError:
        return json.loads(text)


def visited_urls(messages: list[dict[str, Any]]) -> list[str]:
    """Every URL the agent's ``visit`` tool was asked to open, distinct, sorted."""
    urls: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for raw in _TOOL_CALL.findall(str(message.get("content", ""))):
            try:
                call = _loads(raw.strip())
            except Exception:
                continue
            if not isinstance(call, dict) or call.get("name") != "visit":
                continue
            target = (call.get("arguments") or {}).get("url")
            for url in [target] if isinstance(target, str) else list(target or ()):
                if isinstance(url, str) and url.strip():
                    urls.add(url.strip())
    return sorted(urls)


def _bind_to_gateway(react_agent: Any) -> None:
    real_client = react_agent.OpenAI
    base_url = os.environ["OPENAI_BASE_URL"]
    api_key = os.environ["OPENAI_API_KEY"]

    def gateway_client(*_args: Any, timeout: float = 600.0, **_kwargs: Any) -> Any:
        return real_client(api_key=api_key, base_url=base_url, timeout=timeout)

    def estimated_tokens(_self: Any, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages) // _CHARS_PER_TOKEN

    react_agent.OpenAI = gateway_client
    react_agent.MultiTurnReactAgent.count_tokens = estimated_tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)

    artifact = _isolate_and_load_artifact()

    out_dir = args.out_dir.resolve()
    prompt = args.prompt_file.read_text(encoding="utf-8")
    inference = (args.repo_dir / "inference").resolve()
    if not (inference / "react_agent.py").is_file():
        print(f"no Tongyi DeepResearch checkout at {args.repo_dir} (inference/react_agent.py missing)", file=sys.stderr)
        return 2
    sys.path.insert(0, str(inference))
    os.chdir(inference)  # the agent's file tool resolves ./eval_data relative to here
    try:
        import react_agent  # type: ignore[import-not-found]
    except ImportError as exc:
        print(f"Tongyi DeepResearch requirements are not installed in {sys.executable}: {exc}", file=sys.stderr)
        return 2
    _bind_to_gateway(react_agent)

    started = time.time()
    try:
        agent = react_agent.MultiTurnReactAgent(
            llm={
                "model": args.model,
                "generate_cfg": {
                    "max_input_tokens": 320000,
                    "max_retries": 10,
                    "temperature": 0.6,
                    "top_p": 0.95,
                    "presence_penalty": 1.1,
                },
                "model_type": "qwen_dashscope",
            },
            function_list=["search", "visit", "google_scholar", "PythonInterpreter"],
        )
        result = agent._run({"item": {"question": prompt, "answer": ""}, "planning_port": 0}, args.model)
    except Exception as exc:  # the agent's own failure, recorded not raised
        artifact.write_run(
            out_dir,
            agent=AGENT,
            report="",
            sources=(),
            started_at=started,
            finished_at=time.time(),
            state=artifact.STATE_DRIVER_FAILURE,
            detail=f"{exc.__class__.__name__}: {exc}"[:500],
        )
        return 1

    termination = str(result.get("termination", ""))
    answered = termination == "answer"
    record = artifact.write_run(
        out_dir,
        agent=AGENT,
        report=str(result.get("prediction") or "") if answered else "",
        sources=visited_urls(result.get("messages") or []),
        started_at=started,
        finished_at=time.time(),
        state=artifact.STATE_OK if answered else artifact.STATE_INCONCLUSIVE,
        detail="" if answered else f"agent terminated: {termination or 'unknown'}",
    )
    print(f"{AGENT}: {record['state']}, {record['sources_count']} sources, {record['wall_seconds']} s")
    return 0 if record["state"] == artifact.STATE_OK else 3


if __name__ == "__main__":
    raise SystemExit(main())
