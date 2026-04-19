"""Storage-sink credential scoping (oai-003).

Sinks resolve their provider credentials from environment variables
on the *orchestrator* process. Spawned agents must never see these
credentials — otherwise a compromised agent could exfiltrate the
orchestrator's long-lived cloud keys.

Bernstein's default env-isolation layer (:mod:`bernstein.adapters.env_isolation`)
is whitelist-based, so the following list acts as a warning surface:
any new AWS/GCS/Azure/R2 variable the sinks start consuming must also
be added here so audits can assert it is never forwarded to an agent.

The list is consumed by two places:

- :func:`list_storage_credential_env_vars` — exposed so documentation
  and tests can enumerate the current surface.
- :func:`scrub_env` — strips the listed keys from a given mapping,
  used in one-off spawner paths that bypass ``build_filtered_env``.
"""

from __future__ import annotations

from typing import Final

#: Environment variables consumed by the first-party cloud sinks.
#: Keep in sync with the constructor fallbacks in
#: :mod:`bernstein.core.storage.sinks` — every env var read by a sink
#: must appear here so it can be scrubbed from agent environments.
STORAGE_CREDENTIAL_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        # S3 + R2 (R2 reuses the S3 boto3 stack)
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_ENDPOINT_URL",
        "BERNSTEIN_S3_BUCKET",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "BERNSTEIN_R2_BUCKET",
        # GCS
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "BERNSTEIN_GCS_BUCKET",
        # Azure Blob
        "AZURE_STORAGE_CONNECTION_STRING",
        "AZURE_STORAGE_ACCOUNT_NAME",
        "AZURE_STORAGE_ACCOUNT_KEY",
        "BERNSTEIN_AZURE_CONTAINER",
    }
)


def list_storage_credential_env_vars() -> list[str]:
    """Return a sorted list of sink-owned env-var names.

    Useful for documentation generation and for asserting that the
    env-isolation layer strips every one.
    """
    return sorted(STORAGE_CREDENTIAL_ENV_VARS)


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* with every sink credential removed.

    Args:
        env: Mapping to scrub. Not mutated.

    Returns:
        A fresh dict containing only keys not in
        :data:`STORAGE_CREDENTIAL_ENV_VARS`.
    """
    return {k: v for k, v in env.items() if k not in STORAGE_CREDENTIAL_ENV_VARS}


__all__ = [
    "STORAGE_CREDENTIAL_ENV_VARS",
    "list_storage_credential_env_vars",
    "scrub_env",
]
