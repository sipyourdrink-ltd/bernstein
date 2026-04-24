"""File-backed queue of pending tool-call approvals.

Persists :class:`PendingApproval` entries under
``.sdd/runtime/approvals/*.json`` using atomic writes (``os.replace``).
An in-process :class:`asyncio.Event` per approval id lets the
security-layer gate ``await`` resolution without busy-polling while the
file store keeps cross-process resolvers (web UI, CLI) in sync.

The default TTL is 10 minutes; callers may override it per-push or via
``bernstein.yaml :: approvals.timeout_seconds``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from bernstein.core.approval.models import (
    ApprovalDecision,
    ApprovalTimeoutError,
    PendingApproval,
    ResolvedApproval,
)

logger = logging.getLogger(__name__)

#: Default time-to-live for a queued approval, in seconds.
DEFAULT_TTL_SECONDS: int = 600

#: Relative directory used for on-disk persistence. All files live under
#: ``<workdir>/.sdd/runtime/approvals/`` so a single workdir hosts one
#: logical queue shared across the TUI, web, and CLI resolvers.
_RUNTIME_DIR = Path(".sdd") / "runtime" / "approvals"

#: Suffix for decision sentinels written next to the pending files.
_RESOLVED_SUFFIX = ".resolved.json"

#: Filename suffix for pending approval records.
_PENDING_SUFFIX = ".json"


def _atomic_write(path: Path, payload: str) -> None:
    """Write *payload* to *path* atomically via ``os.replace``.

    A temp file is created in the same directory so the rename stays on
    one filesystem (rename across filesystems is not atomic on POSIX).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Clean up temp on failure; swallow cleanup errors so the original
        # exception surfaces.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class ApprovalQueue:
    """Thread-safe, file-backed queue of pending tool-call approvals.

    The queue is intentionally lightweight: it holds FIFO order, wires
    one :class:`asyncio.Event` per approval id so an agent coroutine can
    ``await wait_for(id)`` without polling, and mirrors every state
    change to ``<base_dir>/*.json`` so out-of-process resolvers (the web
    UI and the ``bernstein approve`` CLI) observe the same queue.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        """Create a queue rooted at *base_dir* (defaults to cwd)."""
        if base_dir is None:
            base_dir = Path.cwd() / _RUNTIME_DIR
        self._base_dir = base_dir
        self._lock = threading.RLock()
        self._pending: dict[str, PendingApproval] = {}
        self._resolved: dict[str, ResolvedApproval] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._load_from_disk()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        """Return the on-disk directory that backs this queue."""
        return self._base_dir

    def _pending_path(self, approval_id: str) -> Path:
        """Return the pending-file path for *approval_id*."""
        return self._base_dir / f"{approval_id}{_PENDING_SUFFIX}"

    def _resolved_path(self, approval_id: str) -> Path:
        """Return the decision-sentinel path for *approval_id*."""
        return self._base_dir / f"{approval_id}{_RESOLVED_SUFFIX}"

    def _load_from_disk(self) -> None:
        """Rehydrate in-memory state from existing JSON files.

        Both pending and already-resolved entries are restored so that a
        fresh queue instance in a different process (the CLI, the web
        server, etc.) sees the same authoritative state as the producer.
        """
        if not self._base_dir.exists():
            return
        for entry in sorted(self._base_dir.glob(f"*{_PENDING_SUFFIX}")):
            if entry.name.endswith(_RESOLVED_SUFFIX):
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable approval %s: %s", entry.name, exc)
                continue
            approval = PendingApproval.from_dict(data)
            self._pending[approval.id] = approval

        for entry in sorted(self._base_dir.glob(f"*{_RESOLVED_SUFFIX}")):
            try:
                raw = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable resolution %s: %s", entry.name, exc)
                continue
            try:
                decision = ApprovalDecision(str(raw.get("decision", "")))
            except ValueError:
                continue
            approval_id = str(raw.get("approval_id", ""))
            if not approval_id:
                continue
            self._resolved[approval_id] = ResolvedApproval(
                approval_id=approval_id,
                decision=decision,
                reason=str(raw.get("reason", "")),
                resolved_at=float(raw.get("resolved_at", 0.0)),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, approval: PendingApproval) -> PendingApproval:
        """Enqueue *approval* and return it.

        The on-disk JSON is written atomically; the in-memory entry is
        added under the queue lock so concurrent ``push``/``resolve`` is
        safe. An :class:`asyncio.Event` is lazily prepared so a later
        :meth:`wait_for` call can observe the decision without races.
        """
        with self._lock:
            self._pending[approval.id] = approval
            self._events.setdefault(approval.id, asyncio.Event())
        payload = json.dumps(approval.to_dict(), indent=2, sort_keys=True)
        _atomic_write(self._pending_path(approval.id), payload)
        logger.info(
            "Queued approval %s for tool=%s session=%s ttl=%ss",
            approval.id,
            approval.tool_name,
            approval.session_id,
            approval.ttl_seconds,
        )
        return approval

    def list_pending(self, session_id: str | None = None, *, now: float | None = None) -> list[PendingApproval]:
        """Return pending approvals in FIFO order.

        Expired entries are filtered out of the returned list but remain
        on disk until a resolver (or the next :meth:`wait_for` timeout)
        evicts them.

        Args:
            session_id: When given, only return approvals for this
                session id.
            now: Optional injected timestamp for deterministic tests.
        """
        with self._lock:
            items = list(self._pending.values())
        items.sort(key=lambda a: a.created_at)
        if session_id is not None:
            items = [a for a in items if a.session_id == session_id]
        current = time.time() if now is None else now
        return [a for a in items if not a.is_expired(now=current)]

    def get(self, approval_id: str) -> PendingApproval | None:
        """Return the pending approval with *approval_id*, or ``None``."""
        with self._lock:
            return self._pending.get(approval_id)

    def resolve(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        reason: str = "",
    ) -> ResolvedApproval:
        """Record an operator decision for *approval_id*.

        Resolving the same id more than once is idempotent: the first
        call wins and subsequent calls return the original resolution
        unchanged. This matches the behaviour expected by racing
        resolvers (web UI + CLI acting on the same approval).

        Args:
            approval_id: Id of the pending approval to resolve.
            decision: Operator verdict.
            reason: Optional free-form note included in the sentinel.

        Returns:
            The authoritative :class:`ResolvedApproval`.

        Raises:
            KeyError: When *approval_id* has neither a pending entry nor
                an already-resolved record.
        """
        with self._lock:
            existing = self._resolved.get(approval_id)
            if existing is not None:
                return existing
            if approval_id not in self._pending:
                raise KeyError(f"Unknown approval id: {approval_id}")
            resolution = ResolvedApproval(approval_id=approval_id, decision=decision, reason=reason)
            self._resolved[approval_id] = resolution
            event = self._events.setdefault(approval_id, asyncio.Event())
            self._pending.pop(approval_id, None)

        payload = json.dumps(
            {
                "approval_id": resolution.approval_id,
                "decision": resolution.decision.value,
                "reason": resolution.reason,
                "resolved_at": resolution.resolved_at,
            },
            indent=2,
            sort_keys=True,
        )
        _atomic_write(self._resolved_path(approval_id), payload)
        # Remove the pending sentinel so list_pending() on re-open is clean.
        try:
            self._pending_path(approval_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not remove pending file for %s: %s", approval_id, exc)

        # Signal any awaiting wait_for(). Event.set() is thread-safe but
        # must be scheduled on the loop that created the Event.
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()
        logger.info(
            "Resolved approval %s decision=%s",
            approval_id,
            decision.value,
        )
        return resolution

    def get_resolution(self, approval_id: str) -> ResolvedApproval | None:
        """Return the resolution for *approval_id*, or ``None``."""
        with self._lock:
            return self._resolved.get(approval_id)

    async def wait_for(
        self,
        approval_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ResolvedApproval:
        """Block until the approval is resolved or its TTL expires.

        Args:
            approval_id: Id returned by :meth:`push`.
            timeout_seconds: Optional override for the wait timeout. When
                ``None`` the approval's ``ttl_seconds`` is used.

        Returns:
            The :class:`ResolvedApproval` produced by :meth:`resolve`.

        Raises:
            ApprovalTimeoutError: When the TTL passes before a decision
                is recorded. A ``REJECT`` resolution is also persisted so
                subsequent readers see a terminal state.
            KeyError: When *approval_id* was never pushed.
        """
        with self._lock:
            existing = self._resolved.get(approval_id)
            if existing is not None:
                return existing
            pending = self._pending.get(approval_id)
            if pending is None:
                raise KeyError(f"Unknown approval id: {approval_id}")
            event = self._events.setdefault(approval_id, asyncio.Event())
            self._loop = asyncio.get_running_loop()

        effective = float(pending.ttl_seconds) if timeout_seconds is None else float(timeout_seconds)
        remaining = max(0.0, pending.expires_at - time.time())
        wait = min(effective, remaining) if timeout_seconds is None else effective

        try:
            await asyncio.wait_for(event.wait(), timeout=wait)
        except TimeoutError as exc:
            # Persist an implicit reject so other resolvers see the terminal state.
            reason = (
                f"Approval {approval_id} for tool '{pending.tool_name}' "
                f"timed out after {pending.ttl_seconds}s without an operator decision."
            )
            # Already-resolved in a race is fine — fall through and
            # return whichever resolution landed first.
            with contextlib.suppress(KeyError):
                self.resolve(approval_id, ApprovalDecision.REJECT, reason=reason)
            resolved = self.get_resolution(approval_id)
            if resolved is not None and resolved.decision is not ApprovalDecision.REJECT:
                return resolved
            raise ApprovalTimeoutError(reason) from exc

        resolved = self.get_resolution(approval_id)
        if resolved is None:  # pragma: no cover — defensive
            raise RuntimeError(f"Approval {approval_id} event fired without a recorded resolution")
        return resolved

    def evict_expired(self, *, now: float | None = None) -> list[str]:
        """Reject every approval whose TTL has lapsed.

        Returns the list of approval ids that were newly rejected. This
        is primarily useful for tests and for the watchdog-style sweeper
        that the server may run alongside the queue.
        """
        current = time.time() if now is None else now
        expired_ids: list[str] = []
        with self._lock:
            # Snapshot via tuple() so concurrent mutations to _pending can't
            # raise RuntimeError mid-iteration. tuple() is iterable without
            # the extra list() allocation Sonar flagged (python:S7504).
            for approval_id, approval in tuple(self._pending.items()):
                if approval.is_expired(now=current):
                    expired_ids.append(approval_id)
        for approval_id in expired_ids:
            try:
                self.resolve(
                    approval_id,
                    ApprovalDecision.REJECT,
                    reason="expired",
                )
            except KeyError:
                continue
        return expired_ids


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_default_queue: ApprovalQueue | None = None
_default_queue_lock = threading.Lock()


def get_default_queue(base_dir: Path | None = None) -> ApprovalQueue:
    """Return the process-wide default :class:`ApprovalQueue`.

    The first call wins: passing a different *base_dir* later has no
    effect. Tests that need isolation should construct their own
    :class:`ApprovalQueue` instead of using the default.
    """
    global _default_queue
    with _default_queue_lock:
        if _default_queue is None:
            _default_queue = ApprovalQueue(base_dir=base_dir)
        return _default_queue


def reset_default_queue() -> None:
    """Drop the cached default queue. Intended for tests only."""
    global _default_queue
    with _default_queue_lock:
        _default_queue = None


# ---------------------------------------------------------------------------
# Always-allow promotion
# ---------------------------------------------------------------------------


def _derive_rule(approval: PendingApproval) -> dict[str, Any]:
    """Build an always-allow rule entry for *approval*.

    Picks the first string argument we recognise as an input field and
    uses its exact value as a literal glob. Operators can edit the rule
    file afterwards to broaden the pattern.
    """
    args = approval.tool_args
    for field_name in ("path", "file_path", "command", "query"):
        value = args.get(field_name)
        if isinstance(value, str) and value:
            return {
                "id": f"aa-{approval.tool_name.lower()}-{approval.id}",
                "tool": approval.tool_name,
                "input_field": field_name,
                "input_pattern": value,
                "description": (f"Promoted from interactive approval {approval.id} at {datetime.now(UTC).isoformat()}"),
            }
    return {
        "id": f"aa-{approval.tool_name.lower()}-{approval.id}",
        "tool": approval.tool_name,
        "input_field": "path",
        "input_pattern": "*",
        "description": (
            f"Promoted from interactive approval {approval.id}; "
            "no recognisable input field — review before relying on this rule."
        ),
    }


def promote_to_always_allow(
    approval: PendingApproval,
    *,
    workdir: Path | None = None,
    rules_path: Path | None = None,
) -> Path:
    """Append an always-allow rule derived from *approval* and return the path.

    The rule is written to the user's agent-writable rules file
    (``<workdir>/.bernstein/always_allow.yaml``) unless *rules_path* is
    given. A companion manifest is re-signed via
    :func:`bernstein.core.security.always_allow.write_always_allow_manifest`
    so the loader accepts the updated file on the next run.

    Args:
        approval: The approval being promoted.
        workdir: Project root; defaults to the current working directory.
        rules_path: Override for the target rules file.

    Returns:
        The path to the rules file that was updated.
    """
    from bernstein.core.security.always_allow import write_always_allow_manifest

    root = workdir if workdir is not None else Path.cwd()
    target = rules_path if rules_path is not None else root / ".bernstein" / "always_allow.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict[str, Any]] = []
    if target.exists():
        try:
            raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Rules file %s unreadable; starting fresh: %s", target, exc)
            raw = None
        if isinstance(raw, dict) and "always_allow" in raw:
            section = raw.get("always_allow") or []
            if isinstance(section, list):
                existing = [item for item in section if isinstance(item, dict)]
        elif isinstance(raw, list):
            existing = [item for item in raw if isinstance(item, dict)]

    rule = _derive_rule(approval)
    existing.append(rule)
    payload = yaml.safe_dump({"always_allow": existing}, default_flow_style=False, sort_keys=False)
    _atomic_write(target, payload)

    # Refresh the manifest so the tamper check passes on reload.
    try:
        write_always_allow_manifest(root, target)
    except (OSError, ValueError) as exc:
        logger.warning("Wrote rule but could not refresh always-allow manifest: %s", exc)
    return target
