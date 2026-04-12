"""Custom Textual widgets for the Bernstein TUI.

This module is a thin re-export shim.  The actual implementations live in:
- ``task_list``      — task display widgets, sparklines, constants
- ``agent_log``      — log and quality gate widgets
- ``status_bar``     — status, scratchpad, and coordinator widgets
- ``approval_panel`` — approval, waterfall, tool observer, and SLO widgets
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

# --- agent_log ---
from bernstein.tui.agent_log import AGENT_ROLE_COLORS_TUI as AGENT_ROLE_COLORS_TUI
from bernstein.tui.agent_log import AgentLogWidget as AgentLogWidget
from bernstein.tui.agent_log import QualityGatePanel as QualityGatePanel
from bernstein.tui.agent_log import QualityGateResult as QualityGateResult
from bernstein.tui.agent_log import ShortcutsFooter as ShortcutsFooter
from bernstein.tui.agent_log import format_agent_label_text as format_agent_label_text
from bernstein.tui.agent_log import get_agent_role_color as get_agent_role_color
from bernstein.tui.agent_log import render_compaction_marker as render_compaction_marker

# --- approval_panel ---
from bernstein.tui.approval_panel import ApprovalAction as ApprovalAction
from bernstein.tui.approval_panel import ApprovalEntry as ApprovalEntry
from bernstein.tui.approval_panel import ApprovalPanel as ApprovalPanel
from bernstein.tui.approval_panel import SLOBurnDownWidget as SLOBurnDownWidget
from bernstein.tui.approval_panel import ToolObserverEntry as ToolObserverEntry
from bernstein.tui.approval_panel import ToolObserverWidget as ToolObserverWidget
from bernstein.tui.approval_panel import WaterfallWidget as WaterfallWidget
from bernstein.tui.approval_panel import build_slo_burndown_text as build_slo_burndown_text
from bernstein.tui.approval_panel import read_new_tool_calls as read_new_tool_calls
from bernstein.tui.approval_panel import render_tool_observer as render_tool_observer
from bernstein.tui.approval_panel import render_waterfall_batches as render_waterfall_batches
from bernstein.tui.status_bar import ROLE_COORDINATOR as ROLE_COORDINATOR
from bernstein.tui.status_bar import ROLE_WORKER as ROLE_WORKER

# --- status_bar ---
from bernstein.tui.status_bar import CoordinatorDashboard as CoordinatorDashboard
from bernstein.tui.status_bar import CoordinatorRow as CoordinatorRow
from bernstein.tui.status_bar import ModelTierEntry as ModelTierEntry
from bernstein.tui.status_bar import ScratchpadEntry as ScratchpadEntry
from bernstein.tui.status_bar import ScratchpadViewer as ScratchpadViewer
from bernstein.tui.status_bar import StatusBar as StatusBar
from bernstein.tui.status_bar import build_coordinator_summary as build_coordinator_summary
from bernstein.tui.status_bar import build_model_tier_entries as build_model_tier_entries
from bernstein.tui.status_bar import classify_role as classify_role
from bernstein.tui.status_bar import filter_scratchpad_entries as filter_scratchpad_entries
from bernstein.tui.status_bar import list_scratchpad_files as list_scratchpad_files
from bernstein.tui.status_bar import render_model_tier_table as render_model_tier_table

# --- task_list ---
from bernstein.tui.task_list import AGENT_IDENTITY_COLORS as AGENT_IDENTITY_COLORS
from bernstein.tui.task_list import COMPACTION_MARKER as COMPACTION_MARKER
from bernstein.tui.task_list import COMPACTION_MARKER_COLOR as COMPACTION_MARKER_COLOR
from bernstein.tui.task_list import SPARKLINE_CHARS as SPARKLINE_CHARS
from bernstein.tui.task_list import STATUS_COLORS as STATUS_COLORS
from bernstein.tui.task_list import STATUS_DOTS as STATUS_DOTS
from bernstein.tui.task_list import WORKER_BADGE_COLORS as WORKER_BADGE_COLORS
from bernstein.tui.task_list import ActionBar as ActionBar
from bernstein.tui.task_list import TaskListWidget as TaskListWidget
from bernstein.tui.task_list import TaskRow as TaskRow
from bernstein.tui.task_list import agent_badge_color as agent_badge_color
from bernstein.tui.task_list import agent_identity_color as agent_identity_color
from bernstein.tui.task_list import build_cache_hit_sparkline as build_cache_hit_sparkline
from bernstein.tui.task_list import build_compaction_marker as build_compaction_marker
from bernstein.tui.task_list import build_token_budget_bar as build_token_budget_bar
from bernstein.tui.task_list import format_agent_label as format_agent_label
from bernstein.tui.task_list import generate_sparkline as generate_sparkline
from bernstein.tui.task_list import status_color as status_color
from bernstein.tui.task_list import status_dot as status_dot
