"""Provenance trust policy for replaying persistent memory into a spawned prompt.

``SQLiteMemoryStore.get_relevant()`` is the read path
``spawner_core._load_persistent_memory`` uses to build every spawned agent's
prompt, and it injects ``MemoryEntry.content`` verbatim. Before this module
existed, that call had no ``source_adapter`` awareness at all: a row written
under any adapter's provenance (or none) was replayed into every other
adapter's spawned prompt. The opt-in ``read_only_from_adapters`` allow-list on
``SQLiteMemoryStore.query()`` / ``CrossTaskKB.subscribe()`` never covered this
path, so it offered no protection here - see ``docs/operations/memory.md``
for the documented cross-adapter memory-poisoning threat this closes.

:class:`MemoryTrustPolicy` is the enforcement primitive: it decides, per
row, whether a ``MemoryEntry`` may be replayed into a spawned prompt. The
policy is enabled and enforced by default - untagged rows (pre-migration
data, and today's operator/CLI writes) stay trusted exactly as before, but a
row carrying an explicit ``source_adapter`` value is only trusted if that
adapter has been opted in. Operators can widen or fully disable the policy
through environment configuration; see :func:`active_trust_policy`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.memory.sqlite_store import MemoryEntry

# Set to a falsy value ("0", "false", "no", "off") to fully disable the
# trust policy and restore the legacy "replay every row" behaviour. This is
# a security control, not an opt-in feature, so it defaults to enabled.
TRUST_POLICY_ENABLED_ENV_VAR = "BERNSTEIN_MEMORY_TRUST_POLICY"

# Comma-separated allow-list of extra source_adapter values to trust
# alongside untagged (NULL) rows, e.g. "claude-code,codex". Empty/unset
# trusts only untagged rows by default.
TRUSTED_ADAPTERS_ENV_VAR = "BERNSTEIN_MEMORY_TRUST_ADAPTERS"

_FALSY = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class MemoryTrustPolicy:
    """Decides which persistent-memory rows may reach a spawned prompt.

    Attributes:
        enabled: When False, every row is trusted (legacy passthrough).
            Defaults to True: this is the enforcement switch, not a feature
            flag callers are expected to flip on.
        trusted_adapters: ``source_adapter`` values that are trusted in
            addition to untagged rows. Empty by default - operators opt
            specific adapters in explicitly once they trust cross-adapter
            replay for them.
        trust_untagged: Whether rows with no recorded ``source_adapter``
            (pre-migration rows, and today's operator/CLI writes - see
            ``docs/operations/memory.md``) are trusted. True by default so
            those flows see no behavioural change.
    """

    enabled: bool = True
    trusted_adapters: frozenset[str] = field(default_factory=frozenset)
    trust_untagged: bool = True

    def is_trusted(self, entry: MemoryEntry) -> bool:
        """Return whether ``entry`` may be replayed into a spawned prompt."""
        if not self.enabled:
            return True
        if entry.source_adapter is None:
            return self.trust_untagged
        return entry.source_adapter in self.trusted_adapters

    def filter_entries(self, entries: Iterable[MemoryEntry]) -> list[MemoryEntry]:
        """Return only the entries this policy trusts, preserving order."""
        return [entry for entry in entries if self.is_trusted(entry)]


def _env_truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in _FALSY


def active_trust_policy() -> MemoryTrustPolicy:
    """Build the trust policy from the current process environment.

    ``BERNSTEIN_MEMORY_TRUST_POLICY`` defaults to enabled; set it to
    ``0``/``false``/``no``/``off`` to disable enforcement entirely.
    ``BERNSTEIN_MEMORY_TRUST_ADAPTERS`` is a comma-separated allow-list of
    additional ``source_adapter`` values to trust alongside untagged rows.
    """
    enabled = _env_truthy(os.environ.get(TRUST_POLICY_ENABLED_ENV_VAR), default=True)
    raw_adapters = os.environ.get(TRUSTED_ADAPTERS_ENV_VAR, "")
    trusted_adapters = frozenset(part.strip() for part in raw_adapters.split(",") if part.strip())
    return MemoryTrustPolicy(enabled=enabled, trusted_adapters=trusted_adapters)
