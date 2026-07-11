"""Compatibility ledger for legacy ``bernstein.core`` redirects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class RedirectLedgerPolicy:
    """Shared policy metadata for the core redirect map."""

    owner: str
    first_release: str
    removal_policy: str


@dataclass(frozen=True, slots=True)
class RedirectLedgerEntry:
    """One reviewed legacy import redirect."""

    old_path: str
    new_path: str
    owner: str
    first_release: str
    removal_policy: str


REDIRECT_LEDGER_POLICY: Final[RedirectLedgerPolicy] = RedirectLedgerPolicy(
    owner="core-maintainers",
    first_release="pre-1.0",
    removal_policy=(
        "Keep until a redirect-specific removal PR deletes the map entry and proves no live importers remain."
    ),
)

REVIEWED_REDIRECT_MAP_DIGEST: Final[str] = "7739633119917414455f337b5550be737e9aed29c19f7a441e8bbf6dd0b74c57"


def redirect_map_digest(redirects: Mapping[str, str]) -> str:
    """Return a stable SHA-256 digest for a redirect map."""
    canonical = json.dumps(sorted(redirects.items()), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_redirect_ledger(redirects: Mapping[str, str]) -> dict[str, RedirectLedgerEntry]:
    """Build reviewed ledger entries for the current redirect map."""
    return {
        old: RedirectLedgerEntry(
            old_path=f"bernstein.core.{old}",
            new_path=new,
            owner=REDIRECT_LEDGER_POLICY.owner,
            first_release=REDIRECT_LEDGER_POLICY.first_release,
            removal_policy=REDIRECT_LEDGER_POLICY.removal_policy,
        )
        for old, new in redirects.items()
    }


__all__ = [
    "REDIRECT_LEDGER_POLICY",
    "REVIEWED_REDIRECT_MAP_DIGEST",
    "RedirectLedgerEntry",
    "RedirectLedgerPolicy",
    "build_redirect_ledger",
    "redirect_map_digest",
]
