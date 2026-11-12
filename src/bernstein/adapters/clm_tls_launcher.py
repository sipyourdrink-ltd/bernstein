"""mTLS launcher for the CLM adapter.

Aider talks to the customer-side CLM gateway via the OpenAI Python SDK,
which dispatches through :class:`httpx.Client`. Plain ``OPENAI_API_BASE``
gets the URL onto the gateway, but mTLS needs the client cert / key /
CA bundle threaded into the underlying HTTP client - and httpx 0.28+
deliberately does *not* read those from environment variables.

This launcher monkey-patches :class:`httpx.Client` and
:class:`httpx.AsyncClient` so that any callsite that constructs one
without an explicit ``verify=`` / ``cert=`` automatically inherits the
customer-issued :class:`ssl.SSLContext` produced by
:func:`bernstein.core.protocols.cluster.cluster_tls.build_httpx_client_kwargs`.

The launcher is intentionally tiny: read the three CLM_*_FILE env vars,
build a TLSConfig, install the patches, and hand off to aider's
console-script entry point. No SDK initialisation or aider-version-
specific knowledge is required, so this stays robust to aider releases.

Usage (set by :class:`bernstein.adapters.clm.ClmAdapter` when mTLS is
configured)::

    python -m bernstein.adapters.clm_tls_launcher aider [aider args...]
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Any

from bernstein.adapters.clm import (
    CLM_CA_FILE_ENV,
    CLM_CERT_FILE_ENV,
    CLM_KEY_FILE_ENV,
    CLM_VERIFY_MODE_ENV,
)
from bernstein.core.protocols.cluster.cluster_tls import (
    TLSConfig,
    build_httpx_client_kwargs,
)


def _resolve_tls_from_env() -> TLSConfig | None:
    """Build a :class:`TLSConfig` from the CLM_*_FILE env triple.

    Returns ``None`` if any of the three is unset - in that case the
    launcher is being run without mTLS (defensive: the adapter only
    invokes us when all three are set, but we don't crash if it's not).
    """
    cert = os.environ.get(CLM_CERT_FILE_ENV, "").strip()
    key = os.environ.get(CLM_KEY_FILE_ENV, "").strip()
    ca = os.environ.get(CLM_CA_FILE_ENV, "").strip()
    if not (cert and key and ca):
        return None
    verify_raw = (os.environ.get(CLM_VERIFY_MODE_ENV) or "required").strip()
    if verify_raw not in ("required", "optional", "disabled"):
        verify_raw = "required"
    return TLSConfig(
        ca_file=Path(ca),
        cert_file=Path(cert),
        key_file=Path(key),
        verify_mode=verify_raw,  # type: ignore[arg-type]
    )


def install_httpx_mtls_defaults(tls: TLSConfig) -> None:
    """Patch ``httpx.Client`` / ``httpx.AsyncClient`` to default to ``tls``.

    Each constructor call gets its kwargs *inspected* (not silently
    overwritten): if the caller already supplied ``verify=`` or
    ``cert=`` we honour their explicit choice. This keeps the patch
    safe for libraries that intentionally configure a different
    transport (rare, but possible inside the OpenAI SDK's retry
    helper).
    """
    import httpx  # local import: keep launcher import-light if httpx absent

    kwargs = build_httpx_client_kwargs(tls)
    original_client_init = httpx.Client.__init__
    original_async_init = httpx.AsyncClient.__init__

    def _client_init(self: httpx.Client, *args: Any, **call_kwargs: Any) -> None:
        if "verify" not in call_kwargs:
            call_kwargs["verify"] = kwargs["verify"]
        original_client_init(self, *args, **call_kwargs)

    def _async_init(self: httpx.AsyncClient, *args: Any, **call_kwargs: Any) -> None:
        if "verify" not in call_kwargs:
            call_kwargs["verify"] = kwargs["verify"]
        original_async_init(self, *args, **call_kwargs)

    httpx.Client.__init__ = _client_init  # type: ignore[method-assign]
    httpx.AsyncClient.__init__ = _async_init  # type: ignore[method-assign]


def _run_aider(argv: list[str]) -> int:
    """Hand off to aider's console-script entry point.

    We use :func:`runpy.run_module` rather than :func:`os.execvp` so the
    httpx monkey-patch installed above survives into aider's process.
    ``sys.argv`` is rewritten to look like a direct ``aider …`` invocation.
    """
    if not argv:
        sys.stderr.write("clm_tls_launcher: no command supplied\n")
        return 2
    target, *rest = argv
    if target != "aider":
        # Defensive: the adapter only ever passes "aider" as the first
        # positional; if a future caller asks for a different binary we
        # refuse rather than silently mis-routing the request.
        sys.stderr.write(f"clm_tls_launcher: only supports aider, got {target!r}\n")
        return 2
    sys.argv = [target, *rest]
    try:
        runpy.run_module("aider", run_name="__main__", alter_sys=True)
    # SystemExit is the expected control signal from runpy: aider calls
    # sys.exit(), and we translate its code into our own return value
    # rather than letting the process die mid-launcher.
    except SystemExit as exc:  # NOSONAR python:S5754 - runpy propagates aider's sys.exit; mapped to a return code
        code = exc.code
        if isinstance(code, int):
            return code
        return 0 if code is None else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Launch Aider with TLS material resolved from the environment."""
    args = list(sys.argv[1:] if argv is None else argv)
    tls = _resolve_tls_from_env()
    if tls is not None:
        install_httpx_mtls_defaults(tls)
    return _run_aider(args)


if __name__ == "__main__":
    sys.exit(main())
