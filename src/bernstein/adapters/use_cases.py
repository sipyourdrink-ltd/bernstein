"""Per-adapter metadata for the ``bernstein integrations list`` command.

Bernstein ships many CLI adapters under :mod:`bernstein.adapters`. Each
adapter file already carries a module docstring with implementation
notes, but the docstrings are written for contributors patching the
adapter and not for end users picking which CLI to wire up.

This module is the single source of truth for end-user copy:

* ``headline`` - one-line use case suitable for a CLI table row.
* ``binary`` - the executable name probed via ``shutil.which`` to
  determine the ``--installed`` flag (overrides the registry key).
* ``details`` - optional multi-line description used by
  ``bernstein integrations list --details``.
* ``docs_path`` - optional repo-relative path to the per-adapter doc
  page.  When absent the index page is linked instead.

Adapters whose copy is missing fall back to the first line of the
module docstring (see ``integrations_cmd._fallback_headline``). New
adapters can either register an entry here or rely on a clean module
docstring; both paths keep the data in the package tree, not in a
separate markdown file that would drift.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterUseCase:
    """End-user copy describing what an adapter is for.

    Attributes:
        headline: Single-line summary, no trailing punctuation. Shown in
            the default table view.
        binary: Name of the CLI binary on ``$PATH`` that the adapter
            shells out to. Empty when the adapter is in-process (e.g.
            ``mock``, ``generic``, SDK-only adapters).
        details: Optional longer-form description shown by
            ``--details``. Use plain hyphens, no em-dashes.
        docs_path: Optional repo-relative path to per-adapter docs
            (e.g. ``docs/adapters/claude.md``).
    """

    headline: str
    binary: str = ""
    details: str = ""
    docs_path: str = ""


# Adapter name -> use case copy. Keys must match the registry keys in
# ``bernstein.adapters.registry``. When in doubt about a binary name,
# cross-reference ``adapter_cmd._BINARY_OVERRIDES``.
USE_CASES: dict[str, AdapterUseCase] = {
    "agy": AdapterUseCase(
        headline="Antigravity CLI (agy) - successor to the non-enterprise Gemini CLI path",
        binary="agy",
        details=(
            "Print-mode headless runs with the terminal sandbox pinned and "
            "permission prompts auto-approved. Use the gemini adapter for "
            "the enterprise / API-key lane; see the lane table in the docs."
        ),
        docs_path="docs/adapters/agy.md",
    ),
    "aichat": AdapterUseCase(
        headline="Multi-provider chat CLI with shell command and file context",
        binary="aichat",
        details=(
            "Wraps the aichat binary so Bernstein can route prompts to any "
            "OpenAI, Anthropic, or local provider it supports without "
            "swapping adapters."
        ),
    ),
    "aider": AdapterUseCase(
        headline="Pair-programming CLI with automatic commits in the worktree",
        binary="aider",
        details=(
            "Non-interactive aider run via --message --yes. Aider commits "
            "directly, which is fine inside an isolated Bernstein worktree "
            "and gets merged when the branch lands."
        ),
    ),
    "amp": AdapterUseCase(
        headline="Sourcegraph Amp CLI for repository-wide refactors",
        binary="amp",
    ),
    "auggie": AdapterUseCase(
        headline="Augment Code Auggie CLI driven from Bernstein",
        binary="auggie",
    ),
    "autohand": AdapterUseCase(
        headline="Autohand Code CLI for autonomous task execution",
        binary="autohand",
    ),
    "charm": AdapterUseCase(
        headline="Charm Crush CLI - terminal coding agent with TUI affordances",
        binary="crush",
    ),
    "claude": AdapterUseCase(
        headline="Anthropic Claude Code CLI - default headless coding agent",
        binary="claude",
        details=(
            "Spawns ``claude --print`` with MCP config merging, "
            "cache-control blocks, and per-role tool allowlists. The most "
            "broadly exercised adapter in the test matrix."
        ),
        docs_path="docs/adapters/ADAPTER_GUIDE.md",
    ),
    "cline": AdapterUseCase(
        headline="Cline (formerly Claude Dev) CLI for autonomous tasks",
        binary="cline",
    ),
    "clm": AdapterUseCase(
        headline="Sovereign LLM gateway adapter with mTLS launcher",
        binary="clm",
        details=(
            "For customer-side CLM gateways behind an mTLS boundary. "
            "Useful for air-gap and regulated deployments where requests "
            "never leave the customer network."
        ),
        docs_path="docs/adapters/clm.md",
    ),
    "cloudflare": AdapterUseCase(
        headline="Cloudflare Agents SDK driver via wrangler dev or worker trigger",
        binary="wrangler",
    ),
    "codebuddy": AdapterUseCase(
        headline="Tencent CodeBuddy CLI - headless print mode with stream-json events",
        binary="codebuddy",
        docs_path="docs/adapters/codebuddy.md",
    ),
    "codebuff": AdapterUseCase(
        headline="Codebuff CLI integration",
        binary="codebuff",
    ),
    "codex": AdapterUseCase(
        headline="OpenAI Codex CLI - GPT-family coding agent",
        binary="codex",
        details=(
            "Non-interactive Codex run with structured output. Pairs well "
            "with Claude in the same Bernstein plan when you want a "
            "cross-model review pass."
        ),
    ),
    "cody": AdapterUseCase(
        headline="Sourcegraph Cody CLI with code graph context",
        binary="cody",
    ),
    "composio": AdapterUseCase(
        headline="Composio Agent Orchestrator (ao) CLI",
        binary="ao",
    ),
    "continue": AdapterUseCase(
        headline="Continue.dev CLI for IDE-style coding agents in the terminal",
        binary="continue",
    ),
    "copilot": AdapterUseCase(
        headline="GitHub Copilot CLI driven headlessly from Bernstein",
        binary="copilot",
        details=(
            "Non-interactive print mode runs with -p and -s, plus "
            "--allow-all-tools and --no-ask-user so the agent works "
            "autonomously without blocking on permission or clarifying "
            "prompts. Reuses existing GitHub Copilot auth."
        ),
    ),
    "cursor": AdapterUseCase(
        headline="Cursor Agent CLI - print mode with workspace trust",
        binary="cursor-agent",
        details=(
            "Headless cursor-agent runs with --print, --force for actual "
            "edits, and --trust to skip the workspace prompt. Shares the "
            "editor's .cursor/mcp.json by default."
        ),
    ),
    "devin_terminal": AdapterUseCase(
        headline="Devin for Terminal (Cognition) CLI",
        binary="devin",
    ),
    "droid": AdapterUseCase(
        headline="Droid CLI by Factory AI",
        binary="droid",
    ),
    "forge": AdapterUseCase(
        headline="Forge CLI integration",
        binary="forge",
    ),
    "gemini": AdapterUseCase(
        headline="Google Gemini CLI for Gemini 2.5 Pro and Flash",
        binary="gemini",
    ),
    "generic": AdapterUseCase(
        headline="Wrap an arbitrary coding agent CLI by command string",
        binary="",
        details=(
            "Use this when your tool of choice has no first-class adapter "
            "yet. Configure ``cli_command`` and ``display_name`` and "
            "Bernstein will spawn it with the orchestration contract."
        ),
    ),
    "goose": AdapterUseCase(
        headline="Block Goose - extensible coding agent with provider switch",
        binary="goose",
    ),
    "gptme": AdapterUseCase(
        headline="gptme - local-first coding agent with shell and Python tools",
        binary="gptme",
    ),
    "grok": AdapterUseCase(
        headline="xAI Grok Build CLI - headless coding agent with auto-approve",
        binary="grok",
        docs_path="docs/adapters/grok.md",
    ),
    "hermes": AdapterUseCase(
        headline="Hermes Agent by Nous Research",
        binary="hermes",
    ),
    "iac": AdapterUseCase(
        headline="Infrastructure-as-Code agent (Terraform/Pulumi) with plan-before-apply",
        binary="",
        details=(
            "Orchestrates IaC agents that always run plan/preview before "
            "apply. Enforces operator approval on destructive changes."
        ),
    ),
    "jules": AdapterUseCase(
        headline="Google Jules Tools CLI - dispatch tasks to the async cloud agent",
        binary="jules",
        docs_path="docs/adapters/jules.md",
    ),
    "junie": AdapterUseCase(
        headline="JetBrains Junie CLI for IDE-aligned coding tasks",
        binary="junie",
    ),
    "kilo": AdapterUseCase(
        headline="Kilo CLI by Stackblitz",
        binary="kilo",
    ),
    "kimi": AdapterUseCase(
        headline="Kimi CLI for Moonshot models",
        binary="kimi",
    ),
    "kiro": AdapterUseCase(
        headline="Kiro CLI integration",
        binary="kiro",
    ),
    "letta_code": AdapterUseCase(
        headline="Letta Code CLI - stateful coding agent",
        binary="letta",
    ),
    "mimo": AdapterUseCase(
        headline="Xiaomi MiMo Code CLI - OpenCode-core agent with skip-permissions runs",
        binary="mimo",
        docs_path="docs/adapters/mimo.md",
    ),
    "mistral": AdapterUseCase(
        headline="Mistral Vibe CLI",
        binary="mistral",
    ),
    "mock": AdapterUseCase(
        headline="Test-only stub adapter - no API keys, no network",
        binary="",
        details=(
            "Used for zero-API-key demos, CI smoke tests, and the "
            "conformance harness. Selecting ``mock`` skips real LLM "
            "calls entirely."
        ),
    ),
    "ollama": AdapterUseCase(
        headline="Local Ollama / OpenAI-compatible models without cloud keys",
        binary="ollama",
        details=(
            "Drives Aider as the coding frontend with Ollama (or any "
            "OpenAI-compatible) server. Useful for offline development "
            "and benchmark runs that must stay on-prem."
        ),
    ),
    "open_interpreter": AdapterUseCase(
        headline="Open Interpreter CLI - natural language to shell and code",
        binary="interpreter",
    ),
    "openai_agents": AdapterUseCase(
        headline="OpenAI Agents SDK v2 (in-process Python adapter)",
        binary="",
        details=(
            "Imports the OpenAI Agents SDK and runs the session via a "
            "Python entrypoint instead of a subprocess. Install with "
            "``pip install 'bernstein[openai]'``."
        ),
        docs_path="docs/adapters/openai-agents.md",
    ),
    "opencode": AdapterUseCase(
        headline="OpenCode (sst/opencode) terminal coding agent",
        binary="opencode",
    ),
    "openhands": AdapterUseCase(
        headline="OpenHands (formerly OpenDevin) headless run",
        binary="openhands",
    ),
    "pi": AdapterUseCase(
        headline="pi-coding-agent CLI",
        binary="pi",
    ),
    "plandex": AdapterUseCase(
        headline="Plandex CLI - context-aware planning agent",
        binary="plandex",
    ),
    "q_dev": AdapterUseCase(
        headline="AWS Q Developer CLI (binary: q)",
        binary="q",
    ),
    "qoder": AdapterUseCase(
        headline="Alibaba Qoder CLI (binary: qodercli) - headless prompt runs",
        binary="qodercli",
        docs_path="docs/adapters/qoder.md",
    ),
    "qwen": AdapterUseCase(
        headline="Qwen CLI for OpenAI-compatible Qwen models",
        binary="qwen",
    ),
    "ralphex": AdapterUseCase(
        headline="Ralphex (umputun/ralphex) coding agent",
        binary="ralphex",
    ),
    "rovo": AdapterUseCase(
        headline="Atlassian Rovo Dev CLI",
        binary="rovo",
    ),
    "trae": AdapterUseCase(
        headline="ByteDance Trae Agent CLI (binary: trae-cli) - autonomous one-shot runs",
        binary="trae-cli",
        docs_path="docs/adapters/trae.md",
    ),
    "warp": AdapterUseCase(
        headline="Warp Agent CLI (binary: oz) - non-interactive agent runs",
        binary="oz",
        docs_path="docs/adapters/warp.md",
    ),
}


__all__ = ["USE_CASES", "AdapterUseCase"]
