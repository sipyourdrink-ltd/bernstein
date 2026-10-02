"""observability sub-package.

the previous implementation exposed a ``__getattr__`` that walked
``pkgutil.iter_modules`` and lazy-imported every submodule on first attribute
access. That magic defeated static analysis tools (Pyright, Vulture, unimport)
because any attribute could be resolved at runtime, so dead submodules in this
package accreted undetected (see prior audit).

All production importers use fully-qualified submodule paths
(``from bernstein.core.observability.<submodule> import X``) or submodule-style
imports (``from bernstein.core.observability import <submodule>``), both of
which Python's native import machinery handles without any package-level
``__getattr__``. Legacy flat-path names are still served by the meta_path
finder in ``bernstein.core.__init__`` (``_REDIRECT_MAP``); we do NOT re-add the
``pkgutil`` walker here.

If new code needs a symbol re-exported at the package level, import it
explicitly and add it to ``__all__`` below.
"""

from __future__ import annotations

from bernstein.core.observability.hosted_inference_ingest import (
    ATTR_CERTIFIED_ROLES,
    ATTR_COMPLETION_TOKENS,
    ATTR_ENDPOINT_BASE_URL,
    ATTR_ENDPOINT_FINGERPRINT,
    ATTR_MODEL,
    ATTR_OPERATION,
    ATTR_PRINCIPAL_ID,
    ATTR_PROMPT_TOKENS,
    ATTR_REQUEST_DIGEST,
    ATTR_RESPONSE_DIGEST,
    ATTR_ROLE_CERTIFIED,
    ATTR_TOTAL_TOKENS,
    GenAIActivity,
    HostedInferenceIngestAdapter,
    HostedInferenceIngestError,
    HostedInferencePayload,
    IngestCallResult,
    ingest_payload,
)

__all__: list[str] = [
    "ATTR_CERTIFIED_ROLES",
    "ATTR_COMPLETION_TOKENS",
    "ATTR_ENDPOINT_BASE_URL",
    "ATTR_ENDPOINT_FINGERPRINT",
    "ATTR_MODEL",
    "ATTR_OPERATION",
    "ATTR_PRINCIPAL_ID",
    "ATTR_PROMPT_TOKENS",
    "ATTR_REQUEST_DIGEST",
    "ATTR_RESPONSE_DIGEST",
    "ATTR_ROLE_CERTIFIED",
    "ATTR_TOTAL_TOKENS",
    "GenAIActivity",
    "HostedInferenceIngestAdapter",
    "HostedInferenceIngestError",
    "HostedInferencePayload",
    "IngestCallResult",
    "ingest_payload",
    "otlp_ingest",
]
