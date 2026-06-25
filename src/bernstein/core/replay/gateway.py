"""Record/replay gateway for LLM requests and tool dispatch.

The gateway sits between Bernstein's adapter call-sites and the live
providers. In *record* mode every (kind, key) -> response pair is appended
to ``.sdd/runs/<run_id>/events.jsonl``. In *replay* mode the gateway
serves the recorded response instead of invoking the real provider, so a
run can be re-executed deterministically against recorded fixtures.

Design choices:

* **Append-only JSONL** - same on-disk shape as existing trace files; works
  with the rest of the observability stack and stays human-diffable.
* **Recording is opt-in** - controlled by :data:`RECORD_ENV_VAR` or an
  explicit ``record=True`` argument. We don't want to bloat ``.sdd/`` for
  users who never replay.
* **Stable keys** - callers pass an explicit ``key`` (typically a SHA-256
  of the request payload). The gateway never tries to fingerprint the
  request itself; key stability is the caller's job.
* **First-call ordering preserved** - replay lookup falls back to FIFO
  consumption per ``kind`` when the key isn't found, so even hashed
  prompts with timestamp jitter replay cleanly.
"""

from __future__ import annotations

import json
import logging
import operator
import os
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Name of the per-run gateway event log inside ``.sdd/runs/<id>/``.
EVENTS_FILENAME = "events.jsonl"

#: Environment variable that opts the gateway into record mode.
#: Recording stays off by default to avoid growing ``.sdd/`` on every
#: invocation. Set to ``1``/``true``/``yes`` to enable.
RECORD_ENV_VAR = "BERNSTEIN_RECORD"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_recording_enabled(env: dict[str, str] | None = None) -> bool:
    """Return whether the gateway should record this run by default.

    Args:
        env: Optional env dict (defaults to :data:`os.environ`).

    Returns:
        ``True`` if :data:`RECORD_ENV_VAR` is set to a truthy value.
    """
    src = env if env is not None else os.environ
    return src.get(RECORD_ENV_VAR, "").strip().lower() in _TRUTHY


class GatewayMode(StrEnum):
    """Operating mode for :class:`ReplayGateway`."""

    OFF = "off"
    """Pass-through; no recording, no replay."""

    RECORD = "record"
    """Invoke the live provider and append each response to ``events.jsonl``."""

    REPLAY = "replay"
    """Serve recorded responses; never call the live provider."""


class ReplayMissError(RuntimeError):
    """Raised in :attr:`GatewayMode.REPLAY` when no fixture matches."""


@dataclass(frozen=True)
class _Event:
    """One row from ``events.jsonl``."""

    kind: str
    key: str
    response: Any
    ts: float
    seq: int


@dataclass
class _Fixture:
    """A recorded response plus its consumption state during replay.

    Holds the recorded ``response`` and a ``consumed`` flag. The fixture lives
    in exactly one per-kind ordered list (recorded order); the by-key index
    references it by position, so a by-key consume and a by-kind consume mark
    the same object - the two views can never disagree on which recorded slot
    was served (#1855).
    """

    response: Any
    consumed: bool = field(default=False)


class ReplayGateway:
    """Thin wrapper around LLM + tool dispatch with record/replay support.

    Typical usage from an adapter call-site::

        gw = ReplayGateway(run_id="20260517-1530", sdd_dir=Path(".sdd"))
        text = gw.dispatch(
            kind="llm",
            key=request_hash,
            invoke=lambda: real_llm_client.complete(prompt),
        )

    With ``BERNSTEIN_RECORD=1`` (or ``ReplayGateway(record=True)``) the
    response from ``invoke`` is appended to ``events.jsonl``. In replay
    mode, ``invoke`` is **not** called; the recorded response is returned
    instead.

    Args:
        run_id: Unique identifier for this run; used to locate the
            per-run event log under ``.sdd/runs/<run_id>/``.
        sdd_dir: Path to the ``.sdd`` directory.
        mode: Explicit :class:`GatewayMode`. If omitted, defaults to
            :attr:`GatewayMode.RECORD` when :func:`is_recording_enabled`
            is true and ``record`` is not set, else :attr:`GatewayMode.OFF`.
        record: Convenience flag - when ``True``, forces record mode even
            if the env var is unset. Ignored if ``mode`` is provided.
    """

    def __init__(
        self,
        run_id: str,
        sdd_dir: Path,
        *,
        mode: GatewayMode | None = None,
        record: bool = False,
    ) -> None:
        self._run_id = run_id
        self._path = sdd_dir / "runs" / run_id / EVENTS_FILENAME
        self._lock = threading.Lock()
        self._seq = 0

        if mode is None:
            mode = GatewayMode.RECORD if record or is_recording_enabled() else GatewayMode.OFF
        self._mode = mode

        # Replay-mode fixture state. A single ordered list per kind is the
        # source of truth (recorded order); the by-key index points into it by
        # position, so consuming a fixture by key and by kind can never desync
        # even when distinct keys recorded identical response values (#1855).
        self._ordered_by_kind: dict[str, list[_Fixture]] = {}
        self._positions_by_key: dict[tuple[str, str], deque[int]] = {}
        # Per-kind cursor: index of the first not-yet-consumed fixture, so the
        # by-kind FIFO fallback is amortised O(1) instead of rescanning.
        self._kind_cursor: dict[str, int] = {}

        if self._mode is GatewayMode.RECORD:
            # Only create the directory when we'll actually write something.
            # Replay mode reads existing files; OFF mode does nothing.
            self._path.parent.mkdir(parents=True, exist_ok=True)
        elif self._mode is GatewayMode.REPLAY:
            self._load_fixtures()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def mode(self) -> GatewayMode:
        """Current operating mode."""
        return self._mode

    @property
    def path(self) -> Path:
        """Path to ``events.jsonl`` for this run."""
        return self._path

    @property
    def run_id(self) -> str:
        """The run identifier this gateway targets."""
        return self._run_id

    def dispatch(
        self,
        *,
        kind: str,
        key: str,
        invoke: Callable[[], Any],
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Run a recorded/replayed dispatch.

        Args:
            kind: Logical category (e.g. ``"llm"``, ``"tool"``). Used to
                bucket replay fixtures when keys collide.
            key: Stable identifier for this request (typically a hash of
                the request payload). Replay lookups try ``(kind, key)``
                first, then fall back to FIFO consumption of ``kind``.
            invoke: Callable that performs the real dispatch. Called in
                :attr:`GatewayMode.OFF` and :attr:`GatewayMode.RECORD`;
                **never** called in :attr:`GatewayMode.REPLAY`.
            metadata: Optional extra fields persisted alongside the event
                (e.g. model name, adapter name) for debugging.

        Returns:
            The response (either from ``invoke`` or from the fixture).

        Raises:
            ReplayMissError: In replay mode when no fixture matches and
                no FIFO fallback is available for ``kind``.
        """
        if self._mode is GatewayMode.REPLAY:
            return self._replay_lookup(kind=kind, key=key)

        response = invoke()

        if self._mode is GatewayMode.RECORD:
            self._record(kind=kind, key=key, response=response, metadata=metadata)

        return response

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(
        self,
        *,
        kind: str,
        key: str,
        response: Any,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Append one event to ``events.jsonl``.

        Sequence assignment AND the file write happen under ``self._lock``.
        Splitting these two steps (lock-then-release before opening the
        file) let two concurrent record calls swap their ``seq`` order in
        the file, and worse - concurrent file writes past PIPE_BUF can
        interleave bytes, producing malformed JSONL the replay loader
        then silently skips. The lock is local to the gateway, so the
        critical section is short and uncontended for typical adapter
        traffic.
        """
        entry: dict[str, Any] = {
            "ts": time.time(),
            "kind": kind,
            "key": key,
            "response": _make_jsonable(response),
        }
        if metadata:
            entry["metadata"] = _make_jsonable(metadata)

        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            # ``json.dumps`` runs inside the lock so the ``seq`` field is
            # consistent with the file order; the lock also serialises the
            # subsequent file.write so two concurrent records can never
            # interleave bytes past PIPE_BUF.
            line = json.dumps(entry, default=str)
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                # Recording is a debug aid; failures must not break the run.
                logger.warning("ReplayGateway: failed to record %r: %s", kind, exc)

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def _load_fixtures(self) -> None:
        """Load fixtures from ``events.jsonl`` into ordered per-kind lists.

        Events are ordered by their recorded ``seq`` so the by-kind FIFO
        fallback replays in recorded order regardless of duplicate response
        values. Rows missing ``seq`` (legacy logs predating the per-event
        sequence) fall back to file order, which is the implicit recorded
        order. The by-key index records each fixture's *position* in its
        kind's ordered list, so a by-key consume marks the exact recorded
        slot rather than the first slot with a matching value (#1855).
        """
        if not self._path.exists():
            raise ReplayMissError(
                f"No events log at {self._path}; nothing to replay. "
                "Was BERNSTEIN_RECORD=1 set during the original run?",
            )
        # encoding="utf-8" mirrors the record path; relying on the platform
        # default broke replay on Windows runners where cp1252 was active.
        rows: list[tuple[int, int, str, str, Any]] = []
        with self._path.open(encoding="utf-8") as f:
            for file_pos, raw in enumerate(f):
                row_str = raw.strip()
                if not row_str:
                    continue
                try:
                    row = json.loads(row_str)
                except json.JSONDecodeError:
                    logger.warning("ReplayGateway: skipping malformed line in %s", self._path)
                    continue
                kind = str(row.get("kind", ""))
                key = str(row.get("key", ""))
                response = row.get("response")
                # ``seq`` is the recorded order; legacy logs without it use file
                # order. ``file_pos`` is the stable tiebreak so two rows sharing
                # a seq (or both missing it) keep their on-disk order.
                seq_raw = row.get("seq")
                seq = int(seq_raw) if isinstance(seq_raw, int) else file_pos
                rows.append((seq, file_pos, kind, key, response))

        # Sort by (seq, file_pos) so the per-kind lists are in recorded order
        # even if the log was written or stitched out of strict line order.
        rows.sort(key=operator.itemgetter(0, 1))
        for _seq, _pos, kind, key, response in rows:
            ordered = self._ordered_by_kind.setdefault(kind, [])
            fixture = _Fixture(response=response)
            position = len(ordered)
            ordered.append(fixture)
            self._positions_by_key.setdefault((kind, key), deque()).append(position)

    def _next_unconsumed_index(self, kind: str, ordered: list[_Fixture]) -> int | None:
        """Return the index of the lowest unconsumed fixture for ``kind``.

        Advances and caches a per-kind cursor past already-consumed fixtures
        so repeated by-kind fallbacks stay amortised O(1). Returns ``None``
        when every fixture for the kind has been consumed.
        """
        cursor = self._kind_cursor.get(kind, 0)
        while cursor < len(ordered) and ordered[cursor].consumed:
            cursor += 1
        self._kind_cursor[kind] = cursor
        return cursor if cursor < len(ordered) else None

    def _replay_lookup(self, *, kind: str, key: str) -> Any:
        """Consume the next fixture for ``(kind, key)`` (or FIFO by ``kind``).

        On a by-key hit, consume the lowest unconsumed recorded position for
        that exact ``(kind, key)``. On a miss, fall back to the lowest
        unconsumed position for the kind (recorded-order FIFO). Both paths
        mark the same per-kind ordered list, so duplicate response values can
        never desync the two views (#1855).

        Holds ``self._lock`` for the entire consume so concurrent dispatches
        cannot drain the same fixture twice or skip rows another thread has
        already consumed under the by-kind fallback.
        """
        with self._lock:
            ordered = self._ordered_by_kind.get(kind)
            if ordered is None:
                raise ReplayMissError(
                    f"No fixture for kind={kind!r} key={key!r} in {self._path}. "
                    "Either the run diverged or recording was incomplete.",
                )

            positions = self._positions_by_key.get((kind, key))
            # Skip positions already consumed via the by-kind fallback so a
            # by-key hit never returns a slot that was served as FIFO filler.
            while positions:
                idx = positions[0]
                if ordered[idx].consumed:
                    positions.popleft()
                    continue
                positions.popleft()
                ordered[idx].consumed = True
                return ordered[idx].response

            # By-kind FIFO fallback: lowest unconsumed recorded position.
            fallback_idx = self._next_unconsumed_index(kind, ordered)
            if fallback_idx is not None:
                ordered[fallback_idx].consumed = True
                return ordered[fallback_idx].response

        raise ReplayMissError(
            f"No fixture for kind={kind!r} key={key!r} in {self._path}. "
            "Either the run diverged or recording was incomplete.",
        )


def _make_jsonable(value: Any) -> Any:
    """Best-effort coercion of ``value`` to JSON-serialisable shape.

    Falls back to ``repr`` for opaque objects; primitive types and
    standard containers are passed through unchanged.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_make_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _make_jsonable(v) for k, v in value.items()}
    # Dataclasses, pydantic, custom objects: try dict-like, then repr.
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        with suppress(TypeError, ValueError):
            return _make_jsonable(to_dict())
    return repr(value)
