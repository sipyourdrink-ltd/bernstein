"""Discord bot command handlers for community support and orchestration demos.

Pure command-handler logic for a Discord bot.  No external Discord
library is imported --- this module provides the parsing, formatting,
and response-generation layer.  A separate integration module is
responsible for wiring these handlers to the actual bot framework.

Ref: GitHub issue #637.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Version / constants
# ---------------------------------------------------------------------------

_VERSION = "0.1.0"
_START_TIME = time.monotonic()

_KNOWN_TOPICS: dict[str, str] = {
    "setup": (
        "**Getting Started**\n"
        "1. Install: `pip install bernstein`\n"
        "2. Configure adapters in `bernstein.yaml`\n"
        "3. Run: `bernstein run plans/my-project.yaml`\n"
        "See https://github.com/chernistry/bernstein for full docs."
    ),
    "plans": (
        "**Plan Files (YAML)**\n"
        "Plans describe multi-step projects with `stages` and `steps`.\n"
        "Stages support `depends_on: [stage_name]` for ordering.\n"
        "Steps accept `goal`, `role`, `priority`, `scope`, `complexity`.\n"
        "Execute with: `bernstein run plans/my-project.yaml`"
    ),
    "adapters": (
        "**CLI Agent Adapters**\n"
        "Bernstein ships adapters for many CLI agents:\n"
        "Claude Code, Codex, Gemini CLI, Aider, AMP, Qwen, "
        "Cursor, Cody, Continue, Goose, Kilo, Kiro, "
        "Ollama, OpenCode, and a generic fallback.\n"
        "Set `adapter:` in your plan or `bernstein.yaml`."
    ),
    "quality-gates": (
        "**Quality Gates**\n"
        "Quality gates run after each task to verify correctness:\n"
        "- Ruff lint + format checks\n"
        "- Pyright strict type checking\n"
        "- pytest test suite\n"
        "Configure thresholds in `bernstein.yaml` under `quality:`."
    ),
}

_ADAPTER_COUNT = 17  # adapters shipped with Bernstein


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscordCommand:
    """Descriptor for a single bot command.

    Attributes:
        name: Slash-style command name (without prefix).
        description: One-line human-readable summary.
        handler_name: Name of the handler function in this module.
    """

    name: str
    description: str
    handler_name: str


@dataclass(frozen=True)
class BotConfig:
    """Configuration for the Discord bot.

    Attributes:
        token_env_var: Environment variable holding the bot token.
        prefix: Command prefix character(s).
        guild_id: Optional Discord guild (server) ID to restrict to.
    """

    token_env_var: str = "BERNSTEIN_DISCORD_TOKEN"
    prefix: str = "/"
    guild_id: str | None = None


@dataclass(frozen=True)
class CommandResult:
    """Structured response returned by a command handler.

    Attributes:
        content: Plain-text message body.
        embed_data: Optional Discord embed dictionary.
        ephemeral: If ``True`` the response should only be visible to
            the invoking user.
    """

    content: str
    embed_data: dict[str, Any] | None = None
    ephemeral: bool = False


# ---------------------------------------------------------------------------
# Command handlers (pure, no I/O)
# ---------------------------------------------------------------------------


def handle_demo(_args: str) -> CommandResult:
    """Return a formatted demo showing a sample orchestration run.

    Args:
        args: Optional arguments (currently unused, reserved for future
            demo variants).

    Returns:
        A ``CommandResult`` with demo output and an embed.
    """
    demo_text = (
        "**Sample Orchestration Run**\n"
        "```\n"
        "$ bernstein run plans/refactor-auth.yaml\n"
        "\n"
        "[stage 1/3] Planning\n"
        "  -> Spawning architect agent (claude, high effort)\n"
        "  -> Task: analyse auth module, propose refactor plan\n"
        "  -> Completed in 42s\n"
        "\n"
        "[stage 2/3] Implementation\n"
        "  -> Spawning backend agent (codex, medium effort)\n"
        "  -> Task: refactor auth middleware\n"
        "  -> Spawning qa agent (gemini, low effort)\n"
        "  -> Task: write integration tests\n"
        "  -> Both completed in 1m 18s\n"
        "\n"
        "[stage 3/3] Quality gates\n"
        "  -> ruff: passed\n"
        "  -> pyright: passed\n"
        "  -> pytest: 47 passed, 0 failed\n"
        "\n"
        "Run complete. 3 stages, 4 tasks, 2m 00s total.\n"
        "```"
    )

    embed = format_embed(
        title="Orchestration Demo",
        description="A typical multi-stage Bernstein run.",
        fields=[
            {"name": "Stages", "value": "3", "inline": True},
            {"name": "Tasks", "value": "4", "inline": True},
            {"name": "Agents", "value": "architect, backend, qa", "inline": True},
        ],
        color=0x5865F2,
    )

    return CommandResult(content=demo_text, embed_data=embed)


def handle_help(topic: str) -> CommandResult:
    """Return contextual help for the given topic.

    Supported topics: ``setup``, ``plans``, ``adapters``,
    ``quality-gates``.  An unknown or empty topic returns a list of
    available topics.

    Args:
        topic: Help topic string (case-insensitive, stripped).

    Returns:
        A ``CommandResult`` with the help text (ephemeral).
    """
    normalised = topic.strip().lower()

    if normalised in _KNOWN_TOPICS:
        return CommandResult(
            content=_KNOWN_TOPICS[normalised],
            ephemeral=True,
        )

    available = ", ".join(f"`{t}`" for t in sorted(_KNOWN_TOPICS))
    return CommandResult(
        content=f"Available help topics: {available}",
        ephemeral=True,
    )


def handle_status() -> CommandResult:
    """Return community stats (placeholder values).

    Returns:
        A ``CommandResult`` with version, uptime, and adapter count
        wrapped in an embed.
    """
    uptime_s = time.monotonic() - _START_TIME
    uptime_m = int(uptime_s // 60)
    uptime_h = int(uptime_m // 60)
    remaining_m = uptime_m % 60

    uptime_str = f"{uptime_h}h {remaining_m}m" if uptime_h > 0 else f"{uptime_m}m"

    embed = format_embed(
        title="Bernstein Status",
        description="Current orchestrator status.",
        fields=[
            {"name": "Version", "value": _VERSION, "inline": True},
            {"name": "Uptime", "value": uptime_str, "inline": True},
            {"name": "Adapters", "value": str(_ADAPTER_COUNT), "inline": True},
        ],
        color=0x57F287,
    )

    return CommandResult(
        content=f"Bernstein v{_VERSION} | uptime {uptime_str} | {_ADAPTER_COUNT} adapters",
        embed_data=embed,
    )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def get_available_commands() -> list[DiscordCommand]:
    """Return the list of commands the bot supports.

    Returns:
        A list of ``DiscordCommand`` descriptors.
    """
    return [
        DiscordCommand(
            name="demo",
            description="Show a sample orchestration run",
            handler_name="handle_demo",
        ),
        DiscordCommand(
            name="help",
            description="Get help on a topic (setup, plans, adapters, quality-gates)",
            handler_name="handle_help",
        ),
        DiscordCommand(
            name="status",
            description="Show orchestrator version, uptime, and adapter count",
            handler_name="handle_status",
        ),
    ]


def format_embed(
    title: str,
    description: str,
    fields: list[dict[str, Any]],
    color: int = 0x5865F2,
) -> dict[str, Any]:
    """Build a Discord-style embed dictionary.

    Args:
        title: Embed title.
        description: Embed description text.
        fields: List of field dicts, each with ``name``, ``value``,
            and optional ``inline`` keys.
        color: Integer colour value (default Discord blurple).

    Returns:
        A dictionary matching the Discord embed JSON structure.
    """
    return {
        "title": title,
        "description": description,
        "color": color,
        "fields": [
            {
                "name": f["name"],
                "value": f["value"],
                "inline": f.get("inline", False),
            }
            for f in fields
        ],
    }


def parse_command(
    message_content: str,
    prefix: str = "/",
) -> tuple[str, str]:
    """Parse a prefixed command string into (command, args).

    If ``message_content`` does not start with ``prefix``, returns
    ``("", "")``.

    Args:
        message_content: Raw message text.
        prefix: Command prefix to look for.

    Returns:
        A ``(command, args)`` tuple.  ``command`` is lowercase and
        stripped; ``args`` contains everything after the command name.
    """
    if not message_content.startswith(prefix):
        return ("", "")

    without_prefix = message_content[len(prefix) :]
    parts = without_prefix.strip().split(maxsplit=1)

    if not parts:
        return ("", "")

    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return (command, args)
