"""Route tasks to appropriate model and effort level with tier awareness.

Re-export shim: all symbols live in ``router_core`` and ``router_policies``.
Import from ``bernstein.core.router`` continues to work unchanged.
"""

from __future__ import annotations

# Re-export ModelConfig so that existing ``from bernstein.core.router import ModelConfig``
# statements keep working (ModelConfig originates in ``bernstein.core.models``).
from bernstein.core.models import ModelConfig as ModelConfig

# --- router_core exports ---
from bernstein.core.router_core import CostTracker as CostTracker
from bernstein.core.router_core import ProviderConfig as ProviderConfig
from bernstein.core.router_core import ProviderHealth as ProviderHealth
from bernstein.core.router_core import ProviderHealthStatus as ProviderHealthStatus
from bernstein.core.router_core import ResidencyAttestation as ResidencyAttestation
from bernstein.core.router_core import RouterError as RouterError
from bernstein.core.router_core import RouterState as RouterState
from bernstein.core.router_core import RoutingDecision as RoutingDecision
from bernstein.core.router_core import Tier as Tier
from bernstein.core.router_core import TierAwareRouter as TierAwareRouter
from bernstein.core.router_core import _default_router as _default_router
from bernstein.core.router_core import _select_model_config as _select_model_config
from bernstein.core.router_core import get_default_router as get_default_router
from bernstein.core.router_core import load_model_policy_from_yaml as load_model_policy_from_yaml
from bernstein.core.router_core import load_providers_from_yaml as load_providers_from_yaml
from bernstein.core.router_core import normalize_region as normalize_region
from bernstein.core.router_core import region_matches as region_matches
from bernstein.core.router_core import route_task as route_task

# --- router_policies exports ---
from bernstein.core.router_policies import AutoRouteDecision as AutoRouteDecision
from bernstein.core.router_policies import MaxTokensEscalation as MaxTokensEscalation
from bernstein.core.router_policies import ModelPolicy as ModelPolicy
from bernstein.core.router_policies import PolicyFilter as PolicyFilter
from bernstein.core.router_policies import TokenEscalationTracker as TokenEscalationTracker
from bernstein.core.router_policies import _escalation_tracker as _escalation_tracker
from bernstein.core.router_policies import _last_used_agent_index as _last_used_agent_index
from bernstein.core.router_policies import auto_route_task as auto_route_task
from bernstein.core.router_policies import (
    consider_cache_pricing_in_routing as consider_cache_pricing_in_routing,
)
from bernstein.core.router_policies import get_free_tier_providers as get_free_tier_providers
from bernstein.core.router_policies import select_round_robin_agent as select_round_robin_agent
from bernstein.core.router_policies import (
    select_with_free_tier_priority as select_with_free_tier_priority,
)
from bernstein.core.router_policies import (
    signal_max_tokens_escalation as signal_max_tokens_escalation,
)

# Backward-compatible aliases (previously module-private helpers)
_normalize_region = normalize_region
_region_matches = region_matches
