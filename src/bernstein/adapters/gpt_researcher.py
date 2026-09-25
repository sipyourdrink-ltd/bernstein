"""gpt-researcher adapter for Bernstein.

Runs gpt-researcher's deep-research mode on the task prompt and records the
report and every source URL it read as a verifiable run artifact
(:mod:`.deep_research_artifact`). All three of gpt-researcher's model roles
(fast, smart, strategic) go through the agent's own gateway endpoint and key.

Operator settings:

* ``BERNSTEIN_GPT_RESEARCHER_OPENAI_BASE_URL`` / ``_OPENAI_API_KEY`` /
  ``_MODEL`` - required; the gateway endpoint, this agent's key, the model;
* ``BERNSTEIN_GPT_RESEARCHER_PYTHON`` - the interpreter gpt-researcher is
  installed in (default ``python3``);
* ``BERNSTEIN_GPT_RESEARCHER_EMBEDDING`` - embedding model served by the same
  gateway (default: gpt-researcher's own);
* the retriever settings gpt-researcher reads itself (``RETRIEVER``,
  ``TAVILY_API_KEY``, ...) and its depth knobs are let through unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from bernstein.adapters.deep_research import DeepResearchAdapter, env_prefix, read_gateway

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class GPTResearcherAdapter(DeepResearchAdapter):
    """Adapter for gpt-researcher; unit of work is the research artifact."""

    registry_name = "gpt_researcher"
    slug: ClassVar[str] = "gpt-researcher"
    runner_script: ClassVar[str] = "gpt_researcher_runner.py"
    display_name: ClassVar[str] = "gpt-researcher"
    passthrough_env: ClassVar[tuple[str, ...]] = (
        "RETRIEVER",
        "TAVILY_API_KEY",
        "SERPER_API_KEY",
        "SERPAPI_API_KEY",
        "SEARX_URL",
        "SCRAPER",
        "MAX_SEARCH_RESULTS_PER_QUERY",
        "MAX_ITERATIONS",
        "DEEP_RESEARCH_BREADTH",
        "DEEP_RESEARCH_DEPTH",
        "DEEP_RESEARCH_CONCURRENCY",
        "TOTAL_WORDS",
    )

    #: gpt-researcher's report mode; ``deep`` is its recursive breadth x depth run.
    report_type: ClassVar[str] = "deep"

    def build_env(self, environ: Mapping[str, str]) -> dict[str, str]:
        gw = read_gateway(self.slug, environ)
        env = self._filtered(environ)
        env["OPENAI_BASE_URL"] = gw.base_url
        env["OPENAI_API_KEY"] = gw.api_key
        for role in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM"):
            env[role] = f"openai:{gw.model}"
        embedding = (environ.get(env_prefix(self.slug) + "EMBEDDING") or "").strip()
        if embedding:
            env["EMBEDDING"] = f"openai:{embedding}"
        return env

    def build_command(self, prompt_file: Path, run_dir: Path, environ: Mapping[str, str]) -> list[str]:
        read_gateway(self.slug, environ)
        return [
            self.python(environ),
            str(self.runner_path()),
            "--prompt-file",
            str(prompt_file),
            "--out-dir",
            str(run_dir),
            "--report-type",
            self.report_type,
        ]
