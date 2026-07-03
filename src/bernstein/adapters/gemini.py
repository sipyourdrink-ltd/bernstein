"""Google Gemini / Antigravity CLI adapter.

The upstream CLI is changing binary names ahead of a deprecation date:
the legacy ``gemini`` binary stops serving free / AI Pro / Ultra
subscribers on 2026-06-18; the replacement ``antigravity`` binary uses
the same model set and the same ``--output-format`` semantics.
Enterprise customers retain the legacy binary via paid API keys.

The adapter is dual-binary aware. At spawn time it discovers which
binary is on ``PATH`` using a deterministic cascade defined by
``_DISCOVERY_CASCADE``:

1. The operator override ``BERNSTEIN_GEMINI_BINARY`` wins when set
   (and must resolve on ``PATH``, regardless of ``strict`` mode).
2. Otherwise ``antigravity`` is preferred.
3. The legacy ``gemini`` binary is used as a fallback.
4. If neither resolves, behavior depends on the ``strict`` flag
   passed to :func:`resolve_google_cli_binary`:

   - ``strict=True`` (used by ``bernstein adapters check`` / doctor):
     the adapter raises :class:`BinaryNotInstalledError`.
   - ``strict=False`` (default, used by :meth:`GeminiAdapter.spawn`):
     the resolver returns the first cascade entry as a fallback,
     letting downstream :func:`subprocess.Popen` raise the natural
     ``FileNotFoundError`` on actual invocation. This matches the
     codex / aider adapter posture and keeps tests that mock
     ``subprocess`` from tripping on eager discovery.

The adapter contract (flags, env-isolation allow-list, sandbox
profile, rate-limit meter, network policy) is unchanged: only the
binary name and the discovery step differ.

Recommended models (identical on both binaries):
``gemini-3.1-pro`` (highest reasoning), ``gemini-3-flash`` (default in
the Gemini app, Pro-grade reasoning at Flash speed), or
``gemini-3.1-flash-lite`` for the cheapest tier.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnError,
    SpawnResult,
    build_worker_cmd,
)
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

logger = logging.getLogger(__name__)

#: Operator override env var for the binary name. Takes precedence over
#: the cascade so operators on a non-default install path do not have to
#: rely on ``PATH`` ordering.
BINARY_ENV_VAR: str = "BERNSTEIN_GEMINI_BINARY"

#: Replacement binary shipped by the upstream CLI rename.
ANTIGRAVITY_BINARY: str = "antigravity"

#: Legacy binary name, retained for operators still on the deprecated
#: install path or on a paid Enterprise license.
LEGACY_GEMINI_BINARY: str = "gemini"

#: Discovery cascade in priority order. ``antigravity`` first; the
#: legacy binary as fallback.
_DISCOVERY_CASCADE: tuple[str, ...] = (ANTIGRAVITY_BINARY, LEGACY_GEMINI_BINARY)


class BinaryNotInstalledError(SpawnError):
    """Raised when neither ``antigravity`` nor ``gemini`` resolves on ``PATH``.

    The message lists both expected binaries and the override env var so
    the operator-facing error is self-explanatory without consulting the
    docs.
    """


def resolve_google_cli_binary(
    *,
    which: Any = None,
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> str:
    """Resolve the Google CLI binary to invoke for this adapter.

    Args:
        which: Callable matching :func:`shutil.which`. Tests override
            this to simulate ``PATH`` contents without touching the
            real filesystem.
        env: Environment mapping to consult for
            :data:`BINARY_ENV_VAR`. Defaults to :data:`os.environ`.
        strict: When ``True`` (e.g. from ``bernstein adapters check``)
            raise :class:`BinaryNotInstalledError` if no cascade entry
            resolves. When ``False`` (default, used by :meth:`spawn`)
            return the first cascade entry as a fallback so the
            downstream :func:`subprocess.Popen` raises the natural
            ``FileNotFoundError`` if the binary is genuinely missing.
            This matches the behaviour of the other adapters
            (codex, aider) and keeps tests that mock subprocess from
            tripping on eager discovery.

    Returns:
        The binary name to invoke. Operator override (when set and
        non-empty) wins. Otherwise the first cascade entry that
        resolves on ``PATH`` is returned, or the first cascade entry
        as a fallback when ``strict=False``.

    Raises:
        BinaryNotInstalledError: When ``strict=True`` and neither the
            override nor any cascade entry resolves, OR when the
            override is set but missing on ``PATH`` (regardless of
            ``strict``, since this is operator error).
    """
    source_env = env if env is not None else os.environ
    # Resolve ``shutil.which`` at call time so tests that patch
    # ``bernstein.adapters.gemini.shutil.which`` see the swap.
    resolver = which if which is not None else shutil.which
    override = (source_env.get(BINARY_ENV_VAR) or "").strip()
    if override:
        if resolver(override) is None:
            raise BinaryNotInstalledError(
                f"{BINARY_ENV_VAR}={override!r} but {override!r} is not on PATH. "
                f"Unset {BINARY_ENV_VAR} to fall back to discovery, or install the binary."
            )
        return override

    for candidate in _DISCOVERY_CASCADE:
        if resolver(candidate) is not None:
            return candidate

    if strict:
        raise BinaryNotInstalledError(
            "Neither 'antigravity' nor 'gemini' was found on PATH. "
            "Install the Antigravity CLI (per docs/adapters/antigravity.md), "
            "or set "
            f"{BINARY_ENV_VAR}=<path-or-name> to override discovery."
        )
    # Non-strict mode: return the first cascade entry as a fallback so
    # the call site (typically subprocess.Popen) surfaces the missing
    # binary as a natural FileNotFoundError it can already handle.
    return _DISCOVERY_CASCADE[0]


# ---------------------------------------------------------------------------
# Multimodal attachment encoding (issue #1797)
# ---------------------------------------------------------------------------


def _inject_multimodal_attachments(prompt: str, multimodal_context: Any) -> str:
    """Inline encoded attachments at the head of the Gemini prompt.

    Gemini accepts inline image bytes via ``inline_data`` blocks in the
    Generative Language API request. The CLI surface here forwards the
    prompt as a single argument so we serialise attachments as
    ``<attachment>`` XML-ish blocks that the CLI's prompt processor
    inlines verbatim. This matches the Claude adapter wire format so
    downstream replay can verify exact bytes for both providers.

    The ``sha256`` attribute is computed over the *decoded* base64
    payload -- the bytes the API receives -- so the announced digest
    matches the inlined content even if the source file changes
    between context construction and spawn time.
    (bot-ack: 3284182752 -- CodeRabbit major.)
    """
    inputs = getattr(multimodal_context, "inputs", ())
    if not inputs:
        return prompt

    import base64 as _base64
    import hashlib as _hashlib

    blocks: list[str] = []
    for inp in inputs:
        b64 = getattr(inp, "content_base64", None) or ""
        mime = getattr(inp, "mime_type", "application/octet-stream")
        if b64:
            try:
                raw = _base64.b64decode(b64, validate=True)
                digest = _hashlib.sha256(raw).hexdigest()
            except (ValueError, TypeError):
                digest = ""
        else:
            digest = ""
        blocks.append(f'<attachment mime="{mime}" sha256="{digest}">\n{b64}\n</attachment>')
    header = "\n".join(blocks)
    return f"{header}\n\n{prompt}"


class GeminiAdapter(CLIAdapter):
    """Spawn and monitor Google Gemini / Antigravity CLI sessions."""

    # Provider-string aliases this adapter resolves from in
    # ``_infer_adapter_name_for_provider`` (via the registry's
    # provider-alias table). Unchanged from the old substring branch.
    provides = ("gemini", "google")

    external_endpoints = (("generativelanguage.googleapis.com", 443),)
    # Google Generative Language returns HTTP 429 with status
    # ``RESOURCE_EXHAUSTED`` once per-minute quotas are tripped.
    rate_limit_provider = "google_generative_language"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        self.enforce_network_policy()
        # Issue #1797: inline encoded attachments at the head of the
        # prompt body so the Gemini API receives the bytes alongside
        # the text. The Antigravity / legacy Gemini CLIs do not accept
        # attachments as separate arguments.
        if multimodal_context is not None:
            prompt = _inject_multimodal_attachments(prompt, multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            # Both binaries support keyring / OAuth auth without API
            # keys - this is just informational.
            logger.debug("GeminiAdapter: no GOOGLE_API_KEY/GEMINI_API_KEY set (using OAuth)")

        binary = resolve_google_cli_binary()

        cmd = [
            binary,
            "-p",
            prompt,
            "-m",
            model_config.model,
            "--output-format",
            "json",
            "--yolo",
        ]

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(
            [
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_APPLICATION_CREDENTIALS",
            ]
        )
        if api_key and not env.get("GEMINI_API_KEY"):
            env["GEMINI_API_KEY"] = api_key

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"{binary} not found in PATH. Install the Antigravity CLI "
                    "or the legacy Gemini CLI; see docs/adapters/antigravity.md."
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {binary}: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name=binary)

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Gemini"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Gemini API tier based on environment configuration.

        Checks GOOGLE_API_KEY and GOOGLE_CLOUD_PROJECT to determine tier:
        - With GCP project = Enterprise tier
        - With paid API key = Pro tier
        - Default = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

        if not api_key:
            return None

        # Determine tier from environment
        if gcp_project:
            tier = ApiTier.ENTERPRISE
            rate_limit = RateLimit(
                requests_per_minute=1000,
                tokens_per_minute=100000,
            )
        elif api_key.startswith("AIza"):
            # Standard API key format
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=15,
                tokens_per_minute=1500,
            )

        return ApiTierInfo(
            provider=ProviderType.GEMINI,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )


__all__ = [
    "ANTIGRAVITY_BINARY",
    "BINARY_ENV_VAR",
    "LEGACY_GEMINI_BINARY",
    "BinaryNotInstalledError",
    "GeminiAdapter",
    "resolve_google_cli_binary",
]
