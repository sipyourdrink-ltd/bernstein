"""Direction A: export Bernstein scenarios as Claude Code Routine configs.

A Routine = prompt + repos + environment + connectors + triggers, configured
through ``claude.ai/code/routines``. There is no programmatic creation API
(research preview), so this module produces everything an operator needs to
paste into the Routine UI:

* a prompt body that instructs the Routine to drive Bernstein orchestration,
* an MCP connector config pointing at the local or remote Bernstein server,
* a setup guide that walks through the manual steps,
* a list of recommended trigger types derived from scenario tags.

This is one half of rt-003. Direction B (Routine -> Bernstein scenario lookup)
lives in :mod:`bernstein.mcp.routine_tools` and the new
:class:`RoutineBridge` in :mod:`bernstein.core.planning.routine_bridge`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.planning.scenario_library import (
        ScenarioLibrary,
        ScenarioRecipe,
        ScenarioTaskTemplate,
    )


@dataclass(frozen=True)
class TriggerRecommendation:
    """A single trigger configuration suggestion for a Routine.

    Attributes:
        type: One of ``"github"``, ``"schedule"``, ``"api"``.
        event: Event name when ``type == "github"``
            (e.g. ``"pull_request.opened"``).
        cadence: Cadence string when ``type == "schedule"``
            (e.g. ``"daily"``, ``"hourly"``).
        reason: Short human-readable justification for the suggestion.
    """

    type: str
    reason: str
    event: str = ""
    cadence: str = ""


@dataclass(frozen=True)
class RoutineExport:
    """All artefacts needed to provision a Routine from a scenario.

    Attributes:
        scenario_id: Source scenario identifier.
        name: Suggested Routine name (``"Bernstein: <scenario name>"``).
        prompt: Markdown prompt to paste into the Routine prompt field.
        recommended_triggers: Trigger configurations the operator should set.
        mcp_config: JSON blob to register as an MCP connector.
        environment_vars: Environment variables the Routine session needs
            (e.g. ``BERNSTEIN_AUTH_TOKEN``).
        setup_instructions: Step-by-step markdown guide.
    """

    scenario_id: str
    name: str
    prompt: str
    recommended_triggers: tuple[TriggerRecommendation, ...]
    mcp_config: dict[str, object]
    environment_vars: dict[str, str]
    setup_instructions: str


# Tag groups to map scenario tags onto trigger recommendations.
_REVIEW_TAGS = frozenset({"review", "ci", "pr"})
_MAINTENANCE_TAGS = frozenset({"maintenance", "cleanup", "schedule", "nightly"})
_DEPLOY_TAGS = frozenset({"deploy", "verify", "verification"})


def recommend_triggers(scenario: ScenarioRecipe) -> tuple[TriggerRecommendation, ...]:
    """Suggest Routine triggers based on a scenario's tags.

    Args:
        scenario: Scenario to inspect.

    Returns:
        A tuple of trigger recommendations, possibly empty. Each recommendation
        is independent - operators can pick one or several.
    """
    tags = {t.lower() for t in scenario.tags}
    out: list[TriggerRecommendation] = []
    if tags & _REVIEW_TAGS:
        out.append(
            TriggerRecommendation(
                type="github",
                event="pull_request.opened",
                reason="Review and CI scenarios should run on every new PR",
            )
        )
    if tags & _MAINTENANCE_TAGS:
        out.append(
            TriggerRecommendation(
                type="schedule",
                cadence="daily",
                reason="Maintenance scenarios run best on a nightly schedule",
            )
        )
    if tags & _DEPLOY_TAGS:
        out.append(
            TriggerRecommendation(
                type="api",
                reason="Deploy verification should be triggered by the CD pipeline",
            )
        )
    if not out:
        # Fall back to a manual API trigger so the operator always has at
        # least one option to attach.
        out.append(
            TriggerRecommendation(
                type="api",
                reason="No tags matched a known pattern; use manual API trigger",
            )
        )
    return tuple(out)


def _format_task_list(tasks: tuple[ScenarioTaskTemplate, ...]) -> str:
    """Render a markdown bullet list of scenario tasks."""
    lines: list[str] = []
    for idx, t in enumerate(tasks, start=1):
        suffix = f" - {t.description}" if t.description else ""
        lines.append(f"{idx}. **{t.title}** (role: `{t.role}`, priority: {t.priority}){suffix}")
    return "\n".join(lines)


def build_routine_prompt(scenario: ScenarioRecipe, bernstein_url: str) -> str:
    """Produce the markdown prompt body the Routine session will execute.

    The prompt instructs the Routine to:

    1. Connect to the Bernstein MCP server at ``bernstein_url``.
    2. Call ``bernstein_scenario`` with this scenario's id (and PR/branch
       context if it was triggered by a GitHub event).
    3. Poll ``bernstein_scenario`` with ``action="status"`` until completion.
    4. Summarise the outcome (and post a PR comment when applicable).

    Args:
        scenario: Source scenario.
        bernstein_url: Bernstein task server URL the MCP connector points at.

    Returns:
        Markdown prompt body suitable for pasting into the Routine UI.
    """
    task_list = _format_task_list(scenario.tasks)
    return (
        "You are a Bernstein orchestration relay.\n"
        "When triggered, you drive a multi-agent Bernstein run end to end.\n"
        "\n"
        "## Steps\n"
        "1. Call the Bernstein MCP tool `bernstein_scenario` with:\n"
        "   - action: `run`\n"
        f"   - scenario_id: `{scenario.scenario_id}`\n"
        "   - context: short summary of the trigger event\n"
        "   - pr_number / branch: from the trigger payload when present\n"
        '2. Poll `bernstein_scenario` with action="status" and the returned\n'
        "   orchestration_id every 30s.\n"
        "3. When all tasks finish, summarise outcomes:\n"
        "   - per-task status (passed / failed)\n"
        "   - quality gate failures, if any\n"
        "   - if triggered by a PR, post a review comment with findings\n"
        "\n"
        f"## Scenario: {scenario.name}\n"
        f"{scenario.description}\n"
        "\n"
        f"## Tasks Bernstein will spawn ({len(scenario.tasks)})\n"
        f"{task_list}\n"
        "\n"
        "## Expected behaviour\n"
        f"- Bernstein decomposes this into {len(scenario.tasks)} parallel tasks.\n"
        "- Each task runs in an isolated git worktree.\n"
        "- Quality gates verify output before merge.\n"
        "- A failing task auto-retries with an escalated model.\n"
        "\n"
        "## MCP connector\n"
        f"The Bernstein MCP server should be registered with base URL `{bernstein_url}`.\n"
    )


def build_mcp_config(bernstein_url: str) -> dict[str, object]:
    """Return the MCP connector config to register inside the Routine.

    Args:
        bernstein_url: Bernstein task server URL.

    Returns:
        A JSON-serialisable dict describing the MCP server connection.
    """
    return {
        "name": "bernstein",
        "command": "bernstein",
        "args": ["mcp"],
        "transport": "stdio",
        "env": {
            "BERNSTEIN_SERVER_URL": bernstein_url,
        },
    }


def build_env_vars(scenario: ScenarioRecipe) -> dict[str, str]:
    """Return the environment variables the Routine session should declare.

    Args:
        scenario: Source scenario (used for tag-conditional defaults).

    Returns:
        Mapping of env var name to documentation/default string. Operators
        replace empty values with real secrets when configuring the Routine.
    """
    env: dict[str, str] = {
        "BERNSTEIN_AUTH_TOKEN": "",
        "BERNSTEIN_SERVER_URL": "http://127.0.0.1:8052",
    }
    if "github" in scenario.tags or any("review" in t for t in scenario.tags):
        env["GITHUB_TOKEN"] = ""
    return env


def build_setup_guide(scenario: ScenarioRecipe, repo: str) -> str:
    """Produce a markdown setup guide for the operator.

    Args:
        scenario: Source scenario.
        repo: Target repository in ``owner/name`` form.

    Returns:
        Markdown walk-through of the manual provisioning steps.
    """
    triggers = recommend_triggers(scenario)
    trigger_lines = "\n".join(
        f"- **{r.type}**"
        + (f" (`{r.event}`)" if r.event else "")
        + (f" cadence `{r.cadence}`" if r.cadence else "")
        + f": {r.reason}"
        for r in triggers
    )
    return (
        f"# Provision Routine: {scenario.name}\n"
        "\n"
        "## 1. Open the Routine UI\n"
        "Visit https://claude.ai/code/routines and click **New Routine**.\n"
        "\n"
        "## 2. Paste the prompt\n"
        "Copy `prompt.md` (in this directory) into the Routine prompt field.\n"
        "\n"
        "## 3. Attach the repository\n"
        f"Add `{repo}` as the working repository.\n"
        "\n"
        "## 4. Register the MCP connector\n"
        "Paste the contents of `mcp-config.json` into the Routine's connectors\n"
        "section. The Routine will spawn a `bernstein mcp` stdio process to\n"
        "talk to the Bernstein task server.\n"
        "\n"
        "## 5. Set environment variables\n"
        "Configure the variables listed in `env.json`. `BERNSTEIN_AUTH_TOKEN`\n"
        "is required when the task server has auth enabled; `GITHUB_TOKEN` is\n"
        "required for review or comment-posting scenarios.\n"
        "\n"
        "## 6. Configure a trigger\n"
        f"{trigger_lines}\n"
        "\n"
        "## 7. Save the Routine and copy the trigger id\n"
        "Run `bernstein routine register --scenario "
        f"{scenario.scenario_id} --trigger-id <id>` so Bernstein can correlate\n"
        "incoming webhooks with this scenario.\n"
    )


@dataclass
class RoutineProvisioner:
    """Generate ready-to-use Routine configs from Bernstein scenarios.

    Attributes:
        library: Scenario library to look scenarios up in.
        bernstein_url: Default base URL of the Bernstein task server. Used
            both inside the prompt and inside the MCP connector config.
    """

    library: ScenarioLibrary
    bernstein_url: str = "http://127.0.0.1:8052"

    def list_scenarios(self) -> list[ScenarioRecipe]:
        """Return all scenarios known to this provisioner."""
        return list(self.library.scenarios.values())

    def export_scenario_as_routine(
        self,
        scenario_id: str,
        repo: str,
        bernstein_url: str | None = None,
    ) -> RoutineExport:
        """Build a :class:`RoutineExport` for ``scenario_id``.

        Args:
            scenario_id: Identifier of the scenario to export.
            repo: Target repository in ``owner/name`` form.
            bernstein_url: Override the default Bernstein server URL.

        Returns:
            The export bundle.

        Raises:
            KeyError: If ``scenario_id`` is not in the library.
        """
        recipe = self.library.get(scenario_id)
        if recipe is None:
            msg = f"Unknown scenario: {scenario_id}"
            raise KeyError(msg)
        url = bernstein_url or self.bernstein_url
        return RoutineExport(
            scenario_id=recipe.scenario_id,
            name=f"Bernstein: {recipe.name}",
            prompt=build_routine_prompt(recipe, url),
            recommended_triggers=recommend_triggers(recipe),
            mcp_config=build_mcp_config(url),
            environment_vars=build_env_vars(recipe),
            setup_instructions=build_setup_guide(recipe, repo),
        )

    def write_export(self, export: RoutineExport, out_dir: Path) -> list[Path]:
        """Write all artefacts of ``export`` to ``out_dir``.

        Args:
            export: Bundle produced by :meth:`export_scenario_as_routine`.
            out_dir: Target directory. Created if missing.

        Returns:
            The list of files written, in deterministic order.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        triggers_md = _format_triggers_markdown(export.recommended_triggers)

        files: list[tuple[Path, str]] = [
            (out_dir / "prompt.md", export.prompt),
            (out_dir / "mcp-config.json", json.dumps(export.mcp_config, indent=2) + "\n"),
            (out_dir / "env.json", json.dumps(export.environment_vars, indent=2) + "\n"),
            (out_dir / "setup-guide.md", export.setup_instructions),
            (out_dir / "triggers.md", triggers_md),
        ]
        for path, content in files:
            path.write_text(content, encoding="utf-8")
        return [p for p, _ in files]


def _format_triggers_markdown(triggers: tuple[TriggerRecommendation, ...]) -> str:
    """Render the ``triggers.md`` artefact."""
    if not triggers:
        return "# Recommended Triggers\n\nNo automatic recommendations.\n"
    lines = ["# Recommended Triggers", ""]
    for r in triggers:
        header = f"## {r.type}"
        if r.event:
            header += f" - `{r.event}`"
        if r.cadence:
            header += f" - cadence `{r.cadence}`"
        lines.extend((header, "", r.reason, ""))
    return "\n".join(lines).rstrip() + "\n"
