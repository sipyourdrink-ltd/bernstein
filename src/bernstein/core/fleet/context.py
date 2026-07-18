"""Named operating contexts for the fleet config plane (#2550).

A context atomically pins a composite deployment configuration - server URL,
store DSN, adapter defaults, and a budget-envelope name - as one named unit
(``dev``, ``staging-fleet``, ``prod``). Switching contexts is one command
instead of a hand-exported bag of environment variables, and every run
carries a configuration identity a verifier can recompute.

Substrate coupling:

* **Deterministic projection (determinism).** The context's canonical
  document and :meth:`OperatingContext.settings_hash` are pure functions of
  its content. Two installs with identical context content produce a
  byte-identical canonical document and an equal hash, so a run receipt
  embeds a portable configuration identity.

* **Atomic activation as a chain event.** :meth:`ContextStore.activate`
  writes the activation pointer atomically (an effective resolution sees the
  context layer entirely or not at all, never mixed) and records a
  ``fleet.context_activate`` event embedding the settings hash. The context
  contributes exactly one layer to the ``home.py`` precedence chain, between
  project and global; with no context active the four-layer precedence is
  unchanged.

* **Divergence with a named cause.** A :class:`RunContextReceipt` compares a
  recorded hash against the current one; on mismatch it names the diverging
  keys, and a strict policy refuses the replay rather than merely flagging it.
  Config drift becomes a detected hash divergence with a named cause instead
  of a flaky mystery.

This module never imports the CLI or a running server.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit_chain import record_fleet_context_activate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "ContextDivergence",
    "ContextHashMismatch",
    "ContextStore",
    "OperatingContext",
    "RunContextReceipt",
    "canonical_document",
    "settings_hash_of",
    "validate_context_name",
]

#: Fields (in addition to the free-form config layer) that make up the
#: composite configuration a context pins. These are what the effective
#: settings hash and the divergence report are computed over.
_COMPOSITE_FIELDS = ("server_url", "store_dsn", "adapter_defaults", "budget_envelope")

#: Sentinel distinguishing an absent key from an explicit ``None`` value when
#: comparing two effective-settings maps.
_MISSING = object()


class ContextHashMismatch(Exception):
    """Raised when a strict replay finds the effective-settings hash diverged."""


def canonical_document(payload: Mapping[str, Any]) -> str:
    """Serialise *payload* to canonical JSON (sorted keys, compact, ASCII)."""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def settings_hash_of(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:`` hash of *payload*'s canonical document."""
    return "sha256:" + hashlib.sha256(canonical_document(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperatingContext:
    """A named composite deployment configuration.

    Attributes:
        name: The context name (``dev``, ``staging-fleet``, ...).
        server_url: Task-server URL the context pins.
        store_dsn: Persistence DSN the context pins.
        adapter_defaults: Adapter default overrides.
        budget_envelope: Named budget envelope active under this context.
        config_layer: Optional map of ``home.py`` config keys (``model``,
            ``budget``, ``max_agents``, ...) the context contributes to the
            precedence chain between the project and global layers.
    """

    name: str
    server_url: str = ""
    store_dsn: str = ""
    adapter_defaults: dict[str, Any] = field(default_factory=dict)
    budget_envelope: str = ""
    config_layer: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Own a deep copy of the mutable maps so a caller mutating the dict
        # it passed in cannot desync this context from the settings hash that
        # was computed and audited at activation time.
        object.__setattr__(self, "adapter_defaults", copy.deepcopy(self.adapter_defaults))
        object.__setattr__(self, "config_layer", copy.deepcopy(self.config_layer))

    def composite(self) -> dict[str, Any]:
        """Return the composite settings map hashed into the run receipt.

        Returns a deep copy so the receipt's snapshot is independent of later
        in-place mutation of this context.
        """
        composite: dict[str, Any] = {f: copy.deepcopy(getattr(self, f)) for f in _COMPOSITE_FIELDS}
        # The config-layer overrides participate in the identity too, so a
        # drift in any pinned value changes the hash.
        composite["config_layer"] = copy.deepcopy(self.config_layer)
        return composite

    def canonical_document(self) -> str:
        """Return the byte-identical canonical document for this context."""
        return canonical_document(self.composite())

    def settings_hash(self) -> str:
        """Return the canonical effective-settings hash for this context."""
        return settings_hash_of(self.composite())

    def to_document(self) -> dict[str, Any]:
        """Return the on-disk JSON document, including the config layer."""
        return {
            "name": self.name,
            "server_url": self.server_url,
            "store_dsn": self.store_dsn,
            "adapter_defaults": self.adapter_defaults.copy(),
            "budget_envelope": self.budget_envelope,
            "config_layer": self.config_layer.copy(),
        }

    @classmethod
    def from_document(cls, data: Mapping[str, Any]) -> OperatingContext:
        """Parse a context previously produced by :meth:`to_document`."""
        return cls(
            name=data["name"],
            server_url=data.get("server_url", ""),
            store_dsn=data.get("store_dsn", ""),
            adapter_defaults=dict(data.get("adapter_defaults", {})),
            budget_envelope=data.get("budget_envelope", ""),
            config_layer=dict(data.get("config_layer", {})),
        )

    def run_receipt(self) -> RunContextReceipt:
        """Return the run receipt embedding this context's settings hash."""
        return RunContextReceipt(
            context_name=self.name,
            settings_hash=self.settings_hash(),
            settings=self.composite(),
        )


@dataclass(frozen=True)
class ContextDivergence:
    """The result of comparing a recorded run receipt to a current context."""

    ok: bool
    diverging_keys: list[str]
    recorded_hash: str
    current_hash: str


@dataclass(frozen=True)
class RunContextReceipt:
    """A recorded configuration identity for a run.

    Embedded into the run receipt at run start; replay compares it against the
    current context and refuses or flags on divergence.
    """

    context_name: str
    settings_hash: str
    settings: dict[str, Any]

    def verify_against(
        self,
        current: OperatingContext | Mapping[str, Any],
        *,
        strict: bool = False,
    ) -> ContextDivergence:
        """Compare the recorded hash to *current*'s, naming diverging keys.

        Args:
            current: The context (or its composite map) in effect now.
            strict: When ``True``, raise :class:`ContextHashMismatch` on
                divergence (refuse). When ``False``, return the divergence
                report (flag). The policy is caller-selectable.

        Returns:
            A :class:`ContextDivergence` with ``ok`` and the sorted list of
            diverging keys.
        """
        current_map = current.composite() if isinstance(current, OperatingContext) else dict(current)
        current_hash = settings_hash_of(current_map)
        names = set(self.settings) | set(current_map)
        # A missing key and an explicit ``None`` value are different states;
        # use a sentinel so one is never silently reported as equal to the
        # other when naming diverging keys.
        diverging = sorted(k for k in names if self.settings.get(k, _MISSING) != current_map.get(k, _MISSING))
        ok = current_hash == self.settings_hash
        if not ok and strict:
            raise ContextHashMismatch(
                f"effective-settings hash diverged for context {self.context_name!r}; "
                f"diverging keys: {', '.join(diverging)}"
            )
        return ContextDivergence(
            ok=ok,
            diverging_keys=diverging,
            recorded_hash=self.settings_hash,
            current_hash=current_hash,
        )


class ContextStore:
    """Filesystem-backed store of operating contexts, keyed by name.

    Conventionally rooted at ``<project>/.sdd/fleet/contexts``. The active
    context is recorded by an ``active.json`` pointer that ``home.py`` reads;
    activation is atomic and recorded on the audit chain.
    """

    def __init__(self, root: Path, *, chain: AuditChainStore | None = None) -> None:
        self._root = Path(root)
        self._chain = chain

    def _path(self, name: str) -> Path:
        _validate_name(name)
        return self._root / f"{name}.json"

    @property
    def _active_pointer(self) -> Path:
        return self._root / "active.json"

    def create(self, context: OperatingContext) -> OperatingContext:
        """Persist *context* (atomic write). Definition is a local operation.

        A currently-active context cannot be silently replaced with content
        that would change its effective-settings hash: that would alter what a
        running fleet resolves without a new, audited activation. Re-defining
        the active context to a different identity is refused; deactivate (or
        re-activate) it first.

        Raises:
            ValueError: If *context* renames or alters the active context's
                settings identity in place.
        """
        recorded = self._active_settings_hash()
        if recorded is not None and self.active_name() == context.name and context.settings_hash() != recorded:
            raise ValueError(f"context {context.name!r} is active; re-activate to change its settings identity")
        self._root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._path(context.name), context.to_document())
        return context

    def get(self, name: str) -> OperatingContext:
        """Load the context named *name*.

        Raises:
            KeyError: If no context is stored under *name*.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)
        return OperatingContext.from_document(json.loads(path.read_text(encoding="utf-8")))

    def list_names(self) -> list[str]:
        """Return the names of all stored contexts, sorted."""
        if not self._root.exists():
            return []
        return sorted(p.stem for p in self._root.glob("*.json") if p.name != "active.json")

    def activate(self, name: str, *, actor: str = "operator") -> RunContextReceipt:
        """Atomically activate *name* and record a ``fleet.context_activate`` event.

        The activation event is recorded before the activation pointer is
        written, so a live pointer can never exist without its audit record;
        the pointer write is atomic, so an effective config resolution sees
        the context layer entirely or not at all.
        """
        context = self.get(name)
        receipt = context.run_receipt()
        if self._chain is not None:
            record_fleet_context_activate(
                chain=self._chain,
                name=name,
                settings_hash=receipt.settings_hash,
                actor=actor,
            )
        _atomic_write_json(self._active_pointer, {"name": name, "settings_hash": receipt.settings_hash})
        return receipt

    def deactivate(self) -> None:
        """Remove the active-context pointer, restoring four-layer precedence."""
        pointer = self._active_pointer
        if pointer.exists():
            pointer.unlink()

    def active_name(self) -> str | None:
        """Return the active context name, or ``None`` when none is active."""
        return self._active_field("name")

    def _active_settings_hash(self) -> str | None:
        """Return the settings hash recorded at activation, or ``None``."""
        return self._active_field("settings_hash")

    def _active_field(self, field_name: str) -> str | None:
        pointer = self._active_pointer
        if not pointer.exists():
            return None
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = data.get(field_name)
        return value if isinstance(value, str) and value else None

    def active(self) -> OperatingContext | None:
        """Return the active context, or ``None`` when none is active."""
        name = self.active_name()
        if name is None:
            return None
        try:
            return self.get(name)
        except KeyError:
            return None


def validate_context_name(name: str) -> None:
    """Validate an operating-context name, raising ``ValueError`` if invalid.

    Rejects empty names, path separators, ``.``/``..``, the reserved
    ``active`` pointer name, and NUL bytes, so a name can never escape the
    context directory or shadow the activation pointer.
    """
    if not name or "/" in name or "\\" in name or name in {".", "..", "active"} or "\x00" in name:
        raise ValueError(f"invalid operating context name: {name!r}")


#: Backwards-compatible internal alias.
_validate_name = validate_context_name


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
