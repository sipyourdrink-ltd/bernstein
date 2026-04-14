"""Dashboard side panels and expert views.

Extracted from dashboard.py -- QualityPanel, DelegationTreePanel,
ExpertCostPanel, ExpertBanditPanel, and ExpertDepsPanel classes.
"""

from __future__ import annotations

from typing import Any, cast

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from bernstein.cli.dashboard_polling import ROLE_COLORS

_STYLE_BOLD_BRIGHT_CYAN = "bold bright_cyan"


def _rate_color(rate: float) -> str:
    """Return a Rich color name for a success/pass rate."""
    if rate >= 0.95:
        return "bright_green"
    if rate >= 0.80:
        return "bright_yellow"
    return "bright_red"


def _fmt_secs(secs: float) -> str:
    """Format seconds into a compact duration string."""
    if secs <= 0:
        return "-"
    if secs < 60:
        return f"{secs:.0f}s"
    return f"{secs / 60:.1f}m"


# -- Quality metrics panel -----------------------------------------


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


class QualityPanel(Static):
    """Quality metrics: success rates, tokens, guardrails, latency."""

    can_focus = False

    quality: reactive[dict[str, Any]] = reactive(dict)  # type: ignore[assignment]

    def render(self) -> Text:
        q: dict[str, Any] = self.quality
        t = Text()

        if not q:
            t.append(" QUALITY", style="bold dim")
            t.append("\n waiting...", style="dim")
            return t

        overall: dict[str, Any] = q.get("overall", {})
        per_model: dict[str, Any] = q.get("per_model", {})
        guardrail_pass: float = float(q.get("guardrail_pass_rate", 1.0))
        rejection_rate: float = float(q.get("review_rejection_rate", 0.0))
        success_rate: float = float(overall.get("success_rate", 1.0))

        total = int(overall.get("total_tasks", 0))
        t.append(" QUALITY", style=_STYLE_BOLD_BRIGHT_CYAN)
        t.append(f"  {total} tasks", style="dim")
        t.append("\n")

        self._render_rates(t, success_rate, guardrail_pass, rejection_rate)
        self._render_latency(t, overall)
        self._render_per_model(t, per_model)

        return t

    def _render_rates(self, t: Text, success_rate: float, guardrail_pass: float, rejection_rate: float) -> None:
        """Append success, guardrail, and rejection rates."""
        sr_color = _rate_color(success_rate)
        t.append(f" \u2713 {success_rate * 100:.1f}%", style=f"bold {sr_color}")
        t.append(" success  ", style="dim")
        gr_color = _rate_color(guardrail_pass)
        t.append(f"\u29c2 {guardrail_pass * 100:.1f}%", style=f"bold {gr_color}")
        t.append(" guardrails", style="dim")
        t.append("\n")

        rj_color = "bright_red" if rejection_rate > 0.1 else ("bright_yellow" if rejection_rate > 0.05 else "dim")
        t.append(f" \u2717 {rejection_rate * 100:.1f}%", style=f"bold {rj_color}")
        t.append(" rejection", style="dim")
        t.append("\n")

    def _render_latency(self, t: Text, overall: dict[str, Any]) -> None:
        """Append completion time distribution."""
        p50 = float(overall.get("p50_completion_seconds", 0))
        p90 = float(overall.get("p90_completion_seconds", 0))
        p99 = float(overall.get("p99_completion_seconds", 0))
        if p50 <= 0 and p90 <= 0:
            return
        t.append("\n \u23f1 ", style="bright_cyan")
        t.append("p50 ", style="dim")
        t.append(_fmt_secs(p50), style="bold")
        t.append("  p90 ", style="dim")
        t.append(_fmt_secs(p90), style="bold")
        t.append("  p99 ", style="dim")
        t.append(_fmt_secs(p99), style="bold")
        t.append("\n")

    def _render_per_model(self, t: Text, per_model: dict[str, Any]) -> None:
        """Append per-model quality breakdown."""
        if not per_model:
            return
        t.append("\n \u25a4 PER MODEL", style="bold dim")
        t.append("\n")
        for model, stats in sorted(per_model.items()):
            sr = float(stats.get("success_rate", 1.0))
            avg_tok = float(stats.get("avg_tokens", 0))
            p50_m = float(stats.get("p50_completion_seconds", 0))
            color = _rate_color(sr)
            short_model = model.replace("claude-", "").replace("-20", "-")[:18]
            t.append(f"  {short_model}", style="bold")
            t.append(f"  {sr * 100:.0f}%", style=f"bold {color}")
            if avg_tok > 0:
                t.append(f"  {avg_tok / 1000:.1f}k\u29f3", style="dim")
            if p50_m > 0:
                t.append(f"  {_fmt_secs(p50_m)}", style="dim")
            t.append("\n")


# -- Delegation tree panel ----------------------------------------


class DelegationTreePanel(Static):
    """Shows the agent session delegation tree: who spawned whom."""

    can_focus = False

    agents: reactive[list[dict[str, Any]]] = reactive(list)  # type: ignore[assignment]

    # Unicode tree characters
    _BRANCH = "\u251c\u2500 "  # ├─
    _LAST = "\u2514\u2500 "  # └─
    _PIPE = "\u2502  "  # │
    _BLANK = "   "

    def render(self) -> Text:
        agents: list[dict[str, Any]] = self.agents
        t = Text()
        t.append(" DELEGATION", style=_STYLE_BOLD_BRIGHT_CYAN)
        t.append("\n")

        alive = [a for a in agents if a.get("status") != "dead"]
        if not alive:
            t.append(" no agents", style="dim")
            return t

        roots, children = self._build_tree(alive)
        roots_sorted = sorted(roots, key=lambda a: a.get("spawn_ts", 0))
        for i, root in enumerate(roots_sorted):
            self._render_node(t, root, "", i == len(roots_sorted) - 1, children)

        return t

    def _build_tree(
        self, alive: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """Build tree from parent_id or cell_id grouping."""
        by_id = {a["id"]: a for a in alive if a.get("id")}
        children: dict[str, list[dict[str, Any]]] = {}
        roots: list[dict[str, Any]] = []

        if any(a.get("parent_id") for a in alive):
            for a in alive:
                pid = a.get("parent_id")
                if pid and pid in by_id:
                    children.setdefault(pid, []).append(a)
                else:
                    roots.append(a)
        else:
            roots, children = self._build_tree_from_cells(alive)
        return roots, children

    def _build_tree_from_cells(
        self, alive: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """Group agents by cell_id when parent_id links are absent."""
        cells: dict[str, list[dict[str, Any]]] = {}
        no_cell: list[dict[str, Any]] = []
        children: dict[str, list[dict[str, Any]]] = {}
        roots: list[dict[str, Any]] = []

        for a in alive:
            cid = a.get("cell_id")
            if cid:
                cells.setdefault(cid, []).append(a)
            else:
                no_cell.append(a)

        for _cid, members in sorted(cells.items()):
            leads = [m for m in members if m.get("role", "") in ("manager", "vp", "orchestrator")]
            workers = [m for m in members if m not in leads]
            if leads:
                root = leads[0]
                roots.append(root)
                child_list = leads[1:] + workers
                if child_list:
                    children[root["id"]] = child_list
            else:
                roots.extend(members)
        roots.extend(no_cell)
        return roots, children

    def _render_node(
        self,
        t: Text,
        a: dict[str, Any],
        prefix: str,
        is_last: bool,
        children: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Render a single tree node and recurse into children."""
        role = a.get("role", "?")
        aid = a.get("id", "")
        status = a.get("status", "?")
        model = (a.get("model") or "").replace("claude-", "").replace("-2025", "")[:12]
        runtime = int(a.get("runtime_s", 0))
        m, s = divmod(runtime, 60)
        source = a.get("agent_source", "")

        connector = self._LAST if is_last else self._BRANCH
        dot_color = {"working": "bright_green", "starting": "bright_yellow", "dead": "bright_red"}.get(status, "dim")
        dot = {"working": "\u25c9", "starting": "\u25ce", "dead": "\u25cc"}.get(status, "\u25cf")

        t.append(prefix + connector, style="dim")
        t.append(f"{dot} ", style=f"bold {dot_color}")
        rc = ROLE_COLORS.get(role.lower(), "bright_white")
        t.append(role.upper(), style=f"bold {rc}")
        if source and source not in ("built-in", "builtin", ""):
            t.append(f" ({source})", style=f"italic {rc}")
        if model:
            t.append(f"  {model}", style="dim")
        t.append(f"  {m}:{s:02d}", style="dim")
        cell_id = a.get("cell_id")
        if cell_id:
            t.append(f"  [{cell_id}]", style="dim")
        t.append("\n")

        kids = children.get(aid, [])
        child_prefix = prefix + (self._BLANK if is_last else self._PIPE)
        for i, kid in enumerate(kids):
            self._render_node(t, kid, child_prefix, i == len(kids) - 1, children)


# -- Expert mode panels -------------------------------------------


class ExpertCostPanel(Static):
    """Expert mode: detailed per-model cost breakdown."""

    can_focus = False

    costs: reactive[dict[str, Any]] = reactive(dict)  # type: ignore[assignment]

    def render(self) -> Text:
        t = Text()
        t.append(" COST DETAIL", style=_STYLE_BOLD_BRIGHT_CYAN)
        t.append("\n")
        c = self.costs
        if not c:
            t.append(" no cost data", style="dim")
            return t

        spent = float(c.get("spent_usd", 0.0))
        budget = float(c.get("budget_usd", 0.0))
        per_model: dict[str, float] = c.get("per_model", {})

        t.append(f" ${spent:.4f}", style="bold bright_green")
        if budget > 0:
            pct = spent / budget * 100
            t.append(f" / ${budget:.2f} ({pct:.0f}%)", style="dim")
        t.append("\n")

        if per_model:
            t.append("\n", style="")
            for model, cost in sorted(per_model.items(), key=lambda x: -x[1])[:6]:
                short = model.replace("claude-", "").replace("-2025", "")[:20]
                bar_w = 8
                ratio = cost / spent if spent > 0 else 0.0
                filled = int(ratio * bar_w)
                bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
                t.append(f"  {short:<20}", style="bold")
                t.append(f" ${cost:.4f}", style="bright_green")
                t.append(f" {bar}\n", style="dim")

        return t


class ExpertBanditPanel(Static):
    """Expert mode: multi-armed bandit routing statistics."""

    can_focus = False

    bandit: reactive[dict[str, Any]] = reactive(dict)  # type: ignore[assignment]

    def render(self) -> Text:
        t = Text()
        t.append(" BANDIT ROUTING", style=_STYLE_BOLD_BRIGHT_CYAN)
        t.append("\n")
        b = self.bandit
        if not b or b.get("active") is False:
            t.append(" not active", style="dim")
            t.append("\n \u2014routing bandit to enable", style="dim")
            return t

        selection_frequency = cast(_CAST_DICT_STR_ANY, b.get("selection_frequency", {}) or {})
        exploration_stats = cast(_CAST_DICT_STR_ANY, b.get("exploration_stats", {}) or {})
        shadow_stats = cast(_CAST_DICT_STR_ANY, b.get("shadow_stats", {}) or {})

        total_completions = int(b.get("total_completions", 0) or 0)
        exploration_rate = float(b.get("exploration_rate", 0.0) or 0.0)
        mode = str(b.get("mode", "bandit"))

        t.append(f" {total_completions} completions", style="bold")
        t.append(f"  explore={exploration_rate:.3f}", style="dim")
        t.append(f"\n {mode}", style="dim")
        t.append("\n\n", style="")

        self._render_arms(t, selection_frequency, exploration_stats)
        self._render_shadow(t, shadow_stats)

        return t

    def _render_arms(
        self, t: Text, selection_frequency: dict[str, Any], exploration_stats: dict[str, Any]
    ) -> None:
        """Render per-arm selection and mean reward stats."""
        arms = sorted(selection_frequency.items(), key=lambda item: (-int(item[1]), item[0]))
        for model, pulls_raw in arms:
            pulls = int(pulls_raw)
            stats = exploration_stats.get(model, {}) if isinstance(exploration_stats.get(model), dict) else {}
            mean = float(stats.get("mean", 0.0) or 0.0)
            last = float(stats.get("last", 0.0) or 0.0)
            short = model.replace("claude-", "").replace("-2025", "")[:18]
            mean_color = "bright_green" if mean <= 0.15 else ("bright_yellow" if mean <= 0.35 else "bright_red")
            t.append(f"  {short:<18}", style="bold")
            t.append(f" {pulls:>3d} sel", style="dim")
            t.append(f"  \u03bc={mean:.3f}", style=f"bold {mean_color}")
            t.append(f"  last={last:.3f}\n", style="dim")

    def _render_shadow(self, t: Text, shadow_stats: dict[str, Any]) -> None:
        """Render shadow mode agreement stats."""
        matched = int(shadow_stats.get("matched_outcomes", 0) or 0)
        pending = int(shadow_stats.get("pending_outcomes", 0) or 0)
        if matched <= 0 and pending <= 0:
            return
        t.append("\n shadow ", style=_STYLE_BOLD_BRIGHT_CYAN)
        t.append(
            f"agree={float(shadow_stats.get('agreement_rate', 0.0) or 0.0):.0%} "
            f"disagree={int(shadow_stats.get('disagreement_count', 0) or 0)} "
            f"pending={pending}",
            style="dim",
        )


class ExpertDepsPanel(Static):
    """Expert mode: task dependency overview."""

    can_focus = False

    tasks: reactive[list[dict[str, Any]]] = reactive(list)  # type: ignore[assignment]

    def render(self) -> Text:
        t = Text()
        t.append(" DEPENDENCIES", style=_STYLE_BOLD_BRIGHT_CYAN)
        t.append("\n")
        all_tasks: list[dict[str, Any]] = self.tasks

        tasks_with_deps = [tk for tk in all_tasks if tk.get("depends_on")]
        blocked = [tk for tk in all_tasks if tk.get("status") == "blocked"]

        if not tasks_with_deps and not blocked:
            t.append(" no task dependencies", style="dim")
            return t

        t.append(f" {len(tasks_with_deps)} with deps", style="bold")
        if blocked:
            t.append(f"  {len(blocked)} blocked", style="bold bright_red")
        t.append("\n\n", style="")

        shown = blocked[:4] if blocked else tasks_with_deps[:4]
        for task in shown:
            status = str(task.get("status", ""))
            title = str(task.get("title", "?"))[:28]
            deps = task.get("depends_on", [])
            st_color = "bright_red" if status == "blocked" else "dim"
            t.append(f"  {title}\n", style=f"bold {st_color}")
            if isinstance(deps, list):
                for dep in list(deps)[:2]:
                    t.append(f"    \u2190 {str(dep)[:22]}\n", style="dim")

        return t
