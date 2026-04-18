"""Core: task server, spawner, scheduler.

Sub-packages: orchestration, agents, tasks, quality, server, cost, tokens,
security, config, observability, protocols, git, persistence, planning,
routing, communication, knowledge, plugins_core.

Backward compatibility: ``from bernstein.core.<module> import X`` is
redirected to the correct sub-package automatically via a custom module
finder registered on ``sys.meta_path``.
"""

from __future__ import annotations

import importlib
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType  # noqa: TC003 - used at runtime by MetaPathFinder

# Map: old module name → new fully-qualified module path
_REDIRECT_MAP: dict[str, str] = {
    "a2a": "bernstein.core.protocols.a2a.a2a",
    "a2a_federation": "bernstein.core.protocols.a2a.a2a_federation",
    "ab_test": "bernstein.core.quality.ab_test",
    "ab_test_results": "bernstein.core.quality.ab_test_results",
    "abort_chain": "bernstein.core.tasks.abort_chain",
    "access_log": "bernstein.core.server.access_log",
    "acp": "bernstein.core.protocols.acp",
    "acp_ide_bridge": "bernstein.core.protocols.acp_ide_bridge",
    "activity_summary_poller": "bernstein.core.orchestration.activity_summary_poller",
    "adapter_autodetect": "bernstein.core.agents.adapter_autodetect",
    "adapter_health": "bernstein.core.agents.adapter_health",
    "adaptive_parallelism": "bernstein.core.orchestration.adaptive_parallelism",
    "adaptive_tick": "bernstein.core.orchestration.adaptive_tick",
    "agency_loader": "bernstein.core.plugins_core.agency_loader",
    "agent_cache": "bernstein.core.agents.agent_cache",
    "agent_cost_ledger": "bernstein.core.agents.agent_cost_ledger",
    "agent_discovery": "bernstein.core.agents.agent_discovery",
    "agent_identity": "bernstein.core.agents.agent_identity",
    "agent_ipc": "bernstein.core.agents.agent_ipc",
    "agent_lifecycle": "bernstein.core.agents.agent_lifecycle",
    "agent_log_aggregator": "bernstein.core.agents.agent_log_aggregator",
    "agent_profiling": "bernstein.core.agents.agent_profiling",
    "agent_reaping": "bernstein.core.agents.agent_reaping",
    "agent_recycling": "bernstein.core.agents.agent_recycling",
    "agent_session_token_breakdown": "bernstein.core.agents.agent_session_token_breakdown",
    "agent_signals": "bernstein.core.agents.agent_signals",
    "agent_state_refresh": "bernstein.core.agents.agent_state_refresh",
    "agent_trust": "bernstein.core.agents.agent_trust",
    "agent_turn_state": "bernstein.core.agents.agent_turn_state",
    "agent_utilization": "bernstein.core.agents.agent_utilization",
    # alert_rules: removed in audit-170 — dead code, no production importers.
    "always_allow": "bernstein.core.security.always_allow",
    "api_compat": "bernstein.core.server.api_compat",
    "api_compat_checker": "bernstein.core.server.api_compat_checker",
    "api_usage": "bernstein.core.cost.api_usage",
    # Icons moved from cli/display/icons.py to core/observability/icons.py (import-linter contract)
    "cli.display.icons": "bernstein.core.observability.icons",
    "apm_integration": "bernstein.core.observability.apm_integration",
    "approval": "bernstein.core.security.approval",
    "arch_conformance": "bernstein.core.quality.arch_conformance",
    # audit-177: new primary name for the AST-derived symbol graph.
    "ast_symbol_graph": "bernstein.core.knowledge.ast_symbol_graph",
    "audit": "bernstein.core.security.audit",
    "audit_export": "bernstein.core.security.audit_export",
    "audit_integrity": "bernstein.core.security.audit_integrity",
    "auth": "bernstein.core.security.auth",
    "auth_middleware": "bernstein.core.security.auth_middleware",
    "auth_rate_limiter": "bernstein.core.security.auth_rate_limiter",
    "auto_approve": "bernstein.core.security.auto_approve",
    "auto_distillation": "bernstein.core.tokens.auto_distillation",
    "auto_mode_classifier": "bernstein.core.routing.auto_mode_classifier",
    "backlog_parser": "bernstein.core.tasks.backlog_parser",
    "bandit_router": "bernstein.core.routing.bandit_router",
    "batch_api": "bernstein.core.tasks.batch_api",
    # batch_mode: removed in audit-026 — dead code, no production importers.
    "batch_router": "bernstein.core.tasks.batch_router",
    # batch_transitions: removed in audit-026 — dead code, no production importers.
    "behavior_anomaly": "bernstein.core.observability.behavior_anomaly",
    "benchmark_gate": "bernstein.core.quality.benchmark_gate",
    "blocking_hooks": "bernstein.core.security.blocking_hooks",
    "blue_green": "bernstein.core.orchestration.blue_green",
    "bootstrap": "bernstein.core.orchestration.bootstrap",
    "budget_actions": "bernstein.core.cost.budget_actions",
    "bulletin": "bernstein.core.communication.bulletin",
    "cache_token_tracker": "bernstein.core.tokens.cache_token_tracker",
    "canary_mode": "bernstein.core.orchestration.canary_mode",
    "capability_router": "bernstein.core.routing.capability_router",
    "capacity_wake": "bernstein.core.orchestration.capacity_wake",
    "cascade": "bernstein.core.routing.cascade",
    "cascade_router": "bernstein.core.routing.cascade_router",
    "cascading_failure_circuit_breaker": "bernstein.core.observability.cascading_failure_circuit_breaker",
    "cascading_token_counter": "bernstein.core.tokens.cascading_token_counter",
    "changelog": "bernstein.core.git.changelog",
    "cheaper_retry": "bernstein.core.cost.cheaper_retry",
    "checkpoint": "bernstein.core.persistence.checkpoint",
    "ci_fix": "bernstein.core.quality.ci_fix",
    "ci_log_parser": "bernstein.core.quality.ci_log_parser",
    "ci_monitor": "bernstein.core.quality.ci_monitor",
    "circuit_breaker": "bernstein.core.observability.circuit_breaker",
    "claude_agent_card": "bernstein.core.agents.claude_agent_card",
    "claude_cost_tracking": "bernstein.core.cost.claude_cost_tracking",
    "claude_max_turns": "bernstein.core.agents.claude_max_turns",
    "claude_message_normalizer": "bernstein.core.agents.claude_message_normalizer",
    "claude_model_prompts": "bernstein.core.agents.claude_model_prompts",
    "claude_permission_profiles": "bernstein.core.security.claude_permission_profiles",
    "claude_prompt_cache_optimizer": "bernstein.core.tokens.claude_prompt_cache_optimizer",
    "claude_session_resume": "bernstein.core.agents.claude_session_resume",
    "claude_tool_result_injection": "bernstein.core.security.claude_tool_result_injection",
    "cluster": "bernstein.core.protocols.cluster.cluster",
    "cluster_auth": "bernstein.core.protocols.cluster.cluster_auth",
    "cluster_autoscaler": "bernstein.core.protocols.cluster.cluster_autoscaler",
    "cluster_task_stealing": "bernstein.core.protocols.cluster.cluster_task_stealing",
    "command_allowlist": "bernstein.core.security.command_allowlist",
    "command_policy": "bernstein.core.security.command_policy",
    "comment_quality": "bernstein.core.quality.comment_quality",
    "commit_signing": "bernstein.core.security.commit_signing",
    "compaction_pipeline": "bernstein.core.tokens.compaction_pipeline",
    "completion_budget": "bernstein.core.cost.completion_budget",
    "completion_confidence": "bernstein.core.cost.completion_confidence",
    "complexity_advisor": "bernstein.core.quality.complexity_advisor",
    "compliance": "bernstein.core.security.compliance",
    "compliance_policies": "bernstein.core.security.compliance_policies",
    "compliance_report": "bernstein.core.security.compliance_report",
    "compression_models": "bernstein.core.tokens.compression_models",
    "config_diff": "bernstein.core.config.config_diff",
    "config_drift_cmd": "bernstein.core.config.config_drift_cmd",
    "config_path_validation": "bernstein.core.config.config_path_validation",
    "config_schema": "bernstein.core.config.config_schema",
    "config_watcher": "bernstein.core.config.config_watcher",
    "connection_pool": "bernstein.core.server.connection_pool",
    "container": "bernstein.core.agents.container",
    "context": "bernstein.core.tokens.context",
    "context_activation": "bernstein.core.tokens.context_activation",
    "context_collapse": "bernstein.core.tokens.context_collapse",
    "context_compression": "bernstein.core.tokens.context_compression",
    "context_degradation_detector": "bernstein.core.tokens.context_degradation_detector",
    "context_fallback": "bernstein.core.tokens.context_fallback",
    "context_inheritance": "bernstein.core.tokens.context_inheritance",
    "context_recommendations": "bernstein.core.tokens.context_recommendations",
    "context_window": "bernstein.core.tokens.context_window",
    "convergence_guard": "bernstein.core.orchestration.convergence_guard",
    "conversation_export": "bernstein.core.communication.conversation_export",
    "coordinator": "bernstein.core.orchestration.coordinator",
    "correlation": "bernstein.core.observability.correlation",
    "cost": "bernstein.core.cost.cost",
    "cost_anomaly": "bernstein.core.cost.cost_anomaly",
    "cost_arbitrage": "bernstein.core.cost.cost_arbitrage",
    "cost_autopilot": "bernstein.core.cost.cost_autopilot",
    "cost_comparison": "bernstein.core.cost.cost_comparison",
    "cost_estimation": "bernstein.core.cost.cost_estimation",
    "cost_forecast": "bernstein.core.cost.cost_forecast",
    "cost_history": "bernstein.core.cost.cost_history",
    "cost_per_line": "bernstein.core.cost.cost_per_line",
    "cost_root_cause": "bernstein.core.cost.cost_root_cause",
    "cost_tracker": "bernstein.core.cost.cost_tracker",
    "coverage_gate": "bernstein.core.quality.coverage_gate",
    "cross_agent_consistency": "bernstein.core.agents.cross_agent_consistency",
    "cross_model_verifier": "bernstein.core.quality.cross_model_verifier",
    "custom_metrics": "bernstein.core.observability.custom_metrics",
    # cycle_detector: removed in audit-192 — dead code, no production importers.
    "dashboard_auth": "bernstein.core.server.dashboard_auth",
    "data_residency": "bernstein.core.security.data_residency",
    "datadog_export": "bernstein.core.observability.datadog_export",
    "dead_code_detector": "bernstein.core.quality.dead_code_detector",
    "dead_letter_queue": "bernstein.core.tasks.dead_letter_queue",
    # degraded_mode: removed in audit-170 — dead code, no production importers.
    "denial_tracker": "bernstein.core.security.denial_tracker",
    "dep_impact": "bernstein.core.quality.dep_impact",
    "dep_validator": "bernstein.core.quality.dep_validator",
    "dependency_scan": "bernstein.core.quality.dependency_scan",
    "deterministic": "bernstein.core.orchestration.deterministic",
    "differential_privacy": "bernstein.core.security.differential_privacy",
    "difficulty_estimator": "bernstein.core.tasks.difficulty_estimator",
    "disaster_recovery": "bernstein.core.persistence.disaster_recovery",
    "dlp_scanner": "bernstein.core.security.dlp_scanner",
    "dlp_scanner_v2": "bernstein.core.security.dlp_scanner_v2",
    # doc_generator: removed in audit-169 — dead code, no production importers.
    "dp_telemetry": "bernstein.core.security.dp_telemetry",
    "drain": "bernstein.core.orchestration.drain",
    "drain_merge": "bernstein.core.orchestration.drain_merge",
    "dual_approval": "bernstein.core.security.dual_approval",
    # duplicate_detector: removed in audit-192 — dead code, no production importers.
    "duration_predictor": "bernstein.core.planning.duration_predictor",
    "education_tier": "bernstein.core.cost.education_tier",
    "effectiveness": "bernstein.core.quality.effectiveness",
    # embedding_scorer: removed in audit-169 — dead code, no production importers.
    # error_classifier: removed in audit-170 — dead code, no production importers.
    "eu_ai_act": "bernstein.core.security.eu_ai_act",
    "evolution": "bernstein.core.orchestration.evolution",
    "external_policy_hook": "bernstein.core.security.external_policy_hook",
    "fast_mode": "bernstein.core.routing.fast_mode",
    "fast_path": "bernstein.core.quality.fast_path",
    "file_discovery": "bernstein.core.knowledge.file_discovery",
    "file_health": "bernstein.core.persistence.file_health",
    "file_locks": "bernstein.core.persistence.file_locks",
    "flaky_detector": "bernstein.core.quality.flaky_detector",
    "formal_verification": "bernstein.core.quality.formal_verification",
    "frame_headers": "bernstein.core.server.frame_headers",
    "free_tier": "bernstein.core.cost.free_tier",
    # gate_cache: removed in audit-192 — dead mixin, no production importers.
    "gate_commands": "bernstein.core.quality.gate_commands",
    "gate_pipeline": "bernstein.core.quality.gate_pipeline",
    "gate_plugins": "bernstein.core.quality.gate_plugins",
    "gate_runner": "bernstein.core.quality.gate_runner",
    "git_basic": "bernstein.core.git.git_basic",
    "git_context": "bernstein.core.git.git_context",
    "git_hooks": "bernstein.core.git.git_hooks",
    "git_hygiene": "bernstein.core.git.git_hygiene",
    "git_ops": "bernstein.core.git.git_ops",
    "git_pr": "bernstein.core.git.git_pr",
    "github": "bernstein.core.git.github",
    # graduated_memory_guard: removed in audit-169 — dead code, no production importers.
    "graduation": "bernstein.core.quality.graduation",
    "grafana_dashboard": "bernstein.core.observability.grafana_dashboard",
    # audit-177: graph.py -> task_graph.py. Shim kept for back-compat callers.
    "graph": "bernstein.core.knowledge.task_graph",
    "grpc_client": "bernstein.core.protocols.grpc.grpc_client",
    "grpc_server": "bernstein.core.protocols.grpc.grpc_server",
    "guardrails": "bernstein.core.security.guardrails",
    # health_score: removed in audit-170 — dead code, no production importers.
    "heartbeat": "bernstein.core.agents.heartbeat",
    "heartbeat_escalation": "bernstein.core.agents.heartbeat_escalation",
    "hijacker": "bernstein.core.routing.hijacker",
    "hipaa": "bernstein.core.security.hipaa",
    "home": "bernstein.core.config.home",
    "hook_events": "bernstein.core.config.hook_events",
    "hook_protocol": "bernstein.core.config.hook_protocol",
    "hook_templates": "bernstein.core.config.hook_templates",
    "hooks_receiver": "bernstein.core.server.hooks_receiver",
    "http_retry": "bernstein.core.server.http_retry",
    "idempotent_merge": "bernstein.core.git.idempotent_merge",
    "idle_detection": "bernstein.core.agents.idle_detection",
    "in_process_agent": "bernstein.core.agents.in_process_agent",
    "incident": "bernstein.core.observability.incident",
    "incident_timeline": "bernstein.core.observability.incident_timeline",
    "incremental_merge": "bernstein.core.git.incremental_merge",
    "integration_test_gen": "bernstein.core.quality.integration_test_gen",
    "ip_allowlist": "bernstein.core.security.ip_allowlist",
    "janitor": "bernstein.core.quality.janitor",
    "jira_sync": "bernstein.core.git.jira_sync",
    "json_logging": "bernstein.core.server.json_logging",
    "jwt_tokens": "bernstein.core.security.jwt_tokens",
    "key_rotation": "bernstein.core.security.key_rotation",
    "key_rotation_support": "bernstein.core.security.key_rotation_support",
    "knowledge_base": "bernstein.core.knowledge.knowledge_base",
    "knowledge_graph": "bernstein.core.knowledge.knowledge_graph",
    "lessons": "bernstein.core.knowledge.lessons",
    "license_manager": "bernstein.core.security.license_manager",
    "license_scanner": "bernstein.core.security.license_scanner",
    "lifecycle": "bernstein.core.tasks.lifecycle",
    "llm": "bernstein.core.routing.llm",
    "load_scaler": "bernstein.core.orchestration.load_scaler",
    "log_redact": "bernstein.core.observability.log_redact",
    "log_search": "bernstein.core.observability.log_search",
    "loop_detector": "bernstein.core.observability.loop_detector",
    # mailbox: removed in audit-176 — dead code, superseded by bulletin.py +
    # signals.py; no production importers.
    "manager": "bernstein.core.orchestration.manager",
    "manager_models": "bernstein.core.orchestration.manager_models",
    "manager_parsing": "bernstein.core.orchestration.manager_parsing",
    "manager_prompts": "bernstein.core.orchestration.manager_prompts",
    "manifest": "bernstein.core.config.manifest",
    "mcp_auth_lifecycle": "bernstein.core.protocols.mcp.mcp_auth_lifecycle",
    "mcp_client": "bernstein.core.protocols.mcp.mcp_client",
    "mcp_composition": "bernstein.core.protocols.mcp.mcp_composition",
    "mcp_config_validator": "bernstein.core.protocols.mcp.mcp_config_validator",
    "mcp_elicitation": "bernstein.core.protocols.mcp.mcp_elicitation",
    "mcp_gateway": "bernstein.core.protocols.mcp.mcp_gateway",
    "mcp_health_monitor": "bernstein.core.protocols.mcp.mcp_health_monitor",
    "mcp_lazy_discovery": "bernstein.core.protocols.mcp.mcp_lazy_discovery",
    "mcp_manager": "bernstein.core.protocols.mcp.mcp_manager",
    "mcp_marketplace": "bernstein.core.protocols.mcp.mcp_marketplace",
    "mcp_metrics": "bernstein.core.protocols.mcp.mcp_metrics",
    "mcp_protocol_test": "bernstein.core.protocols.mcp.mcp_protocol_test",
    "mcp_readiness": "bernstein.core.protocols.mcp.mcp_readiness",
    "mcp_registry": "bernstein.core.protocols.mcp.mcp_registry",
    "mcp_resource_cache": "bernstein.core.protocols.mcp.mcp_resource_cache",
    "mcp_sandbox": "bernstein.core.protocols.mcp.mcp_sandbox",
    "mcp_server": "bernstein.core.protocols.mcp.mcp_server",
    "mcp_skill_bridge": "bernstein.core.protocols.mcp.mcp_skill_bridge",
    "mcp_skill_registry": "bernstein.core.protocols.mcp.mcp_skill_registry",
    "mcp_task_filter": "bernstein.core.protocols.mcp.mcp_task_filter",
    "mcp_tool_normalization": "bernstein.core.protocols.mcp.mcp_tool_normalization",
    "mcp_transport": "bernstein.core.protocols.mcp.mcp_transport",
    "mcp_usage_analytics": "bernstein.core.protocols.mcp.mcp_usage_analytics",
    "mcp_version_compat": "bernstein.core.protocols.mcp.mcp_version_compat",
    # audit-191: back-compat shims for old ``bernstein.core.protocols.
    # <mcp_*|a2a_*|cluster_*|grpc_*>`` import paths. Modules now live in
    # subpackages (protocols.mcp.mcp_foo etc.) but external plugins and
    # test files may import from the old top-level path. These dotted
    # keys are matched by ``_CoreRedirectFinder`` when the short form is
    # ``protocols.mcp_manager`` etc.
    "protocols.a2a_federation": "bernstein.core.protocols.a2a.a2a_federation",
    "protocols.cluster_auth": "bernstein.core.protocols.cluster.cluster_auth",
    "protocols.cluster_autoscaler": "bernstein.core.protocols.cluster.cluster_autoscaler",
    "protocols.cluster_task_stealing": "bernstein.core.protocols.cluster.cluster_task_stealing",
    "protocols.grpc_client": "bernstein.core.protocols.grpc.grpc_client",
    "protocols.grpc_server": "bernstein.core.protocols.grpc.grpc_server",
    "protocols.mcp_auth_lifecycle": "bernstein.core.protocols.mcp.mcp_auth_lifecycle",
    "protocols.mcp_client": "bernstein.core.protocols.mcp.mcp_client",
    "protocols.mcp_composition": "bernstein.core.protocols.mcp.mcp_composition",
    "protocols.mcp_config_validator": "bernstein.core.protocols.mcp.mcp_config_validator",
    "protocols.mcp_elicitation": "bernstein.core.protocols.mcp.mcp_elicitation",
    "protocols.mcp_gateway": "bernstein.core.protocols.mcp.mcp_gateway",
    "protocols.mcp_health_monitor": "bernstein.core.protocols.mcp.mcp_health_monitor",
    "protocols.mcp_lazy_discovery": "bernstein.core.protocols.mcp.mcp_lazy_discovery",
    "protocols.mcp_manager": "bernstein.core.protocols.mcp.mcp_manager",
    "protocols.mcp_marketplace": "bernstein.core.protocols.mcp.mcp_marketplace",
    "protocols.mcp_metrics": "bernstein.core.protocols.mcp.mcp_metrics",
    "protocols.mcp_protocol_test": "bernstein.core.protocols.mcp.mcp_protocol_test",
    "protocols.mcp_readiness": "bernstein.core.protocols.mcp.mcp_readiness",
    "protocols.mcp_registry": "bernstein.core.protocols.mcp.mcp_registry",
    "protocols.mcp_resource_cache": "bernstein.core.protocols.mcp.mcp_resource_cache",
    "protocols.mcp_sandbox": "bernstein.core.protocols.mcp.mcp_sandbox",
    "protocols.mcp_server": "bernstein.core.protocols.mcp.mcp_server",
    "protocols.mcp_skill_bridge": "bernstein.core.protocols.mcp.mcp_skill_bridge",
    "protocols.mcp_skill_registry": "bernstein.core.protocols.mcp.mcp_skill_registry",
    "protocols.mcp_task_filter": "bernstein.core.protocols.mcp.mcp_task_filter",
    "protocols.mcp_tool_normalization": "bernstein.core.protocols.mcp.mcp_tool_normalization",
    "protocols.mcp_transport": "bernstein.core.protocols.mcp.mcp_transport",
    "protocols.mcp_usage_analytics": "bernstein.core.protocols.mcp.mcp_usage_analytics",
    "protocols.mcp_version_compat": "bernstein.core.protocols.mcp.mcp_version_compat",
    # memory_extractor: removed in audit-169 — dead code, no production importers.
    "memory_guard": "bernstein.core.knowledge.memory_guard",
    "memory_integrity": "bernstein.core.knowledge.memory_integrity",
    "memory_lock_protocol": "bernstein.core.knowledge.memory_lock_protocol",
    # memory_sanitizer: removed in audit-169 — dead code, no production importers.
    "merge_queue": "bernstein.core.git.merge_queue",
    "merkle": "bernstein.core.persistence.merkle",
    "metric_collector": "bernstein.core.observability.metric_collector",
    "metric_export": "bernstein.core.observability.metric_export",
    "metrics": "bernstein.core.observability.metrics",
    "model_fallback": "bernstein.core.routing.model_fallback",
    "model_recommender": "bernstein.core.routing.model_recommender",
    "model_routing": "bernstein.core.routing.model_routing",
    "models": "bernstein.core.tasks.models",
    "multi_cell": "bernstein.core.orchestration.multi_cell",
    "network_isolation": "bernstein.core.security.network_isolation",
    "notifications": "bernstein.core.communication.notifications",
    "nudge_manager": "bernstein.core.orchestration.nudge_manager",
    "oauth_pkce": "bernstein.core.security.oauth_pkce",
    "operator": "bernstein.core.orchestration.operator",
    "orchestrator": "bernstein.core.orchestration.orchestrator",
    "orchestrator_backlog": "bernstein.core.orchestration.orchestrator_backlog",
    "orchestrator_cleanup": "bernstein.core.orchestration.orchestrator_cleanup",
    "orchestrator_config": "bernstein.core.orchestration.orchestrator_config",
    "orchestrator_evolve": "bernstein.core.orchestration.orchestrator_evolve",
    "orchestrator_health": "bernstein.core.orchestration.orchestrator_health",
    "orchestrator_run": "bernstein.core.orchestration.orchestrator_run",
    "orchestrator_summary": "bernstein.core.orchestration.orchestrator_summary",
    "orphan_tool_result": "bernstein.core.agents.orphan_tool_result",
    "outcome_pricing": "bernstein.core.cost.outcome_pricing",
    "output_fingerprint": "bernstein.core.quality.output_fingerprint",
    # output_normalizer: removed in audit-192 — dead code, no production importers.
    "permission_delegation": "bernstein.core.security.permission_delegation",
    "permission_graph": "bernstein.core.security.permission_graph",
    "permission_matrix": "bernstein.core.security.permission_matrix",
    "permission_mode": "bernstein.core.security.permission_mode",
    "permission_rules": "bernstein.core.security.permission_rules",
    "permissions": "bernstein.core.security.permissions",
    "pii_output_gate": "bernstein.core.security.pii_output_gate",
    "plan_approval": "bernstein.core.security.plan_approval",
    "plan_builder": "bernstein.core.planning.plan_builder",
    "plan_loader": "bernstein.core.planning.plan_loader",
    "plan_schema": "bernstein.core.planning.plan_schema",
    "planner": "bernstein.core.planning.planner",
    "platform_compat": "bernstein.core.config.platform_compat",
    "plugin_installer": "bernstein.core.plugins_core.plugin_installer",
    "plugin_manifest": "bernstein.core.plugins_core.plugin_manifest",
    "plugin_policy": "bernstein.core.security.plugin_policy",
    "plugin_reconciler": "bernstein.core.plugins_core.plugin_reconciler",
    "policy": "bernstein.core.security.policy",
    "policy_engine": "bernstein.core.security.policy_engine",
    "policy_limits": "bernstein.core.security.policy_limits",
    "policy_templates": "bernstein.core.security.policy_templates",
    "poll_config": "bernstein.core.config.poll_config",
    "post_tool_enforcement": "bernstein.core.security.post_tool_enforcement",
    "postmortem": "bernstein.core.observability.postmortem",
    "pr_size_governor": "bernstein.core.git.pr_size_governor",
    "predictive_alerts": "bernstein.core.observability.predictive_alerts",
    "predictive_cost_model": "bernstein.core.cost.predictive_cost_model",
    "preflight": "bernstein.core.orchestration.preflight",
    "priority_aging": "bernstein.core.tasks.priority_aging",
    "process_utils": "bernstein.core.orchestration.process_utils",
    "profiler": "bernstein.core.observability.profiler",
    "prometheus": "bernstein.core.observability.prometheus",
    "prompt_caching": "bernstein.core.tokens.prompt_caching",
    "prompt_injection": "bernstein.core.tokens.prompt_injection",
    "prompt_precheck": "bernstein.core.tokens.prompt_precheck",
    "prompt_token_analysis": "bernstein.core.tokens.prompt_token_analysis",
    "prompt_versioning": "bernstein.core.tokens.prompt_versioning",
    "provider_circuit_breaker": "bernstein.core.observability.provider_circuit_breaker",
    "provider_latency": "bernstein.core.observability.provider_latency",
    "quality_gate_coalescer": "bernstein.core.quality.quality_gate_coalescer",
    "quality_gates": "bernstein.core.quality.quality_gates",
    "quality_score": "bernstein.core.quality.quality_score",
    "quarantine": "bernstein.core.security.quarantine",
    "query_throttle": "bernstein.core.protocols.query_throttle",
    "quota_poller": "bernstein.core.protocols.quota_poller",
    "quota_probe": "bernstein.core.protocols.quota_probe",
    "rag": "bernstein.core.knowledge.rag",
    "rate_limit_tracker": "bernstein.core.observability.rate_limit_tracker",
    # rate_limited_logger: removed in audit-170 — dead code, no production importers.
    "rbac": "bernstein.core.security.rbac",
    "readme_reminder": "bernstein.core.quality.readme_reminder",
    "recorder": "bernstein.core.persistence.recorder",
    # repo_index: removed in audit-169 — dead code, no production importers.
    "request_dedup": "bernstein.core.server.request_dedup",
    "request_logging": "bernstein.core.server.request_logging",
    "researcher": "bernstein.core.knowledge.researcher",
    "resource_limits": "bernstein.core.security.resource_limits",
    "retrospective": "bernstein.core.quality.retrospective",
    "retry_budget": "bernstein.core.cost.retry_budget",
    "review_rubric": "bernstein.core.quality.review_rubric",
    # reviewer: removed in audit-192 — dead code, no production importers.
    "roadmap_runtime": "bernstein.core.planning.roadmap_runtime",
    "role_classifier": "bernstein.core.routing.role_classifier",
    "rolling_restart": "bernstein.core.orchestration.rolling_restart",
    "route_decision": "bernstein.core.routing.route_decision",
    "router": "bernstein.core.routing.router",
    "router_core": "bernstein.core.routing.router_core",
    "router_policies": "bernstein.core.routing.router_policies",
    "rule_enforcer": "bernstein.core.security.rule_enforcer",
    "run_changelog": "bernstein.core.orchestration.run_changelog",
    "run_report": "bernstein.core.orchestration.run_report",
    "run_session": "bernstein.core.orchestration.run_session",
    "runbooks": "bernstein.core.observability.runbooks",
    "runtime_state": "bernstein.core.persistence.runtime_state",
    "sandbox": "bernstein.core.security.sandbox",
    "sandbox_escape_detector": "bernstein.core.security.sandbox_escape_detector",
    "sandbox_eval": "bernstein.core.security.sandbox_eval",
    "sanitize": "bernstein.core.security.sanitize",
    "sbom": "bernstein.core.security.sbom",
    "scenario_library": "bernstein.core.planning.scenario_library",
    # scratchpad: removed in audit-176 — dead code, cross-worker state
    # superseded by bulletin.py + signals.py; no production importers.
    "sdk_generator": "bernstein.core.plugins_core.sdk_generator",
    "seccomp_profiles": "bernstein.core.security.seccomp_profiles",
    "secrets": "bernstein.core.security.secrets",
    "section_dedup": "bernstein.core.quality.section_dedup",
    "security_correlation": "bernstein.core.security.security_correlation",
    "security_incident_response": "bernstein.core.security.security_incident_response",
    "security_posture": "bernstein.core.security.security_posture",
    "seed": "bernstein.core.config.seed",
    "seed_config": "bernstein.core.config.seed_config",
    "seed_parser": "bernstein.core.config.seed_parser",
    "semantic_cache": "bernstein.core.knowledge.semantic_cache",
    # audit-177: semantic_graph.py -> ast_symbol_graph.py. Shim kept for back-compat.
    "semantic_graph": "bernstein.core.knowledge.ast_symbol_graph",
    "sensitive_data": "bernstein.core.security.sensitive_data",
    "sensitive_file_detector": "bernstein.core.security.sensitive_file_detector",
    "server_app": "bernstein.core.server.server_app",
    "server_launch": "bernstein.core.server.server_launch",
    "server_middleware": "bernstein.core.server.server_middleware",
    "server_models": "bernstein.core.server.server_models",
    "server_supervisor": "bernstein.core.server.server_supervisor",
    "session": "bernstein.core.persistence.session",
    # session_checkpoint: removed in audit-084 — SessionCheckpoint had zero
    # production callers; CheckpointState (now aliased to
    # checkpoint.PartialState) is the operator-visible progress slice.
    "session_continuity": "bernstein.core.persistence.session_continuity",
    "shutdown_sequence": "bernstein.core.orchestration.shutdown_sequence",
    "signals": "bernstein.core.communication.signals",
    "sigstore_attestation": "bernstein.core.security.sigstore_attestation",
    "skill_badges": "bernstein.core.plugins_core.skill_badges",
    "skill_discovery": "bernstein.core.plugins_core.skill_discovery",
    "skill_md": "bernstein.core.plugins_core.skill_md",
    # sla_monitor: removed in audit-170 — dead code, no production importers.
    "slo": "bernstein.core.observability.slo",
    "soc2_report": "bernstein.core.security.soc2_report",
    "spawn_analyzer": "bernstein.core.agents.spawn_analyzer",
    "spawn_dry_run": "bernstein.core.agents.spawn_dry_run",
    "spawn_errors": "bernstein.core.agents.spawn_errors",
    "spawn_prompt": "bernstein.core.agents.spawn_prompt",
    "spawn_rate_limiter": "bernstein.core.agents.spawn_rate_limiter",
    "spawner": "bernstein.core.agents.spawner",
    "spawner_core": "bernstein.core.agents.spawner_core",
    "spawner_merge": "bernstein.core.agents.spawner_merge",
    "spawner_warm_pool": "bernstein.core.agents.spawner_warm_pool",
    "spawner_worktree": "bernstein.core.agents.spawner_worktree",
    "spend_forecast": "bernstein.core.cost.spend_forecast",
    "ssh_backend": "bernstein.core.protocols.ssh_backend",
    "sso_oidc": "bernstein.core.security.sso_oidc",
    # stack_detector: removed in audit-170 — dead code, no production importers.
    "staggered_shutdown": "bernstein.core.orchestration.staggered_shutdown",
    # startup_selftest: removed in audit-170 — dead code, no production importers.
    "state_encryption": "bernstein.core.security.state_encryption",
    "store": "bernstein.core.persistence.store",
    "store_factory": "bernstein.core.persistence.store_factory",
    "store_postgres": "bernstein.core.persistence.store_postgres",
    "store_redis": "bernstein.core.persistence.store_redis",
    "sync": "bernstein.core.persistence.sync",
    # synthesis: removed in audit-169 — dead code, no production importers.
    "task_claim": "bernstein.core.tasks.task_claim",
    # task_completion: removed in audit-018 — collect_completion_data lives in
    # bernstein.core.tasks.task_lifecycle; no shim is provided to keep the
    # duplicate implementation from re-appearing.
    # task_diff_preview: removed in audit-026 — dead code, no production importers.
    # task_event_store: removed in audit-026 — dead code, no production importers.
    # audit-177: new primary name for the task-dependency DAG.
    "task_graph": "bernstein.core.knowledge.task_graph",
    "task_grouping": "bernstein.core.tasks.task_grouping",
    "task_lifecycle": "bernstein.core.tasks.task_lifecycle",
    "task_retry": "bernstein.core.tasks.task_retry",
    "task_spawn_bridge": "bernstein.core.tasks.task_spawn_bridge",
    "task_splitter": "bernstein.core.tasks.task_splitter",
    # task_status_history: removed in audit-026 — dead code, no production importers.
    "task_store": "bernstein.core.tasks.task_store",
    "task_store_core": "bernstein.core.tasks.task_store_core",
    # task_tagging: removed in audit-026 — dead code, no production importers.
    # task_templates: removed in audit-026 — dead code, no production importers.
    "task_tools": "bernstein.core.tasks.task_tools",
    "team_state": "bernstein.core.persistence.team_state",
    "telemetry": "bernstein.core.observability.telemetry",
    "tenant_isolation": "bernstein.core.security.tenant_isolation",
    "tenant_rate_limiter": "bernstein.core.security.tenant_rate_limiter",
    "tenanting": "bernstein.core.security.tenanting",
    # test_data_gen: removed in audit-192 — dead code, no production importers.
    # test_expansion: removed in audit-192 — dead code, no production importers.
    "test_impact": "bernstein.core.quality.test_impact",
    "tick_anomaly": "bernstein.core.orchestration.tick_anomaly",
    "tick_budget": "bernstein.core.orchestration.tick_budget",
    "tick_hooks": "bernstein.core.orchestration.tick_hooks",
    "tick_metrics": "bernstein.core.orchestration.tick_metrics",
    "tick_pipeline": "bernstein.core.orchestration.tick_pipeline",
    "tick_telemetry": "bernstein.core.orchestration.tick_telemetry",
    "token_analyzer": "bernstein.core.tokens.token_analyzer",
    "token_binding": "bernstein.core.tokens.token_binding",
    "token_counter": "bernstein.core.tokens.token_counter",
    "token_estimation": "bernstein.core.tokens.token_estimation",
    "token_monitor": "bernstein.core.tokens.token_monitor",
    "token_waste_report": "bernstein.core.tokens.token_waste_report",
    # tool_timing: removed in audit-170 — dead code, no production importers.
    "tool_use_context": "bernstein.core.agents.tool_use_context",
    # trace_correlation: removed in audit-170 — dead code, no production importers.
    "traces": "bernstein.core.observability.traces",
    "trigger_manager": "bernstein.core.orchestration.trigger_manager",
    "upgrade_executor": "bernstein.core.config.upgrade_executor",
    # usage_telemetry: removed in audit-170 — dead code, no production importers.
    "vault_injector": "bernstein.core.security.vault_injector",
    "verification_nudge": "bernstein.core.quality.verification_nudge",
    "vertical_packs": "bernstein.core.plugins_core.vertical_packs",
    "view_mode": "bernstein.core.config.view_mode",
    "visual_config": "bernstein.core.config.visual_config",
    "voting": "bernstein.core.communication.voting",
    "wal": "bernstein.core.persistence.wal",
    "wal_replay": "bernstein.core.persistence.wal_replay",
    "wal_replication": "bernstein.core.persistence.wal_replication",
    "warm_pool": "bernstein.core.agents.warm_pool",
    "watchdog": "bernstein.core.observability.watchdog",
    # web_graph: removed in audit-169 — dead code, no production importers.
    "webhook_handler": "bernstein.core.server.webhook_handler",
    "webhook_signatures": "bernstein.core.server.webhook_signatures",
    "webhook_verify": "bernstein.core.server.webhook_verify",
    "worker": "bernstein.core.orchestration.worker",
    "workflow": "bernstein.core.planning.workflow",
    "workflow_dsl": "bernstein.core.planning.workflow_dsl",
    "workflow_importer": "bernstein.core.planning.workflow_importer",
    "workload_prediction": "bernstein.core.orchestration.workload_prediction",
    "workspace": "bernstein.core.persistence.workspace",
    "worktree": "bernstein.core.git.worktree",
    "worktree_claude_md": "bernstein.core.git.worktree_claude_md",
    "worktree_isolation": "bernstein.core.git.worktree_isolation",
    "zombie_cleanup": "bernstein.core.agents.zombie_cleanup",
}


class _CoreRedirectFinder(MetaPathFinder):
    """Redirect ``bernstein.core.<old_name>`` to ``bernstein.core.<subpkg>.<old_name>``.

    Most keys in ``_REDIRECT_MAP`` are direct children of ``bernstein.core``
    (e.g. ``mcp_manager`` → ``protocols.mcp.mcp_manager``). audit-191 added
    dotted keys (``protocols.mcp_manager``) so callers that still import via
    the old ``bernstein.core.protocols.<mcp_*|a2a_*|cluster_*|grpc_*>`` paths
    after the subpackage split keep working. Dotted keys match submodule
    imports (``bernstein.core.<subpkg>.<oldname>``).
    """

    _PREFIX = "bernstein.core."

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> ModuleSpec | None:
        """Return a ModuleSpec that loads via our redirect loader."""
        if not fullname.startswith(self._PREFIX):
            return None
        short = fullname[len(self._PREFIX) :]
        if short not in _REDIRECT_MAP:
            return None
        return ModuleSpec(fullname, _CoreRedirectLoader())


class _CoreRedirectLoader:
    """Load a redirected module by importing the real target."""

    def create_module(self, _spec: ModuleSpec) -> ModuleType | None:
        """Let the real import create the module."""
        return None  # use default semantics

    def exec_module(self, module: ModuleType) -> None:
        """Replace the module object with the real target."""
        fullname = module.__name__
        short = fullname[len(_CoreRedirectFinder._PREFIX) :]
        target_name = _REDIRECT_MAP[short]
        real = importlib.import_module(target_name)
        # Copy all attributes from real module
        module.__dict__.update(real.__dict__)
        module.__path__ = getattr(real, "__path__", [])
        module.__file__ = getattr(real, "__file__", None)
        module.__loader__ = getattr(real, "__loader__", None)
        # Also register real module under old name for subsequent imports
        sys.modules[fullname] = real

    def get_code(self, _fullname: str) -> None:
        """Return None — redirect loaders don't provide code objects."""
        return None


# Register the finder once at import time
if not any(isinstance(f, _CoreRedirectFinder) for f in sys.meta_path):
    sys.meta_path.append(_CoreRedirectFinder())
