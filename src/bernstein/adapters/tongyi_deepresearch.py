"""Tongyi DeepResearch adapter for Bernstein.

Runs Tongyi DeepResearch's ReAct agent (search, visit, scholar, python tools)
on the task prompt from an operator-provided checkout, and records the answer
and every page its visit tool opened as a verifiable run artifact
(:mod:`.deep_research_artifact`).

Upstream serves the planning model from a local inference server; here both
the planner and the page summariser go through the agent's own gateway
endpoint and key instead (see :mod:`.tongyi_deepresearch_runner`).

Operator settings:

* ``BERNSTEIN_TONGYI_DEEPRESEARCH_OPENAI_BASE_URL`` / ``_OPENAI_API_KEY`` /
  ``_MODEL`` - required; the gateway endpoint, this agent's key, the model;
* ``BERNSTEIN_TONGYI_DEEPRESEARCH_HOME`` - required; the checkout whose
  ``inference/`` directory holds the agent;
* ``BERNSTEIN_TONGYI_DEEPRESEARCH_PYTHON`` - the interpreter with its
  requirements installed (default ``python3``);
* ``BERNSTEIN_TONGYI_DEEPRESEARCH_SUMMARY_MODEL`` - the page-summary model
  (default: the planning model);
* the tool settings the agent reads itself (``SERPER_KEY_ID``,
  ``JINA_API_KEYS``, ``SANDBOX_FUSION_ENDPOINT``, ``MAX_LLM_CALL_PER_RUN``,
  ...) are let through unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from bernstein.adapters.deep_research import DeepResearchAdapter, env_prefix, read_gateway, require_env

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class TongyiDeepResearchAdapter(DeepResearchAdapter):
    """Adapter for Tongyi DeepResearch; unit of work is the research artifact."""

    registry_name = "tongyi_deepresearch"
    slug: ClassVar[str] = "tongyi-deepresearch"
    runner_script: ClassVar[str] = "tongyi_deepresearch_runner.py"
    display_name: ClassVar[str] = "Tongyi DeepResearch"
    passthrough_env: ClassVar[tuple[str, ...]] = (
        "SERPER_KEY_ID",
        "JINA_API_KEYS",
        "SANDBOX_FUSION_ENDPOINT",
        "MAX_LLM_CALL_PER_RUN",
        "VISIT_SERVER_TIMEOUT",
        "VISIT_SERVER_MAX_RETRIES",
        "WEBCONTENT_MAXLENGTH",
    )

    def build_env(self, environ: Mapping[str, str]) -> dict[str, str]:
        gw = read_gateway(self.slug, environ)
        env = self._filtered(environ)
        # Planner (read by the runner) and page summariser (read by the
        # agent's visit tool) share the one endpoint and key.
        env["OPENAI_BASE_URL"] = env["API_BASE"] = gw.base_url
        env["OPENAI_API_KEY"] = env["API_KEY"] = gw.api_key
        summary = (environ.get(env_prefix(self.slug) + "SUMMARY_MODEL") or "").strip()
        env["SUMMARY_MODEL_NAME"] = summary or gw.model
        return env

    def build_command(self, prompt_file: Path, run_dir: Path, environ: Mapping[str, str]) -> list[str]:
        gw = read_gateway(self.slug, environ)
        home = require_env(env_prefix(self.slug) + "HOME", environ, "the Tongyi DeepResearch checkout")
        return [
            self.python(environ),
            str(self.runner_path()),
            "--prompt-file",
            str(prompt_file),
            "--out-dir",
            str(run_dir),
            "--repo-dir",
            home,
            "--model",
            gw.model,
        ]
