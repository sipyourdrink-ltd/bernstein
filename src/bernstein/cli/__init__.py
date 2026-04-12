"""CLI entry points.

Sub-packages: commands/, ui/, utils/, plan/.
Backward compatibility: ``from bernstein.cli.<module> import X`` is
redirected to the correct sub-package automatically.
"""

from __future__ import annotations

import importlib
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_CLI_REDIRECT_MAP: dict[str, str] = {
    "ab_test_cmd": "bernstein.cli.commands.ab_test_cmd",
    "adapter_cmd": "bernstein.cli.commands.adapter_cmd",
    "advanced_cmd": "bernstein.cli.commands.advanced_cmd",
    "agents_cmd": "bernstein.cli.commands.agents_cmd",
    "aliases": "bernstein.cli.utils.aliases",
    "api_check_cmd": "bernstein.cli.commands.api_check_cmd",
    "audit_cmd": "bernstein.cli.commands.audit_cmd",
    "auth_cmd": "bernstein.cli.commands.auth_cmd",
    "benchmark_charts": "bernstein.cli.display.benchmark_charts",
    "cache_cmd": "bernstein.cli.commands.cache_cmd",
    "changelog_cmd": "bernstein.cli.commands.changelog_cmd",
    "changelog_display": "bernstein.cli.display.changelog_display",
    "chaos_cmd": "bernstein.cli.commands.chaos_cmd",
    "checkpoint_cmd": "bernstein.cli.commands.checkpoint_cmd",
    "ci_cmd": "bernstein.cli.commands.ci_cmd",
    "cli_history": "bernstein.cli.utils.cli_history",
    "compare_screen": "bernstein.cli.display.compare_screen",
    "completions_cmd": "bernstein.cli.commands.completions_cmd",
    "compliance_cmd": "bernstein.cli.commands.compliance_cmd",
    "config_diff_cli": "bernstein.cli.commands.config_diff_cli",
    "config_path_cmd": "bernstein.cli.commands.config_path_cmd",
    "contextual_help": "bernstein.cli.utils.contextual_help",
    "conversation_inspector": "bernstein.cli.display.conversation_inspector",
    "cost": "bernstein.cli.commands.cost",
    "cost_estimate": "bernstein.cli.utils.cost_estimate",
    "crt_effects": "bernstein.cli.display.crt_effects",
    "delegate_cmd": "bernstein.cli.commands.delegate_cmd",
    "dep_impact_cmd": "bernstein.cli.commands.dep_impact_cmd",
    "diff_cmd": "bernstein.cli.commands.diff_cmd",
    "disaster_recovery_cmd": "bernstein.cli.commands.disaster_recovery_cmd",
    "doctor_cmd": "bernstein.cli.commands.doctor_cmd",
    "drain_screen": "bernstein.cli.display.drain_screen",
    "dry_run_cmd": "bernstein.cli.commands.dry_run_cmd",
    "error_suggestions": "bernstein.cli.utils.error_suggestions",
    "errors": "bernstein.cli.utils.errors",
    "eval_benchmark_cmd": "bernstein.cli.commands.eval_benchmark_cmd",
    "evolve_cmd": "bernstein.cli.commands.evolve_cmd",
    "explain_cmd": "bernstein.cli.commands.explain_cmd",
    "explain_help_cmd": "bernstein.cli.commands.explain_help_cmd",
    "figlet_logo": "bernstein.cli.display.figlet_logo",
    "fingerprint_cmd": "bernstein.cli.commands.fingerprint_cmd",
    "frame_buffer": "bernstein.cli.display.frame_buffer",
    "gateway_cmd": "bernstein.cli.commands.gateway_cmd",
    "gradients": "bernstein.cli.display.gradients",
    "graph_cmd": "bernstein.cli.commands.graph_cmd",
    "icons": "bernstein.cli.display.icons",
    "image_renderer": "bernstein.cli.display.image_renderer",
    "incident_cmd": "bernstein.cli.commands.incident_cmd",
    "init_wizard_cmd": "bernstein.cli.commands.init_wizard_cmd",
    "lazy_loader": "bernstein.cli.utils.lazy_loader",
    "leaderboard": "bernstein.cli.display.leaderboard",
    "logs_group_cmd": "bernstein.cli.commands.logs_group_cmd",
    "maintenance_cmd": "bernstein.cli.commands.maintenance_cmd",
    "man_page": "bernstein.cli.utils.man_page",
    "manifest_cmd": "bernstein.cli.commands.manifest_cmd",
    "mcp_cmd": "bernstein.cli.commands.mcp_cmd",
    "memory_cmd": "bernstein.cli.commands.memory_cmd",
    "merge_cmd": "bernstein.cli.commands.merge_cmd",
    "plan_diff": "bernstein.cli.plan.plan_diff",
    "plan_display": "bernstein.cli.plan.plan_display",
    "plan_explain": "bernstein.cli.plan.plan_explain",
    "plan_generate_cmd": "bernstein.cli.commands.plan_generate_cmd",
    "plan_validate_cmd": "bernstein.cli.commands.plan_validate_cmd",
    "playground": "bernstein.cli.commands.playground",
    "policy_cmd": "bernstein.cli.commands.policy_cmd",
    "postmortem_cmd": "bernstein.cli.commands.postmortem_cmd",
    "profile_cmd": "bernstein.cli.commands.profile_cmd",
    "prompts_cmd": "bernstein.cli.commands.prompts_cmd",
    "quickstart_cmd": "bernstein.cli.commands.quickstart_cmd",
    "quickstart_templates": "bernstein.cli.commands.quickstart_templates",
    "replay_filter_cmd": "bernstein.cli.commands.replay_filter_cmd",
    "report_cmd": "bernstein.cli.commands.report_cmd",
    "run_changelog_cmd": "bernstein.cli.commands.run_changelog_cmd",
    "self_update_cmd": "bernstein.cli.commands.self_update_cmd",
    "session_cmd": "bernstein.cli.commands.session_cmd",
    "slo_cmd": "bernstein.cli.commands.slo_cmd",
    "splash": "bernstein.cli.display.splash",
    "splash_assets": "bernstein.cli.display.splash_assets",
    "splash_screen": "bernstein.cli.display.splash_screen",
    "splash_v2": "bernstein.cli.display.splash_v2",
    "status_cmd": "bernstein.cli.commands.status_cmd",
    "stop_cmd": "bernstein.cli.commands.stop_cmd",
    "summary_card": "bernstein.cli.display.summary_card",
    "task_cmd": "bernstein.cli.commands.task_cmd",
    "templates_cmd": "bernstein.cli.commands.templates_cmd",
    "terminal_caps": "bernstein.cli.display.terminal_caps",
    "test_cmd": "bernstein.cli.commands.test_cmd",
    "text_effects": "bernstein.cli.display.text_effects",
    "tip_integration": "bernstein.cli.utils.tip_integration",
    "token_cmd": "bernstein.cli.commands.token_cmd",
    "triggers_cmd": "bernstein.cli.commands.triggers_cmd",
    "undo_cmd": "bernstein.cli.commands.undo_cmd",
    "users_cmd": "bernstein.cli.commands.users_cmd",
    "verbosity": "bernstein.cli.utils.verbosity",
    "verify_cmd": "bernstein.cli.commands.verify_cmd",
    "visual_theme": "bernstein.cli.display.visual_theme",
    "voice_cmd": "bernstein.cli.commands.voice_cmd",
    "voice_control": "bernstein.cli.utils.voice_control",
    "watch_cmd": "bernstein.cli.commands.watch_cmd",
    "worker_cmd": "bernstein.cli.commands.worker_cmd",
    "workflow_cmd": "bernstein.cli.commands.workflow_cmd",
    "workspace_cmd": "bernstein.cli.commands.workspace_cmd",
    "wrap_up_cmd": "bernstein.cli.commands.wrap_up_cmd",
}


class _CLIRedirectFinder(MetaPathFinder):
    """Redirect ``bernstein.cli.<old_name>`` to ``bernstein.cli.<subpkg>.<old_name>``."""

    _PREFIX = "bernstein.cli."

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> ModuleSpec | None:
        if not fullname.startswith(self._PREFIX):
            return None
        short = fullname[len(self._PREFIX) :]
        if "." in short:
            return None
        if short not in _CLI_REDIRECT_MAP:
            return None
        return ModuleSpec(fullname, _CLIRedirectLoader())


class _CLIRedirectLoader:
    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        fullname = module.__name__
        short = fullname[len(_CLIRedirectFinder._PREFIX) :]
        target_name = _CLI_REDIRECT_MAP[short]
        real = importlib.import_module(target_name)
        module.__dict__.update(real.__dict__)
        module.__path__ = getattr(real, "__path__", [])
        module.__file__ = getattr(real, "__file__", None)
        sys.modules[fullname] = real


if not any(isinstance(f, _CLIRedirectFinder) for f in sys.meta_path):
    sys.meta_path.append(_CLIRedirectFinder())
