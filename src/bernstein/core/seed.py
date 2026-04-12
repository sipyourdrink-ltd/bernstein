"""Seed file parser for bernstein.yaml.

Reads the project seed configuration, validates it, and produces the
initial manager Task that kicks off orchestration.

This module is a thin re-export shim.  The actual implementation lives in:
- ``seed_config.py`` — Dataclass/config definitions
- ``seed_parser.py`` — YAML parsing logic and helpers
- ``seed_validators.py`` — Task generation, feature gates, config snapshots
"""

from __future__ import annotations

# Re-export everything so existing ``from bernstein.core.seed import X``
# statements continue to work without changes.
# -- Config definitions (seed_config.py) --------------------------------------
from bernstein.core.seed_config import CORSConfig as CORSConfig
from bernstein.core.seed_config import DashboardAuthConfig as DashboardAuthConfig
from bernstein.core.seed_config import MetricSchema as MetricSchema
from bernstein.core.seed_config import ModelFallbackSeedConfig as ModelFallbackSeedConfig
from bernstein.core.seed_config import NetworkConfig as NetworkConfig
from bernstein.core.seed_config import NotifyConfig as NotifyConfig
from bernstein.core.seed_config import RateLimitBucketConfig as RateLimitBucketConfig
from bernstein.core.seed_config import RateLimitConfig as RateLimitConfig
from bernstein.core.seed_config import SeedConfig as SeedConfig
from bernstein.core.seed_config import SeedError as SeedError
from bernstein.core.seed_config import SessionConfig as SessionConfig
from bernstein.core.seed_config import StorageConfig as StorageConfig
from bernstein.core.seed_config import WebhookConfig as WebhookConfig

# -- Parser (seed_parser.py) --------------------------------------------------
from bernstein.core.seed_parser import _ALLOWED_WEBHOOK_EVENTS as _ALLOWED_WEBHOOK_EVENTS
from bernstein.core.seed_parser import _BUDGET_RE as _BUDGET_RE
from bernstein.core.seed_parser import _DEFAULT_RATE_LIMIT_PATHS as _DEFAULT_RATE_LIMIT_PATHS
from bernstein.core.seed_parser import _ENV_REF_RE as _ENV_REF_RE
from bernstein.core.seed_parser import _VALID_CLIS as _VALID_CLIS
from bernstein.core.seed_parser import _WEBHOOK_EVENT_ALIASES as _WEBHOOK_EVENT_ALIASES
from bernstein.core.seed_parser import _expand_env_value as _expand_env_value
from bernstein.core.seed_parser import _normalize_webhook_event as _normalize_webhook_event
from bernstein.core.seed_parser import _parse_bridge_settings as _parse_bridge_settings
from bernstein.core.seed_parser import _parse_budget as _parse_budget
from bernstein.core.seed_parser import _parse_cors_config as _parse_cors_config
from bernstein.core.seed_parser import _parse_dashboard_auth as _parse_dashboard_auth
from bernstein.core.seed_parser import _parse_metric_entry as _parse_metric_entry
from bernstein.core.seed_parser import _parse_metrics as _parse_metrics
from bernstein.core.seed_parser import _parse_model_fallback as _parse_model_fallback
from bernstein.core.seed_parser import _parse_network_config as _parse_network_config
from bernstein.core.seed_parser import _parse_openclaw_runtime_config as _parse_openclaw_runtime_config
from bernstein.core.seed_parser import _parse_rate_limit_bucket as _parse_rate_limit_bucket
from bernstein.core.seed_parser import _parse_rate_limit_config as _parse_rate_limit_config
from bernstein.core.seed_parser import _parse_role_model_policy as _parse_role_model_policy
from bernstein.core.seed_parser import _parse_smtp as _parse_smtp
from bernstein.core.seed_parser import _parse_string_list as _parse_string_list
from bernstein.core.seed_parser import _parse_team as _parse_team
from bernstein.core.seed_parser import _parse_tenants as _parse_tenants
from bernstein.core.seed_parser import parse_seed as parse_seed

# -- Validators (seed_validators.py) ------------------------------------------
from bernstein.core.seed_validators import ConfigSnapshot as ConfigSnapshot
from bernstein.core.seed_validators import FeatureGateEntry as FeatureGateEntry
from bernstein.core.seed_validators import FeatureGateRegistry as FeatureGateRegistry
from bernstein.core.seed_validators import _build_manager_description as _build_manager_description
from bernstein.core.seed_validators import build_config_snapshot as build_config_snapshot
from bernstein.core.seed_validators import load_feature_gate_override_file as load_feature_gate_override_file
from bernstein.core.seed_validators import seed_to_initial_task as seed_to_initial_task
