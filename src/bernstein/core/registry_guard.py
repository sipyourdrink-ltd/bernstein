"""Shared duplicate-registration guard for the registry classes under ``bernstein``.

At least 20 independent registry classes exist under ``src/bernstein``
(adapters, trackers, tunnels, sandbox backends, storage sinks, MCP servers,
trigger sources, agent definitions, and more), and each has historically
reinvented its own duplicate-id handling -- some correctly (raising
``ValueError`` under a lock), some not (``AgentRegistry.register_definition``
promised ``ValueError`` in its own docstring and logged a warning instead).

``DuplicateGuard`` is the one piece of that logic every registry composes
into its own ``register`` method, so the check exists once, is tested once,
and a registry that adopts it cannot silently regress back to warn-and-overwrite.
A registry keeps everything else about how it stores its own values --
``DuplicateGuard`` only tracks, per key, which module registered it first::

    class WidgetRegistry:
        def __init__(self) -> None:
            self._widgets: dict[str, Widget] = {}
            self._guard = DuplicateGuard("widget")

        def register(self, name: str, widget: Widget) -> None:
            self._guard.register(name, caller_module_name())
            self._widgets[name] = widget

A registry that already raises its own exception type (for example
``core/trackers/registry.py``'s ``DuplicateTrackerError``) keeps doing so by
passing ``error_type=`` -- ``DuplicateGuard`` only requires that the type
accept a single message string, which is true of any plain ``Exception`` or
``ValueError`` subclass that does not override ``__init__``.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "DuplicateGuard",
    "DuplicateRegistrationError",
    "RegistrationEntry",
    "RegistryDecisionAction",
    "RegistryDecisionRecord",
    "SupersededRegistrationError",
    "caller_module_name",
    "compute_implementation_hash",
    "load_decision_journal",
    "mark_superseded_module",
    "reconstruct_registry_state",
]

RegistryDecisionAction = Literal["register", "unregister", "supersede"]


class DuplicateRegistrationError(ValueError):
    """Raised when a registry key is registered a second time.

    A plain ``ValueError`` subclass, so registries (and their callers) that
    already ``except ValueError`` around a ``register`` call keep working
    unchanged. The message always names both the module that registered the
    key first and the module whose second registration was refused.
    """


class SupersededRegistrationError(ImportError, ValueError):
    """Raised when importing or registering a superseded module or id.

    Inherits both :class:`ImportError` (so module import-time tombstones fail
    like standard import errors) and :class:`ValueError` (so registration-time
    checks match registry conventions). Always names both the retired symbol
    or module and the canonical id that replaced it.
    """


@dataclass(frozen=True)
class RegistrationEntry:
    """Recorded provenance and content identity for one registered key."""

    key: str
    module_path: str
    content_hash: str
    registered_at: float
    superseded_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "key": self.key,
            "module_path": self.module_path,
            "content_hash": self.content_hash,
            "registered_at": self.registered_at,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistrationEntry:
        """Deserialize from a dictionary produced by :meth:`to_dict`."""
        return cls(
            key=str(data.get("key", "")),
            module_path=str(data.get("module_path", "")),
            content_hash=str(data.get("content_hash", "")),
            registered_at=float(data.get("registered_at", 0.0)),
            superseded_by=str(data.get("superseded_by", "")),
        )


@dataclass(frozen=True)
class RegistryDecisionRecord:
    """Append-only decision record for a registry state mutation."""

    registry_name: str
    action: RegistryDecisionAction
    key: str
    module_path: str
    content_hash: str = ""
    canonical_id: str = ""
    at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "registry_name": self.registry_name,
            "action": self.action,
            "key": self.key,
            "module_path": self.module_path,
            "content_hash": self.content_hash,
            "canonical_id": self.canonical_id,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryDecisionRecord:
        """Deserialize from a dictionary produced by :meth:`to_dict`."""
        raw_action = str(data.get("action", "register"))
        action: RegistryDecisionAction = (
            raw_action if raw_action in ("register", "unregister", "supersede") else "register"  # type: ignore[assignment]
        )
        return cls(
            registry_name=str(data.get("registry_name", "")),
            action=action,
            key=str(data.get("key", "")),
            module_path=str(data.get("module_path", "")),
            content_hash=str(data.get("content_hash", "")),
            canonical_id=str(data.get("canonical_id", "")),
            at=float(data.get("at", 0.0)),
        )


def compute_implementation_hash(
    implementation: Any = None,
    *,
    module_path: str = "",
    key: str = "",
) -> str:
    """Compute a deterministic ``sha256:...`` content digest for *implementation*."""
    hasher = hashlib.sha256()
    hasher.update(module_path.encode("utf-8"))
    hasher.update(b"\0")

    if implementation is not None:
        is_callable_obj = (
            inspect.isclass(implementation) or inspect.isfunction(implementation) or inspect.ismethod(implementation)
        )
        target = implementation if is_callable_obj else type(implementation)
        hasher.update(f"{target.__module__}.{target.__qualname__}".encode())
        hasher.update(b"\0")
        try:
            source = inspect.getsource(target)
            hasher.update(source.encode("utf-8"))
        except (OSError, TypeError):
            source_file = None
            try:
                source_file = inspect.getsourcefile(target)
            except TypeError:
                source_file = None
            if source_file:
                with contextlib.suppress(OSError):
                    hasher.update(Path(source_file).read_bytes())
        if implementation is not target:
            if is_dataclass(implementation):
                payload = json.dumps(asdict(implementation), sort_keys=True, default=repr)
                hasher.update(payload.encode("utf-8"))
            else:
                hasher.update(repr(implementation).encode("utf-8"))
    else:
        mod = sys.modules.get(module_path)
        mod_file = getattr(mod, "__file__", None) if mod is not None else None
        if isinstance(mod_file, str) and mod_file:
            try:
                hasher.update(Path(mod_file).read_bytes())
            except OSError:
                hasher.update(key.encode("utf-8"))
        else:
            hasher.update(key.encode("utf-8"))

    return f"sha256:{hasher.hexdigest()}"


def mark_superseded_module(
    module_path: str,
    canonical_id: str,
    *,
    guard: DuplicateGuard | None = None,
    key: str = "",
) -> NoReturn:
    """Raise :class:`SupersededRegistrationError` when a retired module is imported."""
    if guard is not None:
        guard.supersede(key or module_path, canonical_id, module_path=module_path, retired_module=module_path)
    raise SupersededRegistrationError(
        f"Superseded module {module_path!r} can no longer be imported; use canonical id {canonical_id!r} instead"
    )


def load_decision_journal(journal_path: Path) -> list[RegistryDecisionRecord]:
    """Load persisted registry decision records from JSONL *journal_path*."""
    if not journal_path.is_file():
        return []
    records: list[RegistryDecisionRecord] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            records.append(RegistryDecisionRecord.from_dict(parsed))
    return records


def reconstruct_registry_state(
    decisions: Iterable[RegistryDecisionRecord],
) -> dict[str, RegistrationEntry]:
    """Replay *decisions* to reconstruct the active approved ``{key: RegistrationEntry}`` map."""
    active: dict[str, RegistrationEntry] = {}
    for decision in decisions:
        if decision.action == "register":
            active[decision.key] = RegistrationEntry(
                key=decision.key,
                module_path=decision.module_path,
                content_hash=decision.content_hash,
                registered_at=decision.at,
            )
        elif decision.action in ("unregister", "supersede"):
            active.pop(decision.key, None)
    return active


def caller_module_name(depth: int = 1) -> str:
    """Return the ``__name__`` of the module that called the caller of this function.

    Call this with no arguments from inside a registry's public ``register``
    method to capture *its* caller's module -- the code that actually
    invoked ``register`` -- without every call site having to pass its own
    ``__name__`` explicitly::

        def register(self, name: str, value: Any) -> None:
            module_path = caller_module_name()
            self._guard.register(name, module_path)
            ...

    ``depth`` counts additional frames to skip beyond the immediate caller,
    for a registry that wraps its public ``register`` in another layer
    (a decorator or a mixin's ``register`` calling a private ``_register``)
    before reaching the code that should be named.

    Returns ``"<unknown>"`` if the call stack is shallower than requested
    (for example when called directly from a REPL) rather than raising --
    naming a mis-detected caller in an error message is far less costly
    than the guard itself crashing on a legitimate registration.
    """
    try:
        frame = sys._getframe(depth + 1)  # pyright: ignore[reportPrivateUsage]
    except ValueError:
        return "<unknown>"
    try:
        return frame.f_globals.get("__name__", "<unknown>")
    finally:
        del frame


class DuplicateGuard:
    """Thread-safe bookkeeping of module provenance, content hashes, supersession, and decisions.

    Args:
        registry_name: Short human-readable label used in error messages
            (for example ``"agent definition"`` or ``"sandbox backend"``).
        journal_path: Optional JSONL path where every mutation writes a
            :class:`RegistryDecisionRecord` row.
    """

    def __init__(self, registry_name: str, *, journal_path: Path | None = None) -> None:
        self._registry_name = registry_name
        self._entries: dict[str, RegistrationEntry] = {}
        self._superseded_keys: dict[str, str] = {}
        self._superseded_modules: dict[str, str] = {}
        self._decisions: list[RegistryDecisionRecord] = []
        self._journal_path = journal_path
        self._lock = threading.RLock()

    def attach_journal(self, journal_path: Path) -> None:
        """Attach a JSONL decision journal path and flush buffered records."""
        with self._lock:
            self._journal_path = journal_path
            for decision in self._decisions:
                self._append_journal_row(decision)

    def register(
        self,
        key: str,
        module_path: str,
        *,
        implementation: Any = None,
        content_hash: str = "",
        error_type: type[Exception] = DuplicateRegistrationError,
    ) -> RegistrationEntry:
        """Record that ``module_path`` registered ``key`` with its content hash.

        Args:
            key: The registry id being registered.
            module_path: The ``__name__`` of the module performing the
                registration, typically obtained via :func:`caller_module_name`.
            implementation: Optional registered object/class/callable used to
                compute a deterministic content digest when ``content_hash``
                is omitted.
            content_hash: Explicit ``sha256:...`` digest override.
            error_type: Exception type to raise on a duplicate. Must be
                constructible from a single message string. Defaults to
                :class:`DuplicateRegistrationError`.

        Returns:
            The recorded :class:`RegistrationEntry`.

        Raises:
            SupersededRegistrationError: If ``key`` or ``module_path`` has
                been marked superseded, naming the canonical id.
            error_type: If ``key`` is already registered, naming both the
                existing and the incoming module path.
        """
        resolved_hash = content_hash or compute_implementation_hash(
            implementation,
            module_path=module_path,
            key=key,
        )
        now = time.time()
        with self._lock:
            canonical = self._superseded_keys.get(key) or self._superseded_modules.get(module_path)
            if canonical is not None:
                raise SupersededRegistrationError(
                    f"Superseded {self._registry_name} {key!r} ({module_path!r}) cannot register; "
                    f"use canonical id {canonical!r} instead"
                )
            existing = self._entries.get(key)
            if existing is not None:
                raise error_type(
                    f"Duplicate {self._registry_name}: {key!r} is already registered from "
                    f"{existing.module_path!r} ({existing.content_hash}); refusing second registration "
                    f"from {module_path!r} ({resolved_hash})"
                )
            entry = RegistrationEntry(
                key=key,
                module_path=module_path,
                content_hash=resolved_hash,
                registered_at=now,
            )
            self._entries[key] = entry
            self._record_decision(
                RegistryDecisionRecord(
                    registry_name=self._registry_name,
                    action="register",
                    key=key,
                    module_path=module_path,
                    content_hash=resolved_hash,
                    at=now,
                )
            )
            return entry

    def forget(self, key: str, *, module_path: str = "") -> None:
        """Drop bookkeeping for ``key``, allowing it to be registered again.

        Call this from a registry's ``unregister``/``remove`` method so a
        legitimate re-registration after removal is not mistaken for a
        duplicate.
        """
        with self._lock:
            existing = self._entries.pop(key, None)
            if existing is not None:
                self._record_decision(
                    RegistryDecisionRecord(
                        registry_name=self._registry_name,
                        action="unregister",
                        key=key,
                        module_path=module_path or existing.module_path,
                        content_hash=existing.content_hash,
                        at=time.time(),
                    )
                )

    def supersede(
        self,
        key: str,
        canonical_id: str,
        *,
        module_path: str = "",
        retired_module: str = "",
    ) -> RegistryDecisionRecord:
        """Retire ``key`` (and optionally ``retired_module``) in favor of ``canonical_id``."""
        now = time.time()
        with self._lock:
            existing = self._entries.pop(key, None)
            resolved_module = retired_module or module_path or (existing.module_path if existing else "")
            resolved_hash = existing.content_hash if existing else ""
            self._superseded_keys[key] = canonical_id
            if retired_module:
                self._superseded_modules[retired_module] = canonical_id
            decision = RegistryDecisionRecord(
                registry_name=self._registry_name,
                action="supersede",
                key=key,
                module_path=resolved_module,
                content_hash=resolved_hash,
                canonical_id=canonical_id,
                at=now,
            )
            self._record_decision(decision)
            return decision

    def module_path_for(self, key: str) -> str | None:
        """Return the module path that registered ``key``, or ``None``."""
        with self._lock:
            entry = self._entries.get(key)
            return entry.module_path if entry is not None else None

    def content_hash_for(self, key: str) -> str | None:
        """Return the recorded implementation hash for ``key``, or ``None``."""
        with self._lock:
            entry = self._entries.get(key)
            return entry.content_hash if entry is not None else None

    def entry_for(self, key: str) -> RegistrationEntry | None:
        """Return the full :class:`RegistrationEntry` for ``key``, or ``None``."""
        with self._lock:
            return self._entries.get(key)

    def entries(self) -> dict[str, RegistrationEntry]:
        """Return a shallow copy of active registrations keyed by id."""
        with self._lock:
            return dict(self._entries)

    def is_superseded(self, key: str) -> str | None:
        """Return the canonical id if ``key`` was superseded, else ``None``."""
        with self._lock:
            return self._superseded_keys.get(key)

    def decision_records(self) -> list[RegistryDecisionRecord]:
        """Return all recorded registry mutation decisions in chronological order."""
        with self._lock:
            return list(self._decisions)

    def _record_decision(self, decision: RegistryDecisionRecord) -> None:
        self._decisions.append(decision)
        if self._journal_path is not None:
            self._append_journal_row(decision)

    def _append_journal_row(self, decision: RegistryDecisionRecord) -> None:
        if self._journal_path is None:
            return
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
