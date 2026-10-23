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

from bernstein.adapters.deep_research import DeepResearchAdapter, env_prefix, read_gateway, require_env, url_endpoint
from bernstein.core.security.network_policy import NetworkPolicyDenied

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


#: Where each retriever gpt-researcher ships with sends its queries. A retriever
#: not listed (``custom``, ``mcp``, anything newer) has no destination bernstein
#: can name, so a restrictive policy refuses it. ``searx`` is the operator's own
#: ``SEARX_URL``.
_RETRIEVER_HOSTS: dict[str, tuple[str, int]] = {
    "arxiv": ("export.arxiv.org", 443),
    "bing": ("api.bing.microsoft.com", 443),
    "bocha": ("api.bochaai.com", 443),
    "duckduckgo": ("duckduckgo.com", 443),
    "exa": ("api.exa.ai", 443),
    "google": ("www.googleapis.com", 443),
    "pubmed_central": ("eutils.ncbi.nlm.nih.gov", 443),
    "searchapi": ("www.searchapi.io", 443),
    "semantic_scholar": ("api.semanticscholar.org", 443),
    "serpapi": ("serpapi.com", 443),
    "serper": ("google.serper.dev", 443),
    "tavily": ("api.tavily.com", 443),
}

#: Scrapers that call a fixed service. The rest fetch the pages the search
#: returned, from hosts only the search can name (see ``tool_endpoints``).
_SCRAPER_HOSTS: dict[str, tuple[str, int]] = {
    "tavily_extract": ("api.tavily.com", 443),
    "firecrawl": ("api.firecrawl.dev", 443),
}
_LOCAL_SCRAPERS = frozenset({"bs", "browser", "nodriver", "web_base_loader", "pymupdf"})


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

    def tool_endpoints(self, environ: Mapping[str, str]) -> list[tuple[str, int]]:
        """The retrievers' search hosts, and the scraper's service if it has one.

        gpt-researcher then fetches the pages its searches return, from hosts
        no configuration names; a host allow-list bounds the services, not
        those page fetches (see docs/adapters/deep-research.md).
        """
        endpoints: list[tuple[str, int]] = []
        retrievers = [r.strip().lower() for r in (environ.get("RETRIEVER") or "tavily").split(",")]
        for retriever in filter(None, retrievers):
            if retriever == "searx":
                endpoints.append(
                    url_endpoint(require_env("SEARX_URL", environ, "the searx retriever"), source="SEARX_URL")
                )
            elif retriever in _RETRIEVER_HOSTS:
                endpoints.append(_RETRIEVER_HOSTS[retriever])
            else:
                raise NetworkPolicyDenied(f"retriever:{retriever}", source=f"adapter:{self.name()}")
        scraper = (environ.get("SCRAPER") or "bs").strip().lower()
        if scraper in _SCRAPER_HOSTS:
            endpoints.append(_SCRAPER_HOSTS[scraper])
        elif scraper not in _LOCAL_SCRAPERS:
            raise NetworkPolicyDenied(f"scraper:{scraper}", source=f"adapter:{self.name()}")
        return endpoints

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
