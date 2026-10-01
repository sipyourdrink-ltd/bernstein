"""Spawner environment construction for agent subprocesses and unattended doctor probes.

Issue #5441: extract the spawner's environment construction into one function
shared between the agent spawning path (host, container, sandbox) and doctor /
preflight probes when running unattended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.credential_scoping import AgentCredentialPolicy
    from bernstein.core.secrets import SecretsConfig

__all__ = ["build_spawner_env"]

# Default provider credential variables per adapter family.
_ADAPTER_CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "qwen": (
        "OPENROUTER_API_KEY_PAID",
        "OPENROUTER_API_KEY_FREE",
        "OPENAI_API_KEY",
        "TOGETHERAI_USER_KEY",
    ),
    "aider": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"),
}


def build_spawner_env(
    adapter_name: str | None = None,
    extra_keys: Iterable[str] = (),
    *,
    base_env: Mapping[str, str] | None = None,
    secrets_config: SecretsConfig | None = None,
    agent_id: str | None = None,
    role: str | None = None,
    credential_policy: AgentCredentialPolicy | None = None,
    inherit_orchestrator_pythonpath: bool = False,
    path_override: str | None = None,
) -> dict[str, str]:
    """Construct the execution environment matching orchestrator agent spawns.

    Args:
        adapter_name: Optional adapter name (e.g. ``"claude"``, ``"codex"``).
            If supplied, credential keys specific to that adapter family are
            included. If ``None``, credential keys for all known adapters are
            included.
        extra_keys: Additional environment variable names to pass through.
        base_env: Source environment mapping (defaults to ``os.environ``).
        secrets_config: External secrets provider configuration.
        agent_id: Optional agent identifier for credential scoping.
        role: Optional role name for credential scoping fallback.
        credential_policy: Scoping policy override.
        inherit_orchestrator_pythonpath: Whether to inject orchestrator sys.path.
        path_override: Optional explicit PATH value (e.g. for testing unattended PATH).

    Returns:
        Filtered environment dictionary safe for spawned agents and probes.
    """
    keys: list[str] = list(extra_keys)
    if adapter_name is not None:
        name = adapter_name.lower()
        matched = False
        for prefix, cred_keys in _ADAPTER_CREDENTIAL_KEYS.items():
            if prefix in name:
                keys.extend(cred_keys)
                matched = True
        if not matched:
            keys.extend(["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"])
    else:
        for cred_keys in _ADAPTER_CREDENTIAL_KEYS.values():
            keys.extend(cred_keys)

    env = build_filtered_env(
        extra_keys=keys,
        base_env=base_env,
        secrets_config=secrets_config,
        agent_id=agent_id,
        role=role,
        credential_policy=credential_policy,
        inherit_orchestrator_pythonpath=inherit_orchestrator_pythonpath,
    )

    if path_override is not None:
        env["PATH"] = path_override

    return env
