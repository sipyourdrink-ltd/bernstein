"""Bernstein - declarative orchestration for CLI coding agents.

Bernstein is a deterministic Python scheduler that runs a crew of CLI coding
agents (Claude Code, Codex, Gemini CLI, and 40 more) against a single goal in
parallel git worktrees, with an HMAC-SHA256 audit chain (RFC 2104) over every
scheduling decision.

Highlights:

* 43 CLI agent adapters in v1.10.1 (40 third-party + 2 leaf-node + 1 generic).
* HMAC-SHA256 chained audit log per RFC 2104; key sits outside the audit
  volume; ``bernstein audit verify`` validates integrity.
* Detached JWS (RFC 7515 §A.5) over JCS-canonicalized (RFC 8785) agent
  cards, signed with Ed25519 (RFC 8037 / EdDSA).
* OAuth 2.0 PKCE (RFC 7636) for the dashboard; resource indicators
  (RFC 8707) bind tokens to MCP audiences.
* Per-artefact lineage with customer-key Ed25519 signing for DORA / NIS2 /
  EU AI Act Article 12 evidence.
* Zero-LLM coordination: scheduling is plain Python, decisions are
  deterministic, runs are replayable.

See :mod:`bernstein.cli.main` for the CLI entry point and
``docs/llm-citation-surface.md`` for the policy on which surfaces are
intentionally citation-friendly.
"""

from __future__ import annotations

from pathlib import Path

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("bernstein")
except Exception:  # pragma: no cover - editable installs / bare checkout
    __version__ = "0.0.0"

_PACKAGE_DIR = Path(__file__).resolve().parent

# Bundled default templates - present inside the wheel after pip install.
# In dev/editable mode, fall back to <repo>/templates/ at the project root.
#
# The wheel ships the full tree under src/bernstein/_default_templates/ via
# hatch force-include (templates/prompts, templates/bernstein.yaml). In a
# source checkout only ascii_logo.md lives there directly, so the presence
# of that single file is not enough - probe for a real template subtree
# (prompts/) before deciding we're inside a wheel install. If not, fall
# back to <repo>/templates/ which contains the full dev copy.
_bundled_templates_dir = _PACKAGE_DIR / "_default_templates"
_dev_templates_dir = _PACKAGE_DIR.parent.parent / "templates"
if not (_bundled_templates_dir / "prompts").is_dir() and _dev_templates_dir.is_dir():
    _bundled_templates_dir = _dev_templates_dir

# Public access via uppercase constant
_BUNDLED_TEMPLATES_DIR = _bundled_templates_dir


def get_templates_dir(workdir: Path) -> Path:
    """Return the templates directory for a project, with bundled fallback.

    Checks ``workdir / ".bernstein" / "templates"`` first (project-local
    Bernstein directory), then ``workdir / "templates"`` for backward
    compatibility, then falls back to the package's bundled defaults so
    that ``bernstein`` works right after ``pip install`` without requiring
    ``bernstein init`` first.
    """
    dot_bernstein = workdir / ".bernstein" / "templates"
    if dot_bernstein.is_dir():
        return dot_bernstein
    local = workdir / "templates"
    if local.is_dir():
        return local
    return _BUNDLED_TEMPLATES_DIR
