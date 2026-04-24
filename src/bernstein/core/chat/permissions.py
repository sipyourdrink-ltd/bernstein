"""Allow-list loader for chat drivers.

Exposes :class:`AllowList`, a tiny container that decides whether a
platform user id is permitted to drive agents from chat. The source of
truth is ``bernstein.yaml`` under the ``chat.allowed_users`` key::

    chat:
      allowed_users:
        - "12345678"        # telegram user id
        - "U01ABCDEF"       # slack user id

Environment variable ``BERNSTEIN_CHAT_ALLOW`` (comma-separated) and an
explicit CLI override (``--allow``) are merged on top of the yaml list
so ad-hoc deployments don't have to edit config.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["AllowList", "load_allow_list"]

logger = logging.getLogger(__name__)

_ENV_VAR = "BERNSTEIN_CHAT_ALLOW"


@dataclass(slots=True)
class AllowList:
    """Set-backed user allow-list.

    An empty allow-list denies everyone -- the chat surface is opt-in by
    design, and a misconfigured yaml should never leak agent control to
    the entire world.
    """

    users: set[str] = field(default_factory=lambda: set())

    def is_allowed(self, user_id: str | int) -> bool:
        """Return True iff the string form of ``user_id`` is permitted."""
        return str(user_id) in self.users

    def extend(self, more: Iterable[str]) -> None:
        """Merge additional user ids into the allow-list."""
        for uid in more:
            cleaned = str(uid).strip()
            if cleaned:
                self.users.add(cleaned)


def load_allow_list(
    config_path: Path | str = "bernstein.yaml",
    *,
    cli_override: Iterable[str] | None = None,
) -> AllowList:
    """Load the allow-list from yaml, env var, and optional CLI override.

    Resolution order (later sources are unioned, not replaced):

      1. ``bernstein.yaml :: chat.allowed_users`` (list of strings).
      2. ``$BERNSTEIN_CHAT_ALLOW`` (comma-separated).
      3. ``cli_override`` -- the raw value of ``--allow``.

    Args:
        config_path: Path to the yaml config.
        cli_override: Optional iterable of user ids parsed from CLI.

    Returns:
        A populated :class:`AllowList`. Never ``None``; an empty list is
        a valid configuration that simply denies everyone.
    """
    allow = AllowList()
    path = Path(config_path)
    if path.exists():
        raw: Any = None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("chat: could not parse %s: %s", path, exc)
        if isinstance(raw, dict):
            raw_dict = cast("dict[Any, Any]", raw)
            chat_section = raw_dict.get("chat")
            if isinstance(chat_section, dict):
                chat_dict = cast("dict[Any, Any]", chat_section)
                allowed = chat_dict.get("allowed_users")
                if isinstance(allowed, list):
                    allowed_list = cast("list[Any]", allowed)
                    allow.extend(str(u) for u in allowed_list)

    env_value = os.environ.get(_ENV_VAR, "")
    if env_value:
        allow.extend(part for part in env_value.split(",") if part.strip())

    if cli_override is not None:
        allow.extend(cli_override)

    return allow
