"""WEB-022: Dashboard embedding support (iframe-friendly).

Configurable X-Frame-Options and CSP headers to allow the dashboard
to be embedded in iframes (VS Code webview, Notion, internal portals).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingConfig:
    """Configuration for iframe embedding.

    Attributes:
        allow_embedding: Whether to allow iframe embedding.
        allowed_origins: Specific origins allowed (empty = all if allow_embedding is True).
    """

    allow_embedding: bool = False
    allowed_origins: list[str] = field(default_factory=list)


def build_frame_options_header(config: EmbeddingConfig) -> str | None:
    """Build X-Frame-Options header value.

    Args:
        config: Embedding configuration.

    Returns:
        Header value, or None if not needed (CSP handles it).
    """
    if not config.allow_embedding:
        return "DENY"
    # When embedding is allowed, we rely on CSP frame-ancestors instead
    return None


def build_csp_header(config: EmbeddingConfig) -> str:
    """Build Content-Security-Policy frame-ancestors directive.

    Args:
        config: Embedding configuration.

    Returns:
        CSP header value string.
    """
    if not config.allow_embedding:
        return "frame-ancestors 'none'"
    if config.allowed_origins:
        origins = " ".join(config.allowed_origins)
        return f"frame-ancestors 'self' {origins}"
    return "frame-ancestors *"


def load_embedding_config(yaml_path: Path | None = None) -> EmbeddingConfig:
    """Load embedding configuration from bernstein.yaml.

    Args:
        yaml_path: Path to config file. Searches defaults if None.

    Returns:
        EmbeddingConfig with user preferences or defaults.
    """
    try:
        import yaml
    except ImportError:
        return EmbeddingConfig()

    candidates: list[Path] = []
    if yaml_path:
        candidates.append(yaml_path)
    else:
        candidates.append(Path("bernstein.yaml"))
        candidates.append(Path.home() / ".bernstein" / "bernstein.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            emb = data.get("embedding")
            if not isinstance(emb, dict):
                continue
            return EmbeddingConfig(
                allow_embedding=bool(emb.get("allow_embedding", False)),
                allowed_origins=list(emb.get("allowed_origins", [])),
            )
        except Exception:
            continue
    return EmbeddingConfig()


class EmbeddingHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware that adds iframe embedding headers to dashboard responses.

    Only applies to /dashboard paths.
    """

    def __init__(self, app: Any, config: EmbeddingConfig | None = None) -> None:
        super().__init__(app)
        self._config = config or load_embedding_config()

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """Add embedding headers to dashboard responses."""
        response: Response = await call_next(request)

        # Only apply to dashboard paths
        if request.url.path.startswith("/dashboard"):
            frame_opts = build_frame_options_header(self._config)
            if frame_opts:
                response.headers["X-Frame-Options"] = frame_opts
            response.headers["Content-Security-Policy"] = build_csp_header(self._config)

        return response
