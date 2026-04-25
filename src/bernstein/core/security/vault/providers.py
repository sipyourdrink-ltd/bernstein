"""Provider registry for the credential vault.

Each :class:`Provider` declares:

* The vault id used as the keychain account.
* Human-friendly display name.
* The legacy environment variable(s) that fed the provider before the
  vault landed (so :func:`bernstein.core.security.vault.resolver.resolve_secret`
  can warn-and-forward when a user hasn't migrated yet).
* The ``token-paste`` UX (label + masking instructions), or OAuth metadata.
* A ``whoami`` endpoint that converts a candidate secret into an account
  label and validates the secret in one round-trip.
* An optional ``revoke`` endpoint hit when ``bernstein creds revoke`` runs.

Providers are intentionally registered in code (not config) for v1.9: the
universe is small, custom registration buys little, and a static registry
keeps `bernstein creds list` deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

#: Stable provider identifier used as the keychain account name. Adding a
#: new provider means appending here and wiring an entry in :data:`_PROVIDERS`.
ProviderId = Literal["github", "linear", "jira", "slack", "telegram"]


class AuthMode(StrEnum):
    """How the user supplies the credential during ``bernstein connect``."""

    TOKEN_PASTE = "token-paste"
    OAUTH_DEVICE_CODE = "oauth-device-code"
    OAUTH_PKCE = "oauth-pkce"


@dataclass(frozen=True)
class TokenPastePrompt:
    """Prompt configuration for a single token-paste field."""

    field: str  # e.g. "token" or "email"
    label: str  # e.g. "GitHub personal access token"
    is_secret: bool = True


@dataclass(frozen=True)
class WhoamiSpec:
    """How to validate a secret and derive an account label.

    ``url_template`` may reference fields from the paste prompts (e.g.
    ``{base_url}/rest/api/3/myself`` for Jira). For OAuth flows the
    template can reference ``{access_token}`` directly.
    """

    url_template: str
    auth_header_template: str
    account_field: tuple[str, ...]  # JSON path: ("login",) → data["login"]
    success_status: int = 200


@dataclass(frozen=True)
class RevokeSpec:
    """How to call the provider's secret-revoke endpoint."""

    url_template: str
    method: str = "DELETE"
    auth_header_template: str = ""
    success_statuses: tuple[int, ...] = (200, 204)


@dataclass(frozen=True)
class OAuthDeviceCodeSpec:
    """Linear-style device-code flow metadata."""

    device_code_endpoint: str
    token_endpoint: str
    client_id: str
    scope: str = ""


@dataclass(frozen=True)
class ProviderConfig:
    """Per-provider configuration consumed by the CLI and resolver."""

    id: ProviderId
    display_name: str
    legacy_env_vars: tuple[str, ...]
    auth_mode: AuthMode
    paste_prompts: tuple[TokenPastePrompt, ...] = field(default_factory=tuple)
    whoami: WhoamiSpec | None = None
    revoke: RevokeSpec | None = None
    oauth_device_code: OAuthDeviceCodeSpec | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PROVIDERS: dict[ProviderId, ProviderConfig] = {
    "github": ProviderConfig(
        id="github",
        display_name="GitHub",
        legacy_env_vars=("GITHUB_TOKEN",),
        auth_mode=AuthMode.TOKEN_PASTE,
        paste_prompts=(TokenPastePrompt(field="token", label="GitHub personal access token (PAT)"),),
        whoami=WhoamiSpec(
            url_template="https://api.github.com/user",
            auth_header_template="Bearer {token}",
            account_field=("login",),
        ),
        # GitHub does not expose a self-service token revoke for fine-grained PATs
        # at a stable URL; users revoke through the web UI. Leaving revoke=None
        # keeps `creds revoke` to a local-only delete (which the ticket allows).
        revoke=None,
        notes="PATs revoke through https://github.com/settings/tokens — this CLI only deletes the local copy.",
    ),
    "linear": ProviderConfig(
        id="linear",
        display_name="Linear",
        legacy_env_vars=("LINEAR_API_KEY",),
        # Default to token-paste; the OAuth device-code flow is opt-in via
        # the connect CLI flag because most Linear users still ship API keys.
        auth_mode=AuthMode.TOKEN_PASTE,
        paste_prompts=(TokenPastePrompt(field="token", label="Linear API key (lin_api_...)"),),
        whoami=WhoamiSpec(
            url_template="https://api.linear.app/graphql",
            auth_header_template="{token}",
            account_field=("data", "viewer", "email"),
        ),
        revoke=None,
        oauth_device_code=OAuthDeviceCodeSpec(
            device_code_endpoint="https://api.linear.app/oauth/authorize",
            token_endpoint="https://api.linear.app/oauth/token",
            client_id="bernstein-cli",
            scope="read,write",
        ),
        notes="OAuth requires a registered Linear OAuth app; opt in with --oauth.",
    ),
    "jira": ProviderConfig(
        id="jira",
        display_name="Jira Cloud",
        legacy_env_vars=("JIRA_EMAIL", "JIRA_API_TOKEN"),
        auth_mode=AuthMode.TOKEN_PASTE,
        paste_prompts=(
            TokenPastePrompt(
                field="base_url",
                label="Jira Cloud base URL (https://acme.atlassian.net)",
                is_secret=False,
            ),
            TokenPastePrompt(field="email", label="Atlassian account email", is_secret=False),
            TokenPastePrompt(field="token", label="Jira API token"),
        ),
        whoami=WhoamiSpec(
            url_template="{base_url}/rest/api/3/myself",
            auth_header_template="Basic {basic_b64}",
            account_field=("emailAddress",),
        ),
        revoke=None,
        notes="Jira API tokens revoke through https://id.atlassian.com/manage-profile/security/api-tokens.",
    ),
    "slack": ProviderConfig(
        id="slack",
        display_name="Slack",
        legacy_env_vars=("BERNSTEIN_SLACK_TOKEN", "SLACK_BOT_TOKEN"),
        auth_mode=AuthMode.TOKEN_PASTE,
        paste_prompts=(TokenPastePrompt(field="token", label="Slack bot token (xoxb-...)"),),
        whoami=WhoamiSpec(
            url_template="https://slack.com/api/auth.test",
            auth_header_template="Bearer {token}",
            account_field=("user",),
        ),
        revoke=RevokeSpec(
            url_template="https://slack.com/api/auth.revoke",
            method="POST",
            auth_header_template="Bearer {token}",
        ),
    ),
    "telegram": ProviderConfig(
        id="telegram",
        display_name="Telegram bot",
        legacy_env_vars=("BERNSTEIN_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"),
        auth_mode=AuthMode.TOKEN_PASTE,
        paste_prompts=(TokenPastePrompt(field="token", label="Telegram bot token (123456:ABC-DEF...)"),),
        whoami=WhoamiSpec(
            url_template="https://api.telegram.org/bot{token}/getMe",
            auth_header_template="",
            account_field=("result", "username"),
        ),
        # Telegram has no token-revoke endpoint; users rotate via @BotFather.
        revoke=None,
        notes="Rotate the token via @BotFather; this CLI only deletes the local copy.",
    ),
}


# Type alias used in the public surface; callers can rely on the dataclass
# alias even if the registry contents change later.
Provider = ProviderConfig


def list_providers() -> tuple[ProviderConfig, ...]:
    """Return all registered providers in stable, sorted-by-id order."""
    return tuple(_PROVIDERS[pid] for pid in sorted(_PROVIDERS.keys()))


def require_provider(provider_id: str) -> ProviderConfig:
    """Look up a provider by id or raise :class:`KeyError`.

    Used by the CLI so a typo like ``bernstein connect githab`` produces a
    clean error rather than a stack trace.
    """
    if provider_id not in _PROVIDERS:
        valid = ", ".join(sorted(_PROVIDERS.keys()))
        raise KeyError(f"Unknown provider {provider_id!r}; valid: {valid}")
    return _PROVIDERS[provider_id]  # type: ignore[index]
