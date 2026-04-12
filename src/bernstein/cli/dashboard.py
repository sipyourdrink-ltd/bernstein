"""Bernstein TUI -- retro-futuristic agent orchestration dashboard.

Design: Bloomberg terminal meets early macOS. Dark, clean, information-dense.
Three columns: Agents (live logs) | Tasks (status board) | Activity feed.
Bottom: sparkline + stats + chat input.

This module is a thin re-export shim. Implementation lives in:
- dashboard_polling: helpers, data loaders, formatters, constants
- dashboard_header: DashboardHeader, AgentListContainer, AgentWidget, BigStats
- dashboard_actions: QualityPanel, DelegationTreePanel, Expert*Panel
- dashboard_app: BernsteinApp, ChatInput, run_dashboard
"""

from __future__ import annotations

# -- dashboard_actions: side panels and expert views --
from bernstein.cli.dashboard_actions import DelegationTreePanel as DelegationTreePanel
from bernstein.cli.dashboard_actions import ExpertBanditPanel as ExpertBanditPanel
from bernstein.cli.dashboard_actions import ExpertCostPanel as ExpertCostPanel
from bernstein.cli.dashboard_actions import ExpertDepsPanel as ExpertDepsPanel
from bernstein.cli.dashboard_actions import QualityPanel as QualityPanel

# -- dashboard_app: main application class --
from bernstein.cli.dashboard_app import BernsteinApp as BernsteinApp
from bernstein.cli.dashboard_app import ChatInput as ChatInput
from bernstein.cli.dashboard_app import run_dashboard as run_dashboard
from bernstein.cli.dashboard_header import _AGENT_WIDGET_HEIGHT as _AGENT_WIDGET_HEIGHT
from bernstein.cli.dashboard_header import _MAX_VISIBLE_AGENTS as _MAX_VISIBLE_AGENTS

# -- dashboard_header: header and agent display widgets --
from bernstein.cli.dashboard_header import AgentListContainer as AgentListContainer
from bernstein.cli.dashboard_header import AgentWidget as AgentWidget
from bernstein.cli.dashboard_header import BigStats as BigStats
from bernstein.cli.dashboard_header import DashboardHeader as DashboardHeader
from bernstein.cli.dashboard_polling import _RETRY_PATTERNS as _RETRY_PATTERNS
from bernstein.cli.dashboard_polling import _SPARK_CHARS as _SPARK_CHARS

# -- dashboard_polling: constants, helpers, formatters --
from bernstein.cli.dashboard_polling import AGENT_STATUS as AGENT_STATUS
from bernstein.cli.dashboard_polling import ROLE_COLORS as ROLE_COLORS
from bernstein.cli.dashboard_polling import SERVER_URL as SERVER_URL
from bernstein.cli.dashboard_polling import STATUS_ICONS as STATUS_ICONS
from bernstein.cli.dashboard_polling import _build_runtime_subtitle as _build_runtime_subtitle
from bernstein.cli.dashboard_polling import _build_status_icons as _build_status_icons
from bernstein.cli.dashboard_polling import _fetch_all as _fetch_all
from bernstein.cli.dashboard_polling import _format_activity_line as _format_activity_line
from bernstein.cli.dashboard_polling import _format_elapsed_label as _format_elapsed_label
from bernstein.cli.dashboard_polling import _format_gate_report_lines as _format_gate_report_lines
from bernstein.cli.dashboard_polling import _format_relative_age as _format_relative_age
from bernstein.cli.dashboard_polling import _gate_status_color as _gate_status_color
from bernstein.cli.dashboard_polling import _get as _get
from bernstein.cli.dashboard_polling import _gradient_text as _gradient_text
from bernstein.cli.dashboard_polling import _load_activity_summaries as _load_activity_summaries
from bernstein.cli.dashboard_polling import _load_agents as _load_agents
from bernstein.cli.dashboard_polling import _load_cache_stats as _load_cache_stats
from bernstein.cli.dashboard_polling import _load_guardrail_violations as _load_guardrail_violations
from bernstein.cli.dashboard_polling import _load_quarantine as _load_quarantine
from bernstein.cli.dashboard_polling import _mini_cost_sparkline as _mini_cost_sparkline
from bernstein.cli.dashboard_polling import _post as _post
from bernstein.cli.dashboard_polling import _priority_cell as _priority_cell
from bernstein.cli.dashboard_polling import _role_glyph as _role_glyph
from bernstein.cli.dashboard_polling import _summarize_agent_errors as _summarize_agent_errors
from bernstein.cli.dashboard_polling import _tail_log as _tail_log
from bernstein.cli.dashboard_polling import _task_retry_count as _task_retry_count
