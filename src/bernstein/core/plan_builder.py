"""PlanBuilder: renders TaskPlan as editable markdown.

The markdown output is designed to be opened in $EDITOR, modified by the
user, and (optionally) parsed back into a plan object.  Every section is
machine-parseable while remaining human-friendly.

Typical flow:
    1. After goal decomposition, create_plan() returns a TaskPlan.
    2. PlanBuilder(plan, tasks).render_to_markdown() → string written to stdout
       or a temp file opened in $EDITOR.
    3. User edits model assignments, reorders tasks, adds notes.
    4. (Separate step) parse_markdown_plan() reads the edited file back.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from bernstein.core.models import PlanStatus, Task, TaskPlan

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RISK_ICON: dict[str, str] = {
    "low": "✓",
    "medium": "⚠",
    "high": "⚡",
    "critical": "🔴",
}

_STATUS_LABEL: dict[str, str] = {
    PlanStatus.PENDING.value: "pending — awaiting approval",
    PlanStatus.APPROVED.value: "approved",
    PlanStatus.REJECTED.value: "rejected",
    PlanStatus.EXPIRED.value: "expired",
}


def _fmt_cost(usd: float) -> str:
    """Format a USD cost for display."""
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def _fmt_minutes(minutes: int) -> str:
    """Format minutes as 'Xh Ym' or just 'Ym'."""
    if minutes >= 60:
        h, m = divmod(minutes, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{minutes}m"


def _topological_order(tasks: list[Task]) -> list[Task]:
    """Return tasks in topological (dependency-first) order.

    Uses Kahn's algorithm.  Tasks with no entry in the input list are
    treated as already-resolved external dependencies.

    Args:
        tasks: List of Task objects to order.

    Returns:
        Tasks sorted so that dependencies always come before dependents.
        Cycles are broken by appending remaining nodes at the end.
    """
    by_id: dict[str, Task] = {t.id: t for t in tasks}
    in_degree: dict[str, int] = {t.id: 0 for t in tasks}
    dependents: dict[str, list[str]] = {t.id: [] for t in tasks}

    for task in tasks:
        for dep_id in task.depends_on:
            if dep_id in by_id:
                in_degree[task.id] += 1
                dependents[dep_id].append(task.id)

    queue = sorted(
        [t_id for t_id, deg in in_degree.items() if deg == 0],
        key=lambda t_id: by_id[t_id].priority,
    )
    ordered: list[Task] = []
    while queue:
        t_id = queue.pop(0)
        ordered.append(by_id[t_id])
        for dep in sorted(dependents[t_id], key=lambda d: by_id[d].priority):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Append any remaining (cyclic) tasks
    seen = {t.id for t in ordered}
    ordered.extend(t for t in tasks if t.id not in seen)
    return ordered


# ---------------------------------------------------------------------------
# PlanBuilder
# ---------------------------------------------------------------------------


class PlanBuilder:
    """Assembles and renders an execution plan as editable markdown.

    Args:
        plan: The TaskPlan produced by :func:`~bernstein.core.plan_approval.create_plan`.
        tasks: Optional list of Task objects for dependency and effort details.
            When provided, richer dependency information and effort hints are
            included in the output.
    """

    def __init__(self, plan: TaskPlan, tasks: list[Task] | None = None) -> None:
        self._plan = plan
        self._tasks: dict[str, Task] = {}
        if tasks:
            self._tasks = {t.id: t for t in tasks}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render_to_markdown(self) -> str:
        """Render the plan as a complete, editable markdown document.

        The returned string is ready for display in a terminal or writing to
        a file that can be opened in ``$EDITOR``.

        Returns:
            Markdown string containing all plan components: task list, agent
            assignments, model selections, cost breakdown, and dependency order.
        """
        parts: list[str] = [
            self._header(),
            self._summary_table(),
            self._task_list(),
            self._cost_breakdown(),
            self._dependency_order(),
            self._agent_assignments(),
            self._footer(),
        ]
        return "\n\n".join(p for p in parts if p) + "\n"

    # ------------------------------------------------------------------
    # Private section renderers
    # ------------------------------------------------------------------

    def _header(self) -> str:
        plan = self._plan
        created = datetime.datetime.fromtimestamp(plan.created_at, tz=datetime.timezone.utc)
        status_label = _STATUS_LABEL.get(plan.status.value, plan.status.value)
        lines = [
            f"# Execution Plan: {plan.id}",
            "",
            f"**Goal:** {plan.goal}",
            f"**Status:** {status_label}",
            f"**Created:** {created.strftime('%Y-%m-%d %H:%M UTC')}",
        ]
        if plan.decision_reason:
            lines.append(f"**Decision note:** {plan.decision_reason}")
        return "\n".join(lines)

    def _summary_table(self) -> str:
        plan = self._plan
        high_risk_count = len(plan.high_risk_tasks)
        rows = [
            ("Total tasks", str(len(plan.task_estimates))),
            ("Estimated cost", _fmt_cost(plan.total_estimated_cost_usd)),
            ("Estimated time", _fmt_minutes(plan.total_estimated_minutes)),
            ("High-risk tasks", str(high_risk_count)),
        ]
        table = "## Summary\n\n| Metric | Value |\n|--------|-------|\n"
        table += "".join(f"| {label} | {value} |\n" for label, value in rows)
        return table

    def _task_list(self) -> str:
        plan = self._plan
        if not plan.task_estimates:
            return "## Tasks\n\n_No tasks in this plan._"

        lines = [
            "## Tasks",
            "",
            "<!-- Edit model/effort per task, reorder, or add context notes. -->",
        ]

        for i, est in enumerate(plan.task_estimates, start=1):
            task = self._tasks.get(est.task_id)
            effort = (task.effort or "auto") if task else "auto"
            scope = task.scope.value if task else "medium"
            depends_on_ids = task.depends_on if task else []
            if depends_on_ids:
                dep_str = ", ".join(depends_on_ids)
            else:
                dep_str = "none"

            risk_icon = _RISK_ICON.get(est.risk_level, "?")
            risk_reasons_str = "; ".join(est.risk_reasons) if est.risk_reasons else "none"

            lines += [
                "",
                f"### {i}. {est.title}",
                "",
                f"- **ID:** `{est.task_id}`",
                f"- **Role:** {est.role}",
                f"- **Model:** {est.model}",
                f"- **Effort:** {effort}",
                f"- **Scope:** {scope}",
                f"- **Estimated time:** {_fmt_minutes(task.estimated_minutes if task else 30)}",
                f"- **Estimated cost:** {_fmt_cost(est.estimated_cost_usd)}",
                f"- **Risk:** {risk_icon} {est.risk_level}",
                f"- **Risk reasons:** {risk_reasons_str}",
                f"- **Depends on:** {dep_str}",
            ]

        return "\n".join(lines)

    def _cost_breakdown(self) -> str:
        plan = self._plan
        if not plan.task_estimates:
            return ""

        header = (
            "## Cost Breakdown\n\n"
            "| # | Task | Role | Model | Est. Tokens | Est. Cost | Risk |\n"
            "|---|------|------|-------|-------------|-----------|------|\n"
        )
        rows = ""
        for i, est in enumerate(plan.task_estimates, start=1):
            risk_icon = _RISK_ICON.get(est.risk_level, "?")
            rows += (
                f"| {i} | {est.title} | {est.role} | {est.model} "
                f"| {est.estimated_tokens:,} | {_fmt_cost(est.estimated_cost_usd)} "
                f"| {risk_icon} {est.risk_level} |\n"
            )

        total_tokens = sum(e.estimated_tokens for e in plan.task_estimates)
        rows += f"| | **Total** | | | **{total_tokens:,}** | **{_fmt_cost(plan.total_estimated_cost_usd)}** | |\n"
        return header + rows

    def _dependency_order(self) -> str:
        plan = self._plan
        if not self._tasks:
            # No Task objects — just list in original order with IDs
            lines = [
                "## Dependency Order",
                "",
                "<!-- Topological execution order (add Task objects for full dependency graph) -->",
            ]
            for i, est in enumerate(plan.task_estimates, start=1):
                lines.append(f"{i}. `{est.task_id}` — {est.title}")
            return "\n".join(lines)

        # Full topological sort using Task.depends_on
        tasks_in_plan = [t for t in self._tasks.values() if t.id in {e.task_id for e in plan.task_estimates}]
        ordered = _topological_order(tasks_in_plan)

        lines = [
            "## Dependency Order",
            "",
            "<!-- Tasks listed in execution order; dependencies resolved before dependents. -->",
        ]
        for i, task in enumerate(ordered, start=1):
            deps = task.depends_on
            if deps:
                # Show only deps that are also in this plan
                plan_ids = {e.task_id for e in plan.task_estimates}
                local_deps = [d for d in deps if d in plan_ids]
                external_deps = [d for d in deps if d not in plan_ids]
                dep_parts: list[str] = []
                if local_deps:
                    dep_parts.append(f"after: {', '.join(f'`{d}`' for d in local_deps)}")
                if external_deps:
                    dep_parts.append(f"external: {', '.join(f'`{d}`' for d in external_deps)}")
                dep_note = f" ({'; '.join(dep_parts)})"
            else:
                dep_note = " (no deps)"
            lines.append(f"{i}. `{task.id}` — {task.title}{dep_note}")

        return "\n".join(lines)

    def _agent_assignments(self) -> str:
        plan = self._plan
        # Collect unique role → assigned_agent mappings
        role_agents: dict[str, str] = {}
        for est in plan.task_estimates:
            task = self._tasks.get(est.task_id)
            agent = task.assigned_agent if task and task.assigned_agent else "unassigned"
            # Last non-unassigned agent wins per role
            existing = role_agents.get(est.role)
            if existing is None or (existing == "unassigned" and agent != "unassigned"):
                role_agents[est.role] = agent

        lines = [
            "## Agent Assignments",
            "",
            "<!-- Maps each role to its assigned agent. Edit to override auto-selection. -->",
            "",
            "| Role | Agent |",
            "|------|-------|",
        ]
        for role, agent in sorted(role_agents.items()):
            lines.append(f"| {role} | {agent} |")

        return "\n".join(lines)

    def _footer(self) -> str:
        return (
            "---\n"
            "<!-- To approve: `bernstein plans approve {plan_id}` -->\n"
            "<!-- To reject:  `bernstein plans reject {plan_id}` -->\n"
            "<!-- To re-run:  `bernstein run --from-plan <path>` -->"
        ).replace("{plan_id}", self._plan.id)
