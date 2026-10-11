"""PaperQA2 adapter for Bernstein.

Runs PaperQA2's agent (search the paper index, gather evidence, answer) on the
task prompt over an operator-provided directory of papers, and records the
cited answer and every paper it cited as a verifiable run artifact
(:mod:`.deep_research_artifact`). Every model PaperQA2 calls - answer,
summary, agent, enrichment and embedding - goes through the agent's own
gateway endpoint and key (see :mod:`.paper_qa_runner`).

Operator settings:

* ``BERNSTEIN_PAPER_QA_OPENAI_BASE_URL`` / ``_OPENAI_API_KEY`` / ``_MODEL`` -
  required; the gateway endpoint, this agent's key, the model;
* ``BERNSTEIN_PAPER_QA_PAPERS`` - required; the directory of papers to answer
  from;
* ``BERNSTEIN_PAPER_QA_EMBEDDING`` - embedding model served by the same
  gateway (default ``text-embedding-3-small``);
* ``BERNSTEIN_PAPER_QA_PYTHON`` - the interpreter PaperQA2 is installed in
  (default ``python3``);
* the settings PaperQA2 reads itself (``PQA_HOME``, where its indexes live)
  are let through unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from bernstein.adapters.deep_research import DeepResearchAdapter, env_prefix, read_gateway, require_env

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

#: PaperQA2's own default embedding model, here served by the gateway.
DEFAULT_EMBEDDING = "text-embedding-3-small"


class PaperQAAdapter(DeepResearchAdapter):
    """Adapter for PaperQA2; unit of work is the research artifact."""

    registry_name = "paper_qa"
    slug: ClassVar[str] = "paper-qa"
    runner_script: ClassVar[str] = "paper_qa_runner.py"
    display_name: ClassVar[str] = "PaperQA2"
    passthrough_env: ClassVar[tuple[str, ...]] = ("PQA_HOME",)

    def build_env(self, environ: Mapping[str, str]) -> dict[str, str]:
        gw = read_gateway(self.slug, environ)
        env = self._filtered(environ)
        # PaperQA2 calls every model through LiteLLM, whose OpenAI provider
        # reads the endpoint and key from here; the runner names each model
        # ``openai/<model>`` so none of them falls through to another provider.
        env["OPENAI_API_BASE"] = env["OPENAI_BASE_URL"] = gw.base_url
        env["OPENAI_API_KEY"] = gw.api_key
        return env

    def build_command(self, prompt_file: Path, run_dir: Path, environ: Mapping[str, str]) -> list[str]:
        gw = read_gateway(self.slug, environ)
        papers = require_env(env_prefix(self.slug) + "PAPERS", environ, "the directory of papers")
        embedding = (environ.get(env_prefix(self.slug) + "EMBEDDING") or "").strip() or DEFAULT_EMBEDDING
        return [
            self.python(environ),
            str(self.runner_path()),
            "--prompt-file",
            str(prompt_file),
            "--out-dir",
            str(run_dir),
            "--papers-dir",
            papers,
            "--model",
            gw.model,
            "--embedding",
            embedding,
        ]
