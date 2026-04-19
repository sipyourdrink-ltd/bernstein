"""``load_skill`` MCP tool implementation.

Exposed to agents via :mod:`bernstein.mcp.server` — the tool returns the
SKILL.md body by default and can also fetch a single ``references/`` or
``scripts/`` file when the agent names one. Every invocation emits a WAL
event (best-effort) and a structured return dict that the MCP harness
serialises as JSON.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.skills.loader import (
    SkillNotFoundError,
    default_loader_from_templates,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.skills.loader import SkillLoader

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillLoadResult:
    """Typed response for the ``load_skill`` tool.

    Attributes match the contract in the ticket:

    - Always: ``name``, ``body``, ``available_references``, ``available_scripts``.
    - When ``reference`` is passed: ``reference_content``.
    - When ``script``  is passed: ``script_content``.
    - On error: ``error`` is populated and other fields are best-effort.
    """

    name: str
    body: str
    available_references: list[str]
    available_scripts: list[str]
    reference_content: str | None = None
    script_content: str | None = None
    error: str | None = None


def load_skill(
    name: str,
    *,
    reference: str | None = None,
    script: str | None = None,
    loader: SkillLoader | None = None,
    templates_roles_dir: Path | None = None,
    wal_sink: _WalSinkProto | None = None,
) -> SkillLoadResult:
    """Fetch a skill body (and optionally a reference / script file).

    Args:
        name: Skill name (required).
        reference: Optional filename under ``references/``.
        script: Optional filename under ``scripts/``.
        loader: Inject a pre-built loader (tests use this). When omitted,
            a default loader is built from ``templates_roles_dir``.
        templates_roles_dir: Path to ``templates/roles/`` — used only when
            ``loader`` is ``None`` to discover the ``skills/`` sibling.
            When both are ``None`` the function raises ``ValueError``.
        wal_sink: Optional callback receiving a ``skill_loaded`` event
            dict. Defaults to logging at ``INFO``.

    Returns:
        :class:`SkillLoadResult` describing what was loaded.
    """
    start = time.monotonic()
    resolved_loader = _resolve_loader(loader, templates_roles_dir)
    sink = wal_sink or _log_wal_sink

    try:
        skill = resolved_loader.get(name)
    except SkillNotFoundError:
        return _build_error_result(name, f"skill {name!r} not found")

    reference_content, ref_error = _read_bucket(resolved_loader.read_reference, name, reference, "reference")
    script_content, script_error = _read_bucket(resolved_loader.read_script, name, script, "script")
    error = ref_error or script_error

    sink(
        {
            "event": "skill_loaded",
            "name": name,
            "reference": reference,
            "script": script,
            "source": skill.source_name,
            "duration_s": time.monotonic() - start,
            "error": error,
        }
    )

    return SkillLoadResult(
        name=name,
        body=skill.body,
        available_references=list(skill.references),
        available_scripts=list(skill.scripts),
        reference_content=reference_content,
        script_content=script_content,
        error=error,
    )


def result_as_dict(result: SkillLoadResult) -> dict[str, Any]:
    """Convert a :class:`SkillLoadResult` into a JSON-safe dict.

    Empty / ``None`` fields are preserved so MCP clients see a stable
    shape regardless of which optional parameters the agent passed.
    """
    return asdict(result)


def _resolve_loader(
    loader: SkillLoader | None,
    templates_roles_dir: Path | None,
) -> SkillLoader:
    """Return a loader, building a default one when none was injected."""
    if loader is not None:
        return loader
    if templates_roles_dir is None:
        raise ValueError("load_skill requires either ``loader`` or ``templates_roles_dir``")
    return default_loader_from_templates(templates_roles_dir)


def _read_bucket(
    reader: Callable[[str, str], str],
    skill_name: str,
    filename: str | None,
    label: str,
) -> tuple[str | None, str | None]:
    """Invoke ``reader(skill_name, filename)`` and translate exceptions.

    Returns ``(content, error)`` where exactly one is populated (or both are
    ``None`` when ``filename`` is ``None``). The error message mirrors the
    wording used by the original inline ``try/except`` so callers see an
    identical contract.
    """
    if filename is None:
        return None, None
    try:
        return reader(skill_name, filename), None
    except FileNotFoundError as exc:
        return None, str(exc)
    except (ValueError, RuntimeError) as exc:
        return None, f"failed to read {label}: {exc}"


def _build_error_result(name: str, detail: str) -> SkillLoadResult:
    """Helper for consistent error-result construction."""
    return SkillLoadResult(
        name=name,
        body="",
        available_references=[],
        available_scripts=[],
        error=detail,
    )


def _log_wal_sink(event: dict[str, Any]) -> None:
    """Default WAL sink — structured log line at ``INFO``.

    Real production callers inject :meth:`bernstein.core.persistence.wal.Wal.append`
    (or equivalent) so the event hits the durable log.
    """
    logger.info(
        "skill_loaded name=%s reference=%s script=%s source=%s duration=%.4fs error=%s",
        event.get("name"),
        event.get("reference"),
        event.get("script"),
        event.get("source"),
        event.get("duration_s", 0.0),
        event.get("error"),
    )


# Protocol for WAL sinks — used only for pyright narrowing; Python's
# structural typing checks the callable shape at runtime.
class _WalSinkProto:
    """Callable ``(event: dict) -> None`` protocol alias."""

    def __call__(self, event: dict[str, Any]) -> None: ...  # pragma: no cover
